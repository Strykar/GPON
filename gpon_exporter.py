import argparse
import logging
import os
import re
import sys
import threading
import time

import paramiko
from prometheus_client import start_http_server, Gauge, Info, Counter


__version__ = '1.0.0'

log = logging.getLogger("gpon_exporter")


class _LoggingHostKeyPolicy(paramiko.MissingHostKeyPolicy):  # pylint: disable=too-few-public-methods
    """Trust on first use with file-backed persistence.

    The state file lives at the path passed to the constructor (typically
    `~/.config/gpon-exporter/known_hosts`). Format: one line per host,
    `hostname keytype fingerprint-hex`.

    On the first connection to a host the policy logs a WARNING ("first
    contact") and records the key. On a later connection with a different
    key it logs a WARNING ("host key changed") and updates the record.
    Connections always proceed -- the policy is descriptive, not enforcing.
    Without persistence (file unwritable, or no `--known-hosts` provided),
    every restart looks like first contact, which still beats AutoAddPolicy
    because at least the fingerprint is logged.
    """
    def __init__(self, store_path=None):
        self._store_path = store_path
        self._known = self._load() if store_path else {}

    def _load(self):
        out = {}
        try:
            with open(self._store_path, encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    host, kt, fp = line.split(None, 2)
                    out[host] = (kt, fp)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            log.warning('cannot read host-key store %s: %s', self._store_path, e)
        return out

    def _save(self):
        if not self._store_path:
            return
        try:
            os.makedirs(os.path.dirname(self._store_path) or '.', exist_ok=True)
            with open(self._store_path, 'w', encoding='utf-8') as f:
                for host, (kt, fp) in sorted(self._known.items()):
                    f.write(f'{host} {kt} {fp}\n')
            os.chmod(self._store_path, 0o600)
        except OSError as e:
            log.warning('cannot persist host-key store %s: %s', self._store_path, e)

    def missing_host_key(self, client, hostname, key):
        kt, fp = key.get_name(), key.get_fingerprint().hex()
        prev = self._known.get(hostname)
        if prev is None:
            # First contact: log + persist. This is the ONLY path that
            # writes to disk -- see the elif branch.
            log.warning('first SSH connect to %s; trusting host key %s %s', hostname, kt, fp)
            self._known[hostname] = (kt, fp)
            self._save()
        elif prev != (kt, fp):
            # Key change: log durably, do NOT persist. The connection
            # still proceeds (this policy is descriptive, not enforcing
            # -- a homelab SFP behind RejectPolicy() would be more
            # annoying than useful), BUT the on-disk fingerprint stays
            # at the original. Every subsequent fetch (and every daemon
            # restart loading the file) re-emits this warning until the
            # operator hand-edits the known-hosts file or removes it.
            #
            # This means: signal is durable, but the credential is still
            # bled to whoever holds the swapped key for as long as the
            # operator ignores the warning. Use --known-hosts '' to
            # silence persistence entirely, or swap to RejectPolicy() in
            # the source for real enforcement.
            log.warning('SSH host key for %s changed: was %s %s, now %s %s',
                        hostname, prev[0], prev[1], kt, fp)
        # Always make the current connection's PKey available to paramiko
        # for this single SSHClient instance. This does not touch our
        # persistent _known mapping or the on-disk store.
        client.get_host_keys().add(hostname, kt, key)


parser = argparse.ArgumentParser(
    description='GPON Metrics Exporter',
    epilog=(
        'ONU connection string format: user:password@host[:port]\n'
        '  --device admin:abc123@192.168.1.1\n'
        '  --device admin:abc123@192.168.1.1:22         # explicit port\n'
        '  --device admin@192.168.1.1                   # password from ONU_SSH_PASSWORD\n'
        '  --device admin@host1 --device user2@host2    # multiple ONUs\n'
        'If the password is omitted, the ONU_SSH_PASSWORD environment variable is used.'
    ),
    formatter_class=argparse.RawDescriptionHelpFormatter,
)
parser.add_argument('--device', action='append', required=True,
                    metavar='user:password@host[:port]',
                    help='ONU connection string. Repeatable for multiple ONUs. '
                         'See examples below.')
parser.add_argument('--webserver-port', type=int, default=8114,
                    help='Port for the Prometheus metrics web server')
def _validate_bind_address(s):
    """Round-trip through socket.getaddrinfo so empty / typo / non-resolvable
    bind-address values fail at parse time with a useful error, instead of
    deferring the gaierror to start_http_server time."""
    import socket  # pylint: disable=import-outside-toplevel
    try:
        socket.getaddrinfo(s, None)
    except (socket.gaierror, TypeError) as e:
        raise argparse.ArgumentTypeError(
            f"--bind-address {s!r} does not resolve: {e}. "
            "Use a literal IP (127.0.0.1, 0.0.0.0) or a hostname your system can resolve."
        ) from e
    return s


parser.add_argument('--bind-address', default='127.0.0.1', type=_validate_bind_address,
                    help='Address to bind the metrics HTTP server on. Defaults to 127.0.0.1 '
                         '(loopback only) since the typical deployment has Prometheus '
                         'scraping the same host. Use 0.0.0.0 to expose to the network '
                         '(required when running in Docker/Podman).')
parser.add_argument('--interval', type=int, default=300,
                    help='Seconds between fetches')
parser.add_argument('--enable-omci', action='store_true',
                    help=('Also probe omcicli for PON uptime, LOID auth state, and serial number. '
                          'Off by default: on at least V1.0-220923 firmware, running an omcicli '
                          'command after diag in the same fetch wedges omci_app for several '
                          'minutes and may require an SFP reboot to recover.'))
parser.add_argument('--once', action='store_true',
                    help='Run a single fetch and exit (skip the HTTP server). Useful for cron and smoke tests.')
parser.add_argument('--diagnose', action='store_true',
                    help=('Print a verbose report of connectivity, firmware, and every probe\'s '
                          'raw output. Use this output when filing a bug.'))
parser.add_argument('--log-level', default='INFO',
                    choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
                    help='Logging verbosity')
parser.add_argument('--known-hosts',
                    default=os.path.expanduser('~/.config/gpon-exporter/known_hosts'),
                    help=('Path for persisting SSH host-key fingerprints. Without '
                          'persistence, every daemon restart looks like first contact '
                          'to a fresh attacker-swapped key. The file is created on '
                          'first run with 0600 perms. Pass an empty string to disable '
                          'persistence (still logs fingerprints on every connect).'))

# Populated by init_args(); kept module-level so handlers and self-metrics
# can reference args.* without threading it through every call site. Tests
# import this module without triggering parse_args by calling init_args()
# explicitly with a synthetic argv.
args = None  # pylint: disable=invalid-name


def _parse_device(s):
    """Parse a `user:password@host[:port]` string. Password may be omitted,
    in which case ONU_SSH_PASSWORD env var is used. urlparse handles
    IPv6-bracket forms and percent-encoded passwords for free."""
    from urllib.parse import urlparse, unquote  # pylint: disable=import-outside-toplevel
    parsed = urlparse(f'ssh://{s}')
    if not parsed.hostname or not parsed.username:
        parser.error(
            f"invalid --device {s!r}; expected user:password@host[:port]. "
            "If your password contains @, :, /, or other URL-special characters, "
            "percent-encode them (e.g. p@ss -> p%40ss) or set ONU_SSH_PASSWORD "
            "instead and omit the password from --device."
        )
    pw = unquote(parsed.password) if parsed.password else os.environ.get('ONU_SSH_PASSWORD')
    if not pw:
        parser.error(f"no password for --device {s!r}; either embed it (user:password@...) "
                     "or set the ONU_SSH_PASSWORD env var")
    return parsed.hostname, parsed.port or 22, unquote(parsed.username), pw


def init_args(argv=None):
    """Parse argv and populate module-level args + PROBES. Idempotent.
    Called from main() at startup; tests call it directly with a synthetic
    argv so they don't have to fake sys.argv."""
    global args, PROBES  # pylint: disable=global-statement
    args = parser.parse_args(argv)
    devs = [_parse_device(s) for s in args.device]
    args.hostname = [d[0] for d in devs]
    args.port     = [d[1] for d in devs]
    args.user     = [d[2] for d in devs]
    args.password = [d[3] for d in devs]
    PROBES = (OMCI_PROBES if args.enable_omci else []) + DIAG_PROBES
    return args


# ----- Transceiver gauges (existing) ---------------------------------------
temperature_gauge = Gauge('gpon_temperature_celsius', 'Temperature of the GPON SFP in Celsius', ['ip'])
voltage_gauge     = Gauge('gpon_voltage_volts',       'Supply voltage of the GPON SFP in Volts', ['ip'])
tx_power_gauge    = Gauge('gpon_tx_power_dbm',        'Optical Tx power in dBm', ['ip'])
rx_power_gauge    = Gauge('gpon_rx_power_dbm',        'Optical Rx power in dBm', ['ip'])
bias_current_gauge = Gauge('gpon_bias_current_amperes',
                           'Laser bias current in amperes (Prometheus base-unit convention; '
                           'device reports mA, scaled by 1000 in the parser)', ['ip'])

# ----- ONU state and authentication -----------------------------------------
onu_state_gauge   = Gauge('gpon_onu_state',
                          'GPON ONU state (1=O1 Initial .. 5=O5 Operation .. 7=O7 Emergency Stop)',
                          ['ip'])
pon_uptime_gauge  = Gauge('gpon_pon_uptime_seconds',  'Seconds since PON authentication completed', ['ip'])
loid_auth_status_gauge  = Gauge('gpon_loid_auth_status',  'LOID authentication status code', ['ip'])
loid_auth_attempts      = Gauge('gpon_loid_auth_attempts', 'LOID authentication attempts (Auth Num)', ['ip'])
loid_auth_success       = Gauge('gpon_loid_auth_success',  'LOID authentication successes (Auth Success Num)', ['ip'])
device_info = Info('gpon_device', 'GPON device identity', ['ip'])
mac_info = Info('gpon_mac', 'MAC address of the SFP eth0 interface', ['ip'])
firmware_info = Info('gpon_firmware', 'Firmware version reported by /etc/version', ['ip'])
exporter_info = Info('gpon_exporter', 'gpon_exporter version and build identity')
exporter_info.info({'version': __version__})

# ----- Collector self-health metrics ----------------------------------------
collector_up = Gauge('gpon_exporter_up',
                     '1 if the last fetch succeeded for this device, 0 otherwise',
                     ['ip'])
collector_fetch_seconds = Gauge('gpon_exporter_fetch_seconds',
                                'Wall-clock seconds for the last fetch attempt',
                                ['ip'])
collector_last_fetch_timestamp = Gauge('gpon_exporter_last_fetch_timestamp',
                                       'Unix timestamp of the last successful fetch',
                                       ['ip'])
collector_fetch_failures = Counter('gpon_exporter_fetch_failures',
                                   'Total fetch failures since the collector started',
                                   ['ip'])

# ----- SFP system stats (from /proc on the SFP) -----------------------------
# CPU is exposed as a Counter with {ip, mode} labels, jiffies converted to
# seconds via /HZ (kernel default 100 on Realtek MIPS 2.6.30). Same shape as
# node_exporter's node_cpu_seconds_total; rate(gpon_cpu_seconds_total[5m])
# gives per-mode CPU fraction. _AbsoluteCounter only handles a single {ip}
# label, so the CPU counter is wired up manually with its own delta tracking.
sfp_cpu_seconds_total = Counter('gpon_cpu_seconds',
                                'CPU time on the SFP in seconds, by mode',
                                ['ip', 'mode'])
_sfp_cpu_last = {}  # (ip, mode) -> last absolute seconds, for delta tracking

sfp_mem_total   = Gauge('gpon_memory_total_bytes',   'Total RAM on the SFP, in bytes', ['ip'])
sfp_mem_free    = Gauge('gpon_memory_free_bytes',    'Free RAM on the SFP, in bytes', ['ip'])
sfp_mem_buffers = Gauge('gpon_memory_buffers_bytes', 'Buffer cache on the SFP, in bytes', ['ip'])
sfp_mem_cached  = Gauge('gpon_memory_cached_bytes',  'Page cache on the SFP, in bytes', ['ip'])
sfp_uptime      = Gauge('gpon_system_uptime_seconds',
                        'Seconds since the SFP booted (from /proc/uptime, distinct from '
                        'gpon_pon_uptime_seconds which is PON authentication uptime)',
                        ['ip'])

# Per-interface network counters from /proc/net/dev. Same shape as
# node_exporter's node_network_*_total. Manual delta tracking for the
# {ip, iface} label set.
sfp_net_rx_bytes   = Counter('gpon_network_receive_bytes',    'RX bytes per interface', ['ip', 'iface'])
sfp_net_rx_packets = Counter('gpon_network_receive_packets',  'RX packets per interface', ['ip', 'iface'])
sfp_net_rx_errors  = Counter('gpon_network_receive_errors',   'RX errors per interface', ['ip', 'iface'])
sfp_net_rx_dropped = Counter('gpon_network_receive_dropped',  'RX dropped per interface', ['ip', 'iface'])
sfp_net_tx_bytes   = Counter('gpon_network_transmit_bytes',   'TX bytes per interface', ['ip', 'iface'])
sfp_net_tx_packets = Counter('gpon_network_transmit_packets', 'TX packets per interface', ['ip', 'iface'])
sfp_net_tx_errors  = Counter('gpon_network_transmit_errors',  'TX errors per interface', ['ip', 'iface'])
sfp_net_tx_dropped = Counter('gpon_network_transmit_dropped', 'TX dropped per interface', ['ip', 'iface'])
_sfp_net_last = {}  # (ip, iface, metric_name) -> last absolute

# /proc/net/snmp counters (TCP/IP/UDP). Same naming convention as
# node_exporter (e.g. node_netstat_Tcp_RetransSegs); we drop the "netstat_"
# prefix and use {protocol, name} labels instead. Only the fields useful
# enough to chart for a SOHO/router audience are tracked.
_SNMP_TRACKED = {
    'Ip':  ('ForwDatagrams', 'InDelivers', 'OutRequests', 'ReasmFails'),
    'Tcp': ('ActiveOpens', 'PassiveOpens', 'AttemptFails', 'EstabResets',
            'InSegs', 'OutSegs', 'RetransSegs', 'InErrs', 'OutRsts'),
    'Udp': ('InDatagrams', 'NoPorts', 'InErrors', 'OutDatagrams',
            'RcvbufErrors', 'SndbufErrors'),
}
sfp_snmp_counter = Counter('gpon_snmp', 'Counters from /proc/net/snmp', ['ip', 'protocol', 'name'])
sfp_tcp_curr_estab = Gauge('gpon_tcp_current_established',
                           'TCP CurrEstab gauge -- connections in ESTABLISHED state', ['ip'])
_sfp_snmp_last = {}  # (ip, protocol, name) -> last absolute

# /proc/net/sockstat (gauges; current counts, not cumulative).
sfp_sockets_used = Gauge('gpon_sockets_used',
                         'Total open sockets (sockets: used N)', ['ip'])
sfp_tcp_inuse  = Gauge('gpon_tcp_sockets', 'TCP sockets by state', ['ip', 'state'])
sfp_udp_inuse  = Gauge('gpon_udp_sockets_inuse', 'UDP sockets in use', ['ip'])

# /proc/sys/net/netfilter/nf_conntrack_{count,max}
sfp_conntrack_count = Gauge('gpon_conntrack_entries',
                            'Current conntrack table entries', ['ip'])
sfp_conntrack_max   = Gauge('gpon_conntrack_max',
                            'Conntrack table size limit (nf_conntrack_max)', ['ip'])

# /proc/net/igmp -- count of multicast group memberships per interface.
# Format is one "Idx Device : Count Querier" header line per iface, then one
# group line per group joined. We count the group lines per interface.
sfp_igmp_groups = Gauge('gpon_igmp_groups',
                        'IGMP multicast groups joined, per interface', ['ip', 'iface'])

# ----- Alarms (diag gpon get alarm-status) ----------------------------------
ALARM_KEYS = {
    'LOS':         ('los',         'Loss of Signal'),
    'LOF':         ('lof',         'Loss of Frame'),
    'LOM':         ('lom',         'Loss of PLOAM'),
    'SF':          ('sf',          'Signal Fail'),
    'SD':          ('sd',          'Signal Degrade'),
    'TX Too Long': ('tx_too_long', 'TX too long'),
    'TX Mismatch': ('tx_mismatch', 'TX mismatch'),
}
ALARM_GAUGES = {
    name: Gauge(f'gpon_alarm_{key}', f'{label} alarm (1=raised, 0=clear)', ['ip'])
    for name, (key, label) in ALARM_KEYS.items()
}

# Cumulative count of clear -> raised transitions per alarm. Companion to
# the gauges above. The gauges only reflect state at scrape time, which
# means any alarm that raised AND cleared between two fetches is invisible
# (rate(metric[1h]) = 0 forever, dashboard never goes red, see QUIRKS).
# These counters preserve the raise event for any transition we observed
# at least once, so a long alarm that we did sample stays visible in 24h
# history forever after. For events shorter than --interval we still see
# nothing -- that's a firmware limitation (alarm-history not exposed by
# diag or omcicli on this firmware).
ALARM_RAISES = {
    name: Counter(f'gpon_alarm_{key}_raises',
                  f'Cumulative {label} clear->raised transitions observed', ['ip'])
    for name, (key, label) in ALARM_KEYS.items()
}
# Per-(alarm_name, ip) last observed gauge value. None == no prior
# observation, so we don't fire a transition on first contact.
_ALARM_PREV = {}

# ----- Cumulative counters --------------------------------------------------
# Device counters are absolute values (e.g. "BWMAP received: 312475"). prometheus_client's
# Counter API is .inc(delta) only, so we wrap it: parse the absolute, compute the delta
# from the previous reading, .inc() by that. Counter resets (downward jumps from device
# reboot) are detected and the baseline rebases without back-decrementing. The exposed
# metric type is `counter`, which lets Grafana/PromQL apply rate() correctly with
# automatic counter-reset handling and removes the "metric might not be a counter" hint.
class _AbsoluteCounter:  # pylint: disable=too-few-public-methods
    """Wrap prometheus_client.Counter so handlers can call .labels(ip=).set(absolute)
    the same way they would on a Gauge. The class translates absolutes into deltas
    and feeds .inc()."""
    def __init__(self, metric_name, doc):
        self._counter = Counter(metric_name, doc, ['ip'])
        self._last = {}  # tuple(label_values) -> last absolute reading

    def labels(self, **kw):
        return _AbsoluteCounterLabels(self, tuple(kw.items()))

    def _update(self, label_tuple, absolute):
        prev = self._last.get(label_tuple)
        if prev is None:
            # First contact: record the device's baseline silently and
            # materialise the labeled series at zero so /metrics shows it
            # right away. Do NOT inc by the full absolute -- that produces
            # a phantom rate() spike at every exporter restart, which is
            # exactly the bug a real Counter design is supposed to avoid.
            # rate() correctly returns 0 until the second fetch lands.
            self._counter.labels(**dict(label_tuple))
        elif absolute < prev:
            # Counter reset (device reboot). Don't try to back-decrement
            # the Prometheus counter; just rebase to the new baseline.
            pass
        else:
            self._counter.labels(**dict(label_tuple)).inc(absolute - prev)
        self._last[label_tuple] = absolute


class _AbsoluteCounterLabels:  # pylint: disable=too-few-public-methods
    def __init__(self, parent, label_tuple):
        self._parent = parent
        self._label_tuple = label_tuple

    def set(self, value):
        # Mirror Gauge.labels(...).set(N) so handlers don't change.
        self._parent._update(self._label_tuple, value)  # pylint: disable=protected-access


def _c(metric_name, help_):
    return _AbsoluteCounter(metric_name, help_)


# ----- Activation / rogue counters ------------------------------------------
# `diag gpon get pps-cnt` is reset-on-read on at least firmware V1.0-220923,
# so the value cannot be exposed as a usable Prometheus metric.
rogue_sd_too_long    = _c('gpon_rogue_sd_too_long',   'Rogue ONT SD too-long count')
rogue_sd_mismatch    = _c('gpon_rogue_sd_mismatch',   'Rogue ONT SD mismatch count')

# ----- Per-section counters from `diag gpon show counter global <section>` --
# All entries in COUNTER_MAP are cumulative device counters; exposed as Counter
# type via _c() (the _AbsoluteCounter wrapper above). prometheus_client adds the
# `_total` suffix automatically, so the on-the-wire metric names are
# `gpon_ds_bwmap_received_total`, etc.
def _g(metric_name, help_):
    """Used only for non-counter Gauges (kept for COUNTER_MAP backward
    compatibility; switch to _c for counter semantics)."""
    return Gauge(metric_name, help_, ['ip'])

COUNTER_MAP = {
    'active': {
        'SN Req':       _c('gpon_activation_sn_requests',      'Activation: serial-number requests'),
        'Ranging Req':  _c('gpon_activation_ranging_requests', 'Activation: ranging requests'),
    },
    'ds-phy': {
        'BIP Error bits':         _c('gpon_ds_bip_error_bits',           'Downstream BIP-8 error bits'),
        'BIP Error blocks':       _c('gpon_ds_bip_error_blocks',         'Downstream BIP-8 error blocks'),
        'FEC Correct bits':       _c('gpon_ds_fec_correct_bits',         'Downstream FEC corrected bits'),
        'FEC Correct bytes':      _c('gpon_ds_fec_correct_bytes',        'Downstream FEC corrected bytes'),
        'FEC Correct codewords':  _c('gpon_ds_fec_correct_codewords',    'Downstream FEC corrected codewords'),
        'FEC codewords Uncor':    _c('gpon_ds_fec_uncorrectable_codewords', 'Downstream FEC uncorrectable codewords'),
        'Superframe LOS':         _c('gpon_ds_superframe_los',           'Downstream superframe LOS events'),
        'PLEN fail':              _c('gpon_ds_plen_fail',                'Downstream PLEN check failures'),
        'PLEN correct':           _c('gpon_ds_plen_correct',             'Downstream PLEN correct'),
    },
    'ds-plm': {
        'Total RX PLOAMd':   _c('gpon_ds_ploam_received',     'Downstream PLOAMd received'),
        'CRC Err RX PLOAM':  _c('gpon_ds_ploam_crc_errors',   'Downstream PLOAMd CRC errors'),
        'Proc RX PLOAMd':    _c('gpon_ds_ploam_processed',    'Downstream PLOAMd processed'),
        'Overflow Rx PLOAM': _c('gpon_ds_ploam_overflow',     'Downstream PLOAMd overflow'),
        'Unknown Rx PLOAM':  _c('gpon_ds_ploam_unknown',      'Downstream PLOAMd unknown'),
    },
    'ds-bw': {
        'Total RX BwMap':   _c('gpon_ds_bwmap_received',     'Downstream BWMAP received'),
        'CRC Err RX BwMap': _c('gpon_ds_bwmap_crc_errors',   'Downstream BWMAP CRC errors'),
        'Overflow BwMap':   _c('gpon_ds_bwmap_overflow',     'Downstream BWMAP overflow'),
        'Invalid BwMap 0':  _c('gpon_ds_bwmap_invalid0',     'Downstream BWMAP invalid type 0'),
        'Invalid BwMap 1':  _c('gpon_ds_bwmap_invalid1',     'Downstream BWMAP invalid type 1'),
    },
    'ds-omci': {
        'Total RX OMCI':  _c('gpon_ds_omci_received',    'Downstream OMCI messages received'),
        'RX OMCI byte':   _c('gpon_ds_omci_bytes',       'Downstream OMCI bytes received'),
        'CRC Error OMCI': _c('gpon_ds_omci_crc_errors',  'Downstream OMCI CRC errors'),
        'Processed OMCI': _c('gpon_ds_omci_processed',   'Downstream OMCI messages processed'),
        'Dropped OMCI':   _c('gpon_ds_omci_dropped',     'Downstream OMCI messages dropped'),
    },
    'ds-eth': {
        'Total Unicast':   _c('gpon_ds_ethernet_unicast',          'Downstream Ethernet unicast frames'),
        'Total Multicast': _c('gpon_ds_ethernet_multicast',        'Downstream Ethernet multicast frames'),
        'Fwd Multicast':   _c('gpon_ds_ethernet_multicast_forwarded', 'Downstream Ethernet multicast forwarded'),
        'Leak Multicast':  _c('gpon_ds_ethernet_multicast_leaked', 'Downstream Ethernet multicast leaked'),
        'FCS Error':       _c('gpon_ds_ethernet_fcs_errors',       'Downstream Ethernet FCS errors'),
    },
    'ds-gem': {
        'D/S GEM LOS':      _c('gpon_ds_gem_los',                'Downstream GEM LOS events'),
        'D/S GEM Idle':     _c('gpon_ds_gem_idle',               'Downstream GEM idle frames'),
        'D/S GEM Non Idle': _c('gpon_ds_gem_non_idle',           'Downstream GEM non-idle frames'),
        'D/S HEC correct':  _c('gpon_ds_hec_correct',            'Downstream GEM HEC correct'),
        'Over Interleave':  _c('gpon_ds_gem_over_interleave',    'Downstream GEM over-interleave'),
        'Mis GEM Pkt Len':  _c('gpon_ds_gem_mis_packet_length',  'Downstream GEM mismatched packet length'),
        'Multi Flow Match': _c('gpon_ds_gem_multi_flow_match',   'Downstream GEM multi-flow-match'),
    },
    'us-phy': {
        'TX BOH': _c('gpon_us_boh', 'Upstream burst overhead'),
    },
    'us-plm': {
        'Total TX PLOAM':   _c('gpon_us_ploam_transmitted',       'Upstream PLOAM transmitted'),
        'Process TX PLOAM': _c('gpon_us_ploam_processed',         'Upstream PLOAM processed'),
        'TX Urgent PLOAM':  _c('gpon_us_ploam_urgent',            'Upstream PLOAM urgent'),
        'Proc Urg PLOAM':   _c('gpon_us_ploam_urgent_processed',  'Upstream PLOAM urgent processed'),
        'TX Normal PLOAM':  _c('gpon_us_ploam_normal',            'Upstream PLOAM normal'),
        'Proc Nrm PLOAM':   _c('gpon_us_ploam_normal_processed',  'Upstream PLOAM normal processed'),
        'TX S/N PLOAM':     _c('gpon_us_ploam_serial_number',     'Upstream PLOAM serial-number'),
        'TX NoMsg PLOAM':   _c('gpon_us_ploam_nomsg',             'Upstream PLOAM no-message'),
    },
    'us-omci': {
        'Process OMCI':  _c('gpon_us_omci_processed',   'Upstream OMCI processed'),
        'total TX OMCI': _c('gpon_us_omci_transmitted', 'Upstream OMCI transmitted'),
        'TX OMCI byte':  _c('gpon_us_omci_bytes',       'Upstream OMCI bytes transmitted'),
    },
    'us-gem': {
        'TX GEM Blocks': _c('gpon_us_gem_blocks', 'Upstream GEM blocks transmitted'),
        'TX GEM Bytes':  _c('gpon_us_gem_bytes',  'Upstream GEM bytes transmitted'),
    },
    'us-dbr': {
        'TX DBRu': _c('gpon_us_dbr', 'Upstream DBRu reports transmitted'),
    },
}


# ----- Parsers --------------------------------------------------------------
ONU_STATE_RE = re.compile(r'\(O(\d)\)')
COUNTER_LINE_RE = re.compile(r'^\s*(.+?)\s*:\s*(\d+)\s*$')
ALARM_RE = re.compile(r'Alarm (.+?), status:\s*(\S+)')


def _float(text):
    m = re.search(r'(\d+\.\d+)', text)
    return float(m.group(0)) if m else None


def _signed_float(text):
    m = re.search(r'(-?\d+\.\d+)', text)
    return float(m.group(0)) if m else None


def _ma_to_amperes(text):
    """Device reports `Bias Current: 16.55 mA`; we expose amperes per
    Prometheus base-unit convention. Scale the parsed float by 1e-3."""
    v = _float(text)
    return v * 1e-3 if v is not None else None


def simple(gauge, parser_fn):
    def handler(text, ip):
        v = parser_fn(text)
        if v is not None:
            gauge.labels(ip=ip).set(v)
    return handler


def handle_onu_state(text, ip):
    m = ONU_STATE_RE.search(text)
    if m:
        onu_state_gauge.labels(ip=ip).set(int(m.group(1)))
    else:
        # Don't let the gauge freeze at the previous value when the state
        # output is unrecognised; a stuck '5' would silently mask an outage.
        # Surface 0 as the sentinel for "unknown / not in any standard O-state".
        onu_state_gauge.labels(ip=ip).set(0)
        log.debug('onu-state output did not match O[1-7] pattern: %r', text)


def handle_alarms(text, ip):
    matches = list(ALARM_RE.finditer(text))
    if not matches:
        # Empty response or "command not found"-style output: leave gauges at
        # their last value rather than fake-clearing every alarm to 0.
        # Transition tracking also pauses -- we have no fresh observation.
        return
    # Build the new state from this scrape. Default every alarm to clear
    # so a future firmware that drops one of the lines (say, LOM) can't
    # leave a stale raised value masking an outage. Positives in the
    # response overwrite the default.
    new_state = {name: 0 for name in ALARM_KEYS}
    for m in matches:
        alarm_name = m.group(1).strip()
        status = m.group(2).strip().lower()
        if alarm_name in new_state:
            new_state[alarm_name] = 0 if status == 'clear' else 1
    # Apply the new state and increment the raises counter on every clear
    # -> raised transition we observe. First observation never fires the
    # counter -- we have no prior to compare against, and crediting an
    # already-raised alarm as a fresh raise on first contact would phantom
    # -spike rate(raises_total[15m]) at every exporter restart.
    for alarm_name, value in new_state.items():
        ALARM_GAUGES[alarm_name].labels(ip=ip).set(value)
        # Materialise the labeled Counter at 0 so /metrics shows
        # gpon_alarm_*_raises_total = 0 from first scrape, instead of
        # the series only appearing after the first transition fires.
        # Lets rate(...) return zero correctly during the warm-up window.
        raises = ALARM_RAISES[alarm_name].labels(ip=ip)
        prev = _ALARM_PREV.get((alarm_name, ip))
        if prev == 0 and value == 1:
            raises.inc()
        _ALARM_PREV[(alarm_name, ip)] = value


def handle_rogue_sd(text, ip):
    # rogue_sd_too_long and rogue_sd_mismatch are Counter type (cumulative
    # device counters). Counters are monotonic; we never set them backwards.
    # If a future firmware drops one line, leave that counter alone instead
    # of fake-zeroing it. The other line (if present) updates normally.
    too_long = re.search(r'SD too long count:\s*(\d+)', text)
    mismatch = re.search(r'SD mismatch count:\s*(\d+)', text)
    if too_long:
        rogue_sd_too_long.labels(ip=ip).set(int(too_long.group(1)))
    if mismatch:
        rogue_sd_mismatch.labels(ip=ip).set(int(mismatch.group(1)))


def handle_authuptime(text, ip):
    # Tighter than [\d.]+ -- accept exactly one optional decimal point so
    # "1.2.3 seconds" doesn't match the leading "1.2.3" and crash float().
    # Real input from V1.0-220923: "PON duration time : 138387.000000 seconds".
    m = re.search(r'PON duration time\s*:\s*(\d+(?:\.\d+)?)', text)
    if m:
        pon_uptime_gauge.labels(ip=ip).set(float(m.group(1)))


def handle_loidauth(text, ip):
    for label, gauge in (
        ('Auth Status',       loid_auth_status_gauge),
        ('Auth Num',          loid_auth_attempts),
        ('Auth Success Num',  loid_auth_success),
    ):
        m = re.search(rf'{re.escape(label)}\s*:\s*(\d+)', text)
        if m:
            gauge.labels(ip=ip).set(int(m.group(1)))


def handle_sn(text, ip):
    # Two formats this handler accepts:
    #   1. The legacy `omcicli get sn` output (`SerialNumber: DSNW...`),
    #      kept so test fixtures and older firmware paths still parse.
    #   2. The current source: `ps` output with the omci_app daemon's
    #      command line carrying `-s DSNW...`. omcicli is broken on at
    #      least V1.0-220923 (returns the MIB TOC for every verb), so we
    #      pull the serial from the daemon's argv instead. omci_app
    #      always runs with `-s <SN>` because that's how it knows which
    #      ONU identity to register with the OLT.
    m = re.search(r'SerialNumber:\s*(\S+)', text)
    if not m:
        m = re.search(r'omci_app[^\n]*\s-s\s+(\S+)', text)
    if m:
        device_info.labels(ip=ip).info({'serial_number': m.group(1)})


# Field order in the cpu line (Linux 2.6.30 / kernel docs Documentation/filesystems/proc.txt):
# user nice system idle iowait irq softirq steal guest. Steal/guest don't apply
# on the Realtek SoC (no virtualisation); we expose the seven that do.
_PROC_STAT_MODES = ('user', 'nice', 'system', 'idle', 'iowait', 'irq', 'softirq')
_HZ = 100  # kernel default on MIPS 2.6.30; matches what /proc/stat reports

def handle_proc_stat(text, ip):
    # Find the aggregate "cpu  ..." line (not per-CPU "cpu0", "cpu1" lines).
    # `\s+` not just space because the kernel pads with two spaces.
    for line in text.splitlines():
        if line.startswith('cpu '):
            parts = line.split()
            # parts[0] == 'cpu', then up to 9 jiffy counters
            for mode, raw in zip(_PROC_STAT_MODES, parts[1:1 + len(_PROC_STAT_MODES)]):
                try:
                    absolute_seconds = int(raw) / _HZ
                except ValueError:
                    continue
                key = (ip, mode)
                prev = _sfp_cpu_last.get(key)
                if prev is None:
                    # First-contact rule: silently baseline. inc()'ing the
                    # full absolute would phantom-spike rate() at every restart.
                    sfp_cpu_seconds_total.labels(ip=ip, mode=mode)
                elif absolute_seconds < prev:
                    pass  # SFP rebooted; rebase
                else:
                    sfp_cpu_seconds_total.labels(ip=ip, mode=mode).inc(absolute_seconds - prev)
                _sfp_cpu_last[key] = absolute_seconds
            return


_MEMINFO_FIELDS = {
    'MemTotal':  sfp_mem_total,
    'MemFree':   sfp_mem_free,
    'Buffers':   sfp_mem_buffers,
    'Cached':    sfp_mem_cached,
}

def handle_proc_meminfo(text, ip):
    # Lines look like: "MemTotal:          27528 kB". Always kB on this kernel.
    for line in text.splitlines():
        m = re.match(r'(\w+):\s*(\d+)\s*kB', line)
        if not m:
            continue
        gauge = _MEMINFO_FIELDS.get(m.group(1))
        if gauge:
            gauge.labels(ip=ip).set(int(m.group(2)) * 1024)


def handle_proc_uptime(text, ip):
    # /proc/uptime: "<uptime_s> <idle_s>" -- two floats separated by space.
    # Bound the decimal so "1.2.3 ..." can't crash float() (defensive --
    # the kernel format is fixed, but parser robustness is cheap).
    m = re.match(r'\s*(\d+(?:\.\d+)?)', text)
    if m:
        sfp_uptime.labels(ip=ip).set(float(m.group(1)))


# eth0 is the LAN-side physical interface; br0 inherits the same MAC. PON-side
# interfaces (pon0, nas0, eth0.{2,3}) carry placeholder MACs and are skipped.
# Strict 6-octet MAC shape -- the previous [0-9a-fA-F:]{17} also accepted
# things like ":::::::::::::::::" or 17 hex chars with no colons.
_MAC_RE = re.compile(r'^((?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2})\s*$')

def handle_eth0_mac(text, ip):
    m = _MAC_RE.search(text.strip())
    if m:
        mac_info.labels(ip=ip).info({'mac': m.group(1).lower()})


# /proc/net/dev columns after `iface:` -- this is the standard kernel format
# (Linux 2.6+; Documentation/filesystems/proc.txt). 16 fields total, 8 RX + 8 TX.
_PROC_NET_DEV_FIELDS = [
    ('rx_bytes',   sfp_net_rx_bytes),
    ('rx_packets', sfp_net_rx_packets),
    ('rx_errs',    sfp_net_rx_errors),
    ('rx_drop',    sfp_net_rx_dropped),
    None, None, None, None,  # rx_fifo, rx_frame, rx_compressed, rx_multicast (skipped)
    ('tx_bytes',   sfp_net_tx_bytes),
    ('tx_packets', sfp_net_tx_packets),
    ('tx_errs',    sfp_net_tx_errors),
    ('tx_drop',    sfp_net_tx_dropped),
]

def handle_proc_net_dev(text, ip):
    for line in text.splitlines():
        if ':' not in line:
            continue  # header rows
        iface, _, rest = line.partition(':')
        iface = iface.strip()
        cols = rest.split()
        if len(cols) < 16:
            continue
        for i, field in enumerate(_PROC_NET_DEV_FIELDS):
            if field is None:
                continue
            name, counter = field
            try:
                absolute = int(cols[i])
            except ValueError:
                continue
            key = (ip, iface, name)
            prev = _sfp_net_last.get(key)
            if prev is None:
                counter.labels(ip=ip, iface=iface)  # materialise at zero
            elif absolute < prev:
                pass  # SFP rebooted or interface counters reset; rebase
            else:
                counter.labels(ip=ip, iface=iface).inc(absolute - prev)
            _sfp_net_last[key] = absolute


def handle_proc_net_snmp(text, ip):
    """Pairs of lines: header (`Tcp: name1 name2 ...`) then values
    (`Tcp: v1 v2 ...`). Tcp.CurrEstab is special-cased as a Gauge because
    it's a current-value metric, not a cumulative one."""
    lines = text.splitlines()
    headers = {}  # protocol -> [field names]
    for line in lines:
        proto, _, rest = line.partition(': ')
        if not rest:
            continue
        toks = rest.split()
        # First sighting of a protocol is its header line (alphabetic tokens);
        # the next sighting is its value line (numeric tokens).
        if proto not in headers:
            headers[proto] = toks
            continue
        # Values line.
        values = dict(zip(headers[proto], toks))
        # CurrEstab is a Gauge; everything else tracked is a Counter.
        if proto == 'Tcp' and 'CurrEstab' in values:
            try:
                sfp_tcp_curr_estab.labels(ip=ip).set(int(values['CurrEstab']))
            except ValueError:
                pass
        for name in _SNMP_TRACKED.get(proto, ()):
            if name not in values:
                continue
            try:
                absolute = int(values[name])
            except ValueError:
                continue
            key = (ip, proto, name)
            prev = _sfp_snmp_last.get(key)
            if prev is None:
                sfp_snmp_counter.labels(ip=ip, protocol=proto, name=name)
            elif absolute < prev:
                pass  # rebase on reboot
            else:
                sfp_snmp_counter.labels(ip=ip, protocol=proto, name=name).inc(absolute - prev)
            _sfp_snmp_last[key] = absolute
        del headers[proto]  # ready for the next pair if it shows up again


_SOCKSTAT_RE = re.compile(r'(\w+)\s+(\d+)')

def handle_proc_net_sockstat(text, ip):
    """Lines like 'TCP: inuse 4 orphan 0 tw 10 alloc 4 mem 1' --
    key/value pairs after the protocol name."""
    for line in text.splitlines():
        proto, _, rest = line.partition(': ')
        if not rest:
            continue
        pairs = dict(_SOCKSTAT_RE.findall(rest))
        if proto == 'sockets' and 'used' in pairs:
            sfp_sockets_used.labels(ip=ip).set(int(pairs['used']))
        elif proto == 'TCP':
            for state in ('inuse', 'orphan', 'tw', 'alloc'):
                if state in pairs:
                    sfp_tcp_inuse.labels(ip=ip, state=state).set(int(pairs[state]))
        elif proto == 'UDP' and 'inuse' in pairs:
            sfp_udp_inuse.labels(ip=ip).set(int(pairs['inuse']))


def handle_conntrack(text, ip):
    """`cat <count> <max>` returns two integers on consecutive lines."""
    nums = re.findall(r'^\s*(\d+)\s*$', text, re.MULTILINE)
    if len(nums) >= 2:
        sfp_conntrack_count.labels(ip=ip).set(int(nums[0]))
        sfp_conntrack_max.labels(ip=ip).set(int(nums[1]))


def handle_proc_net_igmp(text, ip):
    """Count group lines per interface. Header lines start with a digit
    (Idx) and contain the iface name; group lines are indented and contain
    a hex address pattern. Counting group lines per iface gives the
    number of multicast groups joined."""
    current = None
    counts = {}
    for line in text.splitlines():
        # Header line: starts with index digit, then iface, then ":"
        m = re.match(r'^\d+\s+(\S+)\s+:', line)
        if m:
            current = m.group(1)
            counts.setdefault(current, 0)
            continue
        # Group line: leading whitespace then hex group address
        if current and re.match(r'^\s+[0-9A-Fa-f]{8}\s', line):
            counts[current] += 1
    for iface, n in counts.items():
        sfp_igmp_groups.labels(ip=ip, iface=iface).set(n)


def handle_firmware(text, ip):
    # /etc/version line looks like: "V1.0-220923 --  Fri Sep 23 19:36:10 CST 2022"
    # Reject BusyBox-style error output (e.g. "cat: can't open ...",
    # "sh: not found") which would otherwise land as firmware_info{version="cat:"}.
    # We check for a colon in the first whitespace-delimited token: real
    # firmware version strings on every observed build use alphanumerics,
    # dots, dashes, and underscores; never a colon. Looser than the original
    # ^V\d match so untested M-prefix or unprefixed builds still get through.
    parts = text.split(None, 1)
    if not parts or ':' in parts[0]:
        return
    firmware_info.labels(ip=ip).info({'version': parts[0]})


def counter_block(section):
    mapping = COUNTER_MAP[section]

    def handler(text, ip):
        for line in text.splitlines():
            m = COUNTER_LINE_RE.match(line)
            if not m:
                continue
            gauge = mapping.get(m.group(1).strip())
            if gauge:
                gauge.labels(ip=ip).set(int(m.group(2)))
    return handler


# ----- Probe table ----------------------------------------------------------
# OMCI probes go first when enabled: running diag before omcicli wedges
# omci_app on at least firmware V1.0-220923, but the reverse order is safe
# because diag does not depend on the OMCI daemon.
OMCI_PROBES = [
    ('authuptime',   'omcicli get authuptime',                handle_authuptime),
    ('loidauth',     'omcicli get loidauth',                  handle_loidauth),
]

DIAG_PROBES = [
    ('firmware',     'cat /etc/version',                      handle_firmware),
    ('proc_stat',    'cat /proc/stat',                        handle_proc_stat),
    ('proc_meminfo', 'cat /proc/meminfo',                     handle_proc_meminfo),
    ('proc_uptime',  'cat /proc/uptime',                      handle_proc_uptime),
    ('eth0_mac',     'cat /sys/class/net/eth0/address',       handle_eth0_mac),
    ('net_dev',      'cat /proc/net/dev',                     handle_proc_net_dev),
    ('net_snmp',     'cat /proc/net/snmp',                    handle_proc_net_snmp),
    ('net_sockstat', 'cat /proc/net/sockstat',                handle_proc_net_sockstat),
    ('conntrack',    'cat /proc/sys/net/netfilter/nf_conntrack_count '
                     '/proc/sys/net/netfilter/nf_conntrack_max',  handle_conntrack),
    ('net_igmp',     'cat /proc/net/igmp',                    handle_proc_net_igmp),
    ('sn',           'ps',                                    handle_sn),
    ('bias_current', 'diag pon get transceiver bias-current',
        simple(bias_current_gauge, _ma_to_amperes)),
    ('rx_power',     'diag pon get transceiver rx-power',     simple(rx_power_gauge,    _signed_float)),
    # Industrial-temp SFPs run down to -40C, so use the signed parser even though
    # the typical reading is positive. Otherwise sub-zero readings parse as positive.
    ('temperature',  'diag pon get transceiver temperature',  simple(temperature_gauge, _signed_float)),
    ('tx_power',     'diag pon get transceiver tx-power',     simple(tx_power_gauge,    _signed_float)),
    ('voltage',      'diag pon get transceiver voltage',      simple(voltage_gauge,     _float)),
    ('onu_state',    'diag gpon get onu-state',               handle_onu_state),
    ('alarms',       'diag gpon get alarm-status',            handle_alarms),
    ('rogue_sd',     'diag gpon get rogue-sd-cnt',            handle_rogue_sd),
] + [
    (section.replace('-', '_'), f'diag gpon show counter global {section}', counter_block(section))
    for section in COUNTER_MAP
]

# Populated by init_args(); see top of file.
PROBES = []


def _fetch_budget():
    """Wall-clock budget for one fetch_and_update_metrics_via_ssh call.
    Half the configured interval, with a 30s floor so a legitimately
    slow first fetch on --once doesn't trip the watchdog. Exposed as a
    function so tests can monkeypatch a small value without changing
    the production default."""
    interval = getattr(args, 'interval', 300) if args else 300
    return max(30, interval // 2)


def fetch_and_update_metrics_via_ssh(hostname, port, username, password):
    client = paramiko.SSHClient()
    # Watchdog: paramiko's exec_command(timeout=10) is per-recv inactivity,
    # not a total read budget. A remote that trickles a byte every 9s
    # would let stdout.read() block forever. The wedge condition documented
    # in QUIRKS (omcicli stalls forever on broken verbs) is the realistic
    # version of this. Bound the entire fetch with a Timer that closes the
    # client from another thread; the in-progress paramiko read then
    # raises rather than hanging, and the outer fetch_all_once treats it
    # as a transient failure.
    fired = threading.Event()

    def _watchdog():
        fired.set()
        try:
            client.close()
        except Exception:  # pylint: disable=broad-exception-caught
            pass  # nothing useful to do if close() itself fails

    timer = threading.Timer(_fetch_budget(), _watchdog)
    timer.daemon = True
    timer.start()
    try:
        client.set_missing_host_key_policy(_LoggingHostKeyPolicy(args.known_hosts or None))
        client.connect(hostname, port=port, username=username, password=password,
                       look_for_keys=False, allow_agent=False, timeout=10)
        # SSH-level keepalive every 15s. Catches the link-dead-but-TCP-half-open
        # case quickly: if the SFP stops responding entirely, paramiko's read
        # raises within ~30s instead of waiting on TCP keep-alive timeouts
        # (which are minutes by default).
        transport = client.get_transport()
        if transport is not None:
            transport.set_keepalive(15)
        # One SSH connection per fetch, one channel per probe. We tried stitching
        # every probe into a single exec_command, but on this firmware diag leaves
        # something attached after returning, so the next omcicli in the same
        # shell wedges. A fresh channel per probe sidesteps that without paying
        # the cost of a new TCP/auth handshake.
        for _, cmd, handler in PROBES:
            _, stdout, _ = client.exec_command(cmd, timeout=10)
            text = stdout.read().decode(errors='replace')
            handler(text, hostname)
    finally:
        timer.cancel()
        # Close even if connect() raised; paramiko handles the never-opened case.
        client.close()
    if fired.is_set():
        # Loop completed but the watchdog still fired during it -- some
        # probe(s) may have been cut short. Surface as a transient failure
        # rather than silently returning partial data.
        raise paramiko.SSHException(
            f'fetch wall-clock budget ({_fetch_budget()}s) exceeded; '
            'some probes may have been truncated'
        )


def explain_exception(e, host):  # pylint: disable=too-many-return-statements
    """Translate the most common SSH failure modes into something a user can
    act on without having to read paramiko internals."""
    name = type(e).__name__
    msg = str(e) or '(no message)'
    if isinstance(e, paramiko.AuthenticationException):
        return f'auth failed (wrong credentials in --device, or {host} locked the account out)'
    if isinstance(e, paramiko.BadHostKeyException):
        return f'host key changed for {host} (clear ~/.ssh/known_hosts if expected)'
    # The hint below is matched against paramiko's exception message text, which
    # is fragile by nature. If a future paramiko release renames these strings
    # the user just falls through to the bare class-name path — annoying, not
    # broken. Add new wordings here as they show up in the wild.
    channel_close_markers = ('Channel closed', 'Connection closed', 'EOF received')
    if isinstance(e, paramiko.SSHException) and any(m in msg for m in channel_close_markers):
        return (f'channel closed by {host} mid-fetch. Another SSH session may hold '
                f'the slot, or omci_app is wedged (see --enable-omci caveat in README)')
    if isinstance(e, OSError):
        # Covers ConnectionRefusedError, socket.timeout, "No route to host", etc.
        if 'refused' in msg.lower():
            return f'connection refused at {host} (is sshd enabled on the SFP?)'
        if 'timed out' in msg.lower() or 'timeout' in msg.lower():
            return f'connect timed out at {host} (check route, IP, and link-up)'
        if 'unreachable' in msg.lower():
            return f'{host} unreachable (check network)'
    return f'{name}: {msg}'


# Exceptions that mean "credentials or host identity are wrong and
# retrying with the same parameters will not help, only burn auth attempts
# at the SFP". Treated as fatal in fetch_all_once so the daemon exits and
# systemd's StartLimitBurst/IntervalSec guard fires as documented in
# odi.service.
_FATAL_FETCH_EXC = (
    paramiko.AuthenticationException,
    paramiko.BadHostKeyException,
)


def fetch_all_once():
    """Fetch metrics from every configured device, once.

    Multi-device contract: a fatal credential failure on one device must
    NOT abort the loop -- subsequent devices still get a fair fetch in
    the same cycle (their creds may be fine). Fatal failures accumulate
    and only after the loop completes does the function exit non-zero
    if any fired.

    Returns the count of TRANSIENT failures (for callers like --once).
    Raises SystemExit(<fatal_count>) if any device hit a fatal failure.
    """
    transient = 0
    fatal = 0
    for i, host in enumerate(args.hostname):
        start = time.monotonic()
        try:
            try:
                fetch_and_update_metrics_via_ssh(
                    host, args.port[i], args.user[i], args.password[i],
                )
            except _FATAL_FETCH_EXC:
                # Don't retry. Re-raise so the outer handler classifies this
                # as fatal rather than as a transient failure.
                raise
            except Exception as first_exc:  # pylint: disable=broad-exception-caught
                # One transient SSH hiccup shouldn't flatline the host for the
                # whole interval. Sleep briefly and retry once before declaring
                # the device down. A second failure is a real failure.
                log.info('fetch retry for %s after: %s',
                         host, explain_exception(first_exc, host))
                time.sleep(5)
                fetch_and_update_metrics_via_ssh(
                    host, args.port[i], args.user[i], args.password[i],
                )
        except _FATAL_FETCH_EXC as e:
            collector_up.labels(ip=host).set(0)
            collector_fetch_failures.labels(ip=host).inc()
            collector_fetch_seconds.labels(ip=host).set(time.monotonic() - start)
            log.error('fatal: %s -- exporter will exit at end of cycle',
                      explain_exception(e, host))
            fatal += 1
        except Exception as e:  # pylint: disable=broad-exception-caught
            collector_up.labels(ip=host).set(0)
            collector_fetch_failures.labels(ip=host).inc()
            collector_fetch_seconds.labels(ip=host).set(time.monotonic() - start)
            log.warning('fetch failed for %s after retry: %s',
                        host, explain_exception(e, host))
            transient += 1
        else:
            now = time.time()
            collector_up.labels(ip=host).set(1)
            collector_last_fetch_timestamp.labels(ip=host).set(now)
            collector_fetch_seconds.labels(ip=host).set(time.monotonic() - start)
            log.info('fetch ok for %s in %.2fs', host, time.monotonic() - start)
    if fatal:
        log.error('exiting due to %d fatal credential/host-key failure(s); '
                  'fix and restart the unit', fatal)
        sys.exit(fatal)
    return transient


_DIAG_REDACT_RE = re.compile(
    r'(\w*(?:PASSWORD|PASSWD|SECRET|KEY|TOKEN))(\s*[=:]\s*)(\S+)',
    re.IGNORECASE,
)


def _scrub(line):
    """Replace anything that looks like a secret-bearing field in
    diagnose output. Catches *_PASSWORD=..., *_PASSWD=..., *_KEY=...,
    *_SECRET=..., *_TOKEN=... and the colon-separated variants. The
    field name is preserved so the user can see WHICH field was
    redacted in their bug report.

    Defence in depth: the standard probe set today doesn't read files
    that contain credentials (no `mib show`, no `cat /var/config/
    lastgood.xml`), so this is mostly an insurance policy against future
    probes that scrape config-file output. Doesn't change normal probe
    paths -- only the human-readable --diagnose dump."""
    return _DIAG_REDACT_RE.sub(r'\1\2<REDACTED>', line)


def diagnose():
    """Run a verbose one-shot probe of every device and print a copy-pasteable
    report. Designed to be the first thing a user runs before filing an issue."""
    if not args.enable_omci:
        print('note: omcicli probes (authuptime, loidauth, sn) are skipped because')
        print('      --enable-omci is off. Re-run with --enable-omci to include them,')
        print('      but be aware it can wedge omci_app on some firmwares.\n')
    failures = 0
    for i, host in enumerate(args.hostname):
        print(f'=== {host}:{args.port[i]} (user={args.user[i]}) ===')
        client = paramiko.SSHClient()
        try:
            client.set_missing_host_key_policy(_LoggingHostKeyPolicy(args.known_hosts or None))
            try:
                client.connect(host, port=args.port[i], username=args.user[i],
                               password=args.password[i], look_for_keys=False,
                               allow_agent=False, timeout=10)
                print('connect: ok')
            except Exception as e:  # pylint: disable=broad-exception-caught
                print(f'connect: FAILED. {explain_exception(e, host)}')
                failures += 1
                continue

            for key, cmd, _ in PROBES:
                start = time.monotonic()
                try:
                    _, stdout, _ = client.exec_command(cmd, timeout=10)
                    text = stdout.read().decode(errors='replace')
                    elapsed_ms = (time.monotonic() - start) * 1000
                    nlines = text.count('\n')
                    nchars = len(text)
                    print(f'\n[{key}] cmd={cmd!r} took={elapsed_ms:.0f}ms bytes={nchars} lines={nlines}')
                    for line in text.splitlines()[:20]:
                        print(f'  | {_scrub(line)}')
                    if nlines > 20:
                        print(f'  | ... {nlines - 20} more lines')
                except Exception as e:  # pylint: disable=broad-exception-caught
                    print(f'\n[{key}] FAILED. {explain_exception(e, host)}')
                    failures += 1
        finally:
            client.close()
        print()
    print('=== summary ===')
    if failures:
        print(f'{failures} probe(s) failed; see output above')
    else:
        print('all probes succeeded')
    return failures


def main():
    if args is None:
        init_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    )
    # Paramiko's INFO line per fetch is too chatty; we'll surface real failures via WARNING.
    logging.getLogger('paramiko').setLevel(logging.WARNING)
    # prometheus_client only materialises a labeled series the first time you
    # touch the label, so pre-touch the per-host self-metrics. Otherwise a
    # healthy collector with zero failures shows "no data" on the failure-rate
    # panel instead of a flat zero, and the same trap on last_fetch_timestamp
    # before the first successful fetch lands.
    for host in args.hostname:
        collector_up.labels(ip=host)
        collector_fetch_seconds.labels(ip=host)
        collector_last_fetch_timestamp.labels(ip=host)
        collector_fetch_failures.labels(ip=host)
    if args.diagnose:
        return diagnose()
    if args.once:
        return fetch_all_once()
    start_http_server(args.webserver_port, addr=args.bind_address)
    # Compensate for fetch wall-clock so the cycle period is exactly
    # --interval seconds instead of (interval + fetch_duration). Without this
    # the schedule drifts forward by ~3 s every 5 minutes.
    while True:
        cycle_start = time.monotonic()
        fetch_all_once()
        elapsed = time.monotonic() - cycle_start
        time.sleep(max(0, args.interval - elapsed))


if __name__ == "__main__":
    raise SystemExit(main())
