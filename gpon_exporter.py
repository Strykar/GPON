import argparse
import logging
import os
import re
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
            log.warning('first SSH connect to %s; trusting host key %s %s', hostname, kt, fp)
        elif prev != (kt, fp):
            log.warning('SSH host key for %s changed: was %s %s, now %s %s',
                        hostname, prev[0], prev[1], kt, fp)
        self._known[hostname] = (kt, fp)
        self._save()
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
parser.add_argument('--bind-address', default='127.0.0.1',
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

args = parser.parse_args()


def _parse_device(s):
    """Parse a `user:password@host[:port]` string. Password may be omitted,
    in which case ONU_SSH_PASSWORD env var is used. urlparse handles
    IPv6-bracket forms and percent-encoded passwords for free."""
    from urllib.parse import urlparse, unquote  # pylint: disable=import-outside-toplevel
    parsed = urlparse(f'ssh://{s}')
    if not parsed.hostname or not parsed.username:
        parser.error(f"invalid --device {s!r}; expected user:password@host[:port]")
    pw = unquote(parsed.password) if parsed.password else os.environ.get('ONU_SSH_PASSWORD')
    if not pw:
        parser.error(f"no password for --device {s!r}; either embed it (user:password@...) "
                     "or set the ONU_SSH_PASSWORD env var")
    return parsed.hostname, parsed.port or 22, unquote(parsed.username), pw


# Expand --device strings into the parallel arrays the rest of the code uses.
_devs = [_parse_device(s) for s in args.device]
args.hostname = [d[0] for d in _devs]
args.port     = [d[1] for d in _devs]
args.user     = [d[2] for d in _devs]
args.password = [d[3] for d in _devs]


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
            # First contact: increment by the full absolute so the exposed
            # value matches the device's running counter from t=0.
            self._counter.labels(**dict(label_tuple)).inc(absolute)
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
        return
    # Got at least one parseable line: pre-reset all alarm gauges so a future
    # firmware that drops one of the lines (say, LOM) can't leave a stale
    # raised value masking an outage. Positives below overwrite the zero.
    for gauge in ALARM_GAUGES.values():
        gauge.labels(ip=ip).set(0)
    for m in matches:
        alarm_name = m.group(1).strip()
        status = m.group(2).strip().lower()
        gauge = ALARM_GAUGES.get(alarm_name)
        if gauge:
            gauge.labels(ip=ip).set(0 if status == 'clear' else 1)


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
    m = re.search(r'PON duration time\s*:\s*([\d.]+)', text)
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
    m = re.search(r'SerialNumber:\s*(\S+)', text)
    if m:
        device_info.labels(ip=ip).info({'serial_number': m.group(1)})


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
    ('sn',           'omcicli get sn',                        handle_sn),
]

DIAG_PROBES = [
    ('firmware',     'cat /etc/version',                      handle_firmware),
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

PROBES = (OMCI_PROBES if args.enable_omci else []) + DIAG_PROBES

def fetch_and_update_metrics_via_ssh(hostname, port, username, password):
    client = paramiko.SSHClient()
    try:
        client.set_missing_host_key_policy(_LoggingHostKeyPolicy(args.known_hosts or None))
        client.connect(hostname, port=port, username=username, password=password,
                       look_for_keys=False, allow_agent=False, timeout=10)
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
        # Close even if connect() raised; paramiko handles the never-opened case.
        client.close()


def explain_exception(e, host):  # pylint: disable=too-many-return-statements
    """Translate the most common SSH failure modes into something a user can
    act on without having to read paramiko internals."""
    name = type(e).__name__
    msg = str(e) or '(no message)'
    if isinstance(e, paramiko.AuthenticationException):
        return f'auth failed (wrong --user/--password, or {host} locked the account out)'
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


def fetch_all_once():
    failures = 0
    for i, host in enumerate(args.hostname):
        start = time.monotonic()
        try:
            fetch_and_update_metrics_via_ssh(
                host, args.port[i], args.user[i], args.password[i],
            )
        except Exception as e:  # pylint: disable=broad-exception-caught
            # Transient SSH/parse failures shouldn't take the daemon down; the next
            # tick will retry and Prometheus will plot a gap rather than a flatline.
            collector_up.labels(ip=host).set(0)
            collector_fetch_failures.labels(ip=host).inc()
            collector_fetch_seconds.labels(ip=host).set(time.monotonic() - start)
            log.warning('fetch failed for %s: %s', host, explain_exception(e, host))
            failures += 1
        else:
            now = time.time()
            collector_up.labels(ip=host).set(1)
            collector_last_fetch_timestamp.labels(ip=host).set(now)
            collector_fetch_seconds.labels(ip=host).set(time.monotonic() - start)
            log.info('fetch ok for %s in %.2fs', host, time.monotonic() - start)
    return failures


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
                        print(f'  | {line}')
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
