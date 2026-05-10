"""Parser unit tests using real `diag` and `omcicli` outputs captured from a
live HSGQ SFP running firmware V1.0-220923. Each handler updates a Gauge that
we read back via prometheus_client's public registry API.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from prometheus_client import REGISTRY  # noqa: E402

import gpon_exporter as c  # noqa: E402


IP = '10.0.0.1'  # distinct from any real host so tests don't collide


def value(metric_name, **labels):
    """Return the current value of a Gauge sample, or None if absent."""
    full_labels = {'ip': IP, **labels}
    return REGISTRY.get_sample_value(metric_name, full_labels)


def test_bias_current_in_amperes():
    """Device reports mA; we scale to amperes (Prometheus base unit) and
    rename the gauge accordingly. 16.75 mA -> 0.01675 A."""
    handler = next(h for k, _, h in c.PROBES if k == 'bias_current')
    handler('Bias Current: 16.750000 mA', IP)
    assert value('gpon_bias_current_amperes') == 0.01675


def test_tx_power_handles_signed_decimals():
    handler = next(h for k, _, h in c.PROBES if k == 'tx_power')
    handler('Tx Power: 2.239385  dBm', IP)
    assert value('gpon_tx_power_dbm') == 2.239385


def test_rx_power_handles_negative():
    handler = next(h for k, _, h in c.PROBES if k == 'rx_power')
    handler('Rx Power: -27.212461  dBm', IP)
    assert value('gpon_rx_power_dbm') == -27.212461


def test_temperature():
    handler = next(h for k, _, h in c.PROBES if k == 'temperature')
    handler('Temperature: 42.359375 C', IP)
    assert value('gpon_temperature_celsius') == 42.359375


def test_temperature_handles_negative():
    """Industrial-temp SFPs run down to -40 C. The temperature parser must
    keep the sign; an earlier version used the unsigned float regex and
    silently dropped the minus, reporting -5 C as +5."""
    handler = next(h for k, _, h in c.PROBES if k == 'temperature')
    handler('Temperature: -5.000000 C', IP)
    assert value('gpon_temperature_celsius') == -5.0


def test_voltage():
    handler = next(h for k, _, h in c.PROBES if k == 'voltage')
    handler('Voltage: 3.232700 V', IP)
    assert value('gpon_voltage_volts') == 3.2327


def test_onu_state_extracts_O_paren_form():
    """ONU state output is 'ONU state: Operation State(O5)'. The O-prefixed
    digit is what we want, not the human-readable phrase."""
    handler = next(h for k, _, h in c.PROBES if k == 'onu_state')
    handler('ONU state: Operation State(O5)', IP)
    assert value('gpon_onu_state') == 5

    handler('ONU state: Standby State(O2)', IP)
    assert value('gpon_onu_state') == 2


def test_onu_state_unrecognised_format_falls_back_to_zero():
    """If a future firmware reformats the ONU state output past our regex,
    the gauge must drop to 0 (sentinel) rather than freezing at the last
    valid reading. A stuck '5' would silently mask an outage."""
    handler = next(h for k, _, h in c.PROBES if k == 'onu_state')
    handler('ONU state: Operation State(O5)', IP)
    assert value('gpon_onu_state') == 5
    handler('ONU state: Some Weird New Wording', IP)
    assert value('gpon_onu_state') == 0


def test_alarms_clear_and_raised():
    handler = next(h for k, _, h in c.PROBES if k == 'alarms')
    text = (
        'Alarm LOS, status: clear\n'
        'Alarm LOF, status: clear\n'
        'Alarm LOM, status: clear\n'
        'Alarm SF, status: clear\n'
        'Alarm SD, status: raised\n'
        'Alarm TX Too Long, status: clear\n'
        'Alarm TX Mismatch, status: clear\n'
    )
    handler(text, IP)
    assert value('gpon_alarm_los') == 0
    assert value('gpon_alarm_lof') == 0
    assert value('gpon_alarm_sd') == 1
    assert value('gpon_alarm_tx_too_long') == 0
    assert value('gpon_alarm_tx_mismatch') == 0


def test_first_contact_does_not_spike_counter():
    """Regression guard for the v1.0.0 first-contact spike: recording the
    device's running absolute as a Counter increment on first contact
    produced a phantom rate() spike at every exporter restart. The fix
    materialises the labeled series at zero, records the baseline silently,
    and only increments on subsequent fetches' deltas."""
    handler = next(h for k, _, h in c.PROBES if k == 'rogue_sd')
    rogue_ip = '10.0.99.1'
    def rv(name):
        return REGISTRY.get_sample_value(name, {'ip': rogue_ip})
    # Device's running totals are huge -- realistic for a long-uptime SFP.
    handler('SD too long count: 100000\nSD mismatch count: 250000', rogue_ip)
    # First contact: series exists but value is zero. rate() correctly
    # returns zero until the second fetch produces a real delta.
    assert rv('gpon_rogue_sd_too_long_total') == 0, \
        'first contact must NOT inc by the absolute (would phantom-spike rate())'
    assert rv('gpon_rogue_sd_mismatch_total') == 0
    # Second fetch: deltas of 3 and 7 land on the Prometheus counter.
    handler('SD too long count: 100003\nSD mismatch count: 250007', rogue_ip)
    assert rv('gpon_rogue_sd_too_long_total') == 3
    assert rv('gpon_rogue_sd_mismatch_total') == 7


def test_ds_phy_block_parses_bip_and_fec():
    """The /stats.asp parity check: BIP-8 errors, FEC corrected/uncorrectable
    codewords, and superframe LOS all live under 'show counter global ds-phy'.
    Uses a unique IP and a baseline-first call so the absolute values land on
    the Counter as deltas-from-zero (see test_first_contact_does_not_spike_counter
    for the why)."""
    handler = next(h for k, _, h in c.PROBES if k == 'ds_phy')
    test_ip = '10.0.91.1'
    def v(name):
        return REGISTRY.get_sample_value(name, {'ip': test_ip})
    handler(
        'gpon show counter global ds-phy\n'
        'BIP Error bits  : 0\nBIP Error blocks: 0\n'
        'FEC Correct bits: 0\nFEC Correct bytes: 0\n'
        'FEC Correct codewords: 0\nFEC codewords Uncor: 0\n'
        'Superframe LOS  : 0\nPLEN fail       : 0\nPLEN correct    : 0\n',
        test_ip,
    )
    text = (
        'gpon show counter global ds-phy\n'
        '============================================================\n'
        '     GPON ONU MAC Device Counter: DS PHY\n'
        'BIP Error bits  : 4\n'
        'BIP Error blocks: 1\n'
        'FEC Correct bits: 0\n'
        'FEC Correct bytes: 100\n'
        'FEC Correct codewords: 5\n'
        'FEC codewords Uncor: 2\n'
        'Superframe LOS  : 0\n'
        'PLEN fail       : 0\n'
        'PLEN correct    : 999\n'
        '============================================================\n'
    )
    handler(text, test_ip)
    assert v('gpon_ds_bip_error_bits_total') == 4
    assert v('gpon_ds_bip_error_blocks_total') == 1
    assert v('gpon_ds_fec_correct_bytes_total') == 100
    assert v('gpon_ds_fec_correct_codewords_total') == 5
    assert v('gpon_ds_fec_uncorrectable_codewords_total') == 2
    assert v('gpon_ds_plen_correct_total') == 999


def test_ds_plm_block():
    handler = next(h for k, _, h in c.PROBES if k == 'ds_plm')
    test_ip = '10.0.91.2'
    def v(name):
        return REGISTRY.get_sample_value(name, {'ip': test_ip})
    handler(
        'Total RX PLOAMd    : 0\nCRC Err RX PLOAM   : 0\n'
        'Proc RX PLOAMd     : 0\nOverflow Rx PLOAM  : 0\n'
        'Unknown Rx PLOAM   : 0\n',
        test_ip,
    )
    text = (
        'Total RX PLOAMd    : 78020\n'
        'CRC Err RX PLOAM   : 0\n'
        'Proc RX PLOAMd     : 12484\n'
        'Overflow Rx PLOAM  : 0\n'
        'Unknown Rx PLOAM   : 0\n'
    )
    handler(text, test_ip)
    assert v('gpon_ds_ploam_received_total') == 78020
    assert v('gpon_ds_ploam_processed_total') == 12484


def test_authuptime():
    handler = next(h for k, _, h in c.PROBES if k == 'authuptime')
    handler('PON duration time : 138387.000000 seconds', IP)
    assert value('gpon_pon_uptime_seconds') == 138387.0


def test_loidauth_three_fields():
    handler = next(h for k, _, h in c.PROBES if k == 'loidauth')
    handler('Auth Status : 1\nAuth Num : 5\nAuth Success Num : 4', IP)
    assert value('gpon_loid_auth_status') == 1
    assert value('gpon_loid_auth_attempts') == 5
    assert value('gpon_loid_auth_success') == 4


def test_serial_number_from_ps_omci_app_argv():
    """Real source on V1.0-220923: omci_app's command line carries the SN
    in `-s <SN>`. Pulled from `ps` because the documented `omcicli get sn`
    is broken on this firmware (returns the MIB TOC for every verb)."""
    handler = next(h for k, _, h in c.PROBES if k == 'sn')
    handler(
        '  321 admin    22616 S    omci_app -s DSNW282D5510 -f off 0 -m enable_wq -d err\n'
        '  477 admin    22616 S    omci_app -s DSNW282D5510 -f off 0 -m enable_wq -d err\n',
        IP,
    )
    assert value('gpon_device_info', serial_number='DSNW282D5510') == 1.0


def test_serial_number_legacy_omcicli_format_still_parses():
    """Kept for forward-compat: if a future firmware fixes omcicli, the
    handler still recognises the historical 'SerialNumber: XYZ' shape."""
    handler = next(h for k, _, h in c.PROBES if k == 'sn')
    handler('SerialNumber: HWTC1234ABCD', IP)
    assert value('gpon_device_info', serial_number='HWTC1234ABCD') == 1.0


def test_firmware_handles_empty_text():
    """An empty /etc/version response (e.g. the cat command output dropped
    entirely) must not raise. The Info metric just stays at whatever it was."""
    handler = next(h for k, _, h in c.PROBES if k == 'firmware')
    handler('', IP)        # must not raise
    handler('   ', IP)     # whitespace-only must not raise either


def test_firmware_rejects_busybox_error():
    """BusyBox cat printing 'cat: can't open /etc/version: ...' must not land
    in Prometheus as firmware_info{version="cat:"}. The handler rejects any
    first-token that contains a colon, on the basis that real firmware
    versions don't have colons but error prefixes always do."""
    handler = next(h for k, _, h in c.PROBES if k == 'firmware')
    handler('V1.0-220923 -- Fri Sep 23 19:36:10 CST 2022', IP)
    assert value('gpon_firmware_info', version='V1.0-220923') == 1.0
    handler("cat: can't open '/etc/version': No such file or directory", IP)
    assert value('gpon_firmware_info', version='V1.0-220923') == 1.0
    handler('sh: not found', IP)
    assert value('gpon_firmware_info', version='V1.0-220923') == 1.0


def test_firmware_accepts_real_version_strings():
    """Every observed firmware (extracted from each tarball's fwu_ver):
    V1.0-220923, V1.7.1-231021, V1.1.8-240408, V1.1.4-250620. Plus a couple
    of hypothetical alternative formats so a future M-prefix or unprefixed
    firmware doesn't get silently dropped by an over-tight validator."""
    handler = next(h for k, _, h in c.PROBES if k == 'firmware')
    for ver in (
        'V1.0-220923', 'V1.7.1-231021', 'V1.1.8-240408', 'V1.1.4-250620',
        'M1.0-something', '1.1.3-250620',
    ):
        handler(f'{ver} -- some date', IP)
        assert value('gpon_firmware_info', version=ver) == 1.0, f'rejected {ver!r}'


def test_alarms_clear_to_raised_to_clear_transition():
    """The full lifecycle: alarm raised, then cleared, must take the gauge
    back to 0. With the pre-reset fix, this works because each fetch with
    a parseable response zeroes everything before applying positives."""
    handler = next(h for k, _, h in c.PROBES if k == 'alarms')
    raised = (
        'Alarm LOS, status: clear\n'
        'Alarm LOF, status: clear\n'
        'Alarm LOM, status: clear\n'
        'Alarm SF, status: raised\n'
        'Alarm SD, status: clear\n'
        'Alarm TX Too Long, status: clear\n'
        'Alarm TX Mismatch, status: clear\n'
    )
    cleared = raised.replace('Alarm SF, status: raised', 'Alarm SF, status: clear')
    handler(raised, IP)
    assert value('gpon_alarm_sf') == 1
    handler(cleared, IP)
    assert value('gpon_alarm_sf') == 0


def test_alarms_preserve_value_on_unparseable_input():
    """If the response has no parseable alarm lines (empty, error, command
    not found), the handler must leave existing gauges alone. Pre-resetting
    in this case would fake a 'clear all' reading and silently mask outages
    that started during the bad fetch."""
    handler = next(h for k, _, h in c.PROBES if k == 'alarms')
    # Establish a raised LOS
    handler('Alarm LOS, status: raised\n'
            'Alarm LOF, status: clear\n'
            'Alarm LOM, status: clear\n'
            'Alarm SF, status: clear\n'
            'Alarm SD, status: clear\n'
            'Alarm TX Too Long, status: clear\n'
            'Alarm TX Mismatch, status: clear\n', IP)
    assert value('gpon_alarm_los') == 1
    # Unparseable response: no Alarm lines at all
    handler('', IP)
    assert value('gpon_alarm_los') == 1, 'empty response must not fake-clear'
    handler("sh: command not found", IP)
    assert value('gpon_alarm_los') == 1, 'error response must not fake-clear'


def test_rogue_sd_partial_input_leaves_other_counter_alone():
    """If a future firmware drops one of the two rogue-SD lines, the missing
    counter must not be touched (Counters are monotonic; can't fake-zero
    backwards). The matched line advances as normal; the missing one stays."""
    handler = next(h for k, _, h in c.PROBES if k == 'rogue_sd')
    rogue_ip = '10.0.99.2'
    def rv(name):
        return REGISTRY.get_sample_value(name, {'ip': rogue_ip})
    # Baseline (silent), then a fetch that lands deltas of 5 and 7.
    handler('SD too long count: 0\nSD mismatch count: 0', rogue_ip)
    handler('SD too long count: 5\nSD mismatch count: 7', rogue_ip)
    assert rv('gpon_rogue_sd_too_long_total') == 5
    assert rv('gpon_rogue_sd_mismatch_total') == 7
    # Subsequent fetch: too_long advances to 8 (delta of 3), mismatch line missing.
    handler('SD too long count: 8', rogue_ip)
    assert rv('gpon_rogue_sd_too_long_total') == 8   # advanced
    assert rv('gpon_rogue_sd_mismatch_total') == 7   # untouched


def test_rogue_sd_preserves_counters_on_unparseable_input():
    handler = next(h for k, _, h in c.PROBES if k == 'rogue_sd')
    rogue_ip = '10.0.99.3'
    def rv(name):
        return REGISTRY.get_sample_value(name, {'ip': rogue_ip})
    # Baseline + one real fetch -> counter advances by 9.
    handler('SD too long count: 0\nSD mismatch count: 0', rogue_ip)
    handler('SD too long count: 9\nSD mismatch count: 11', rogue_ip)
    assert rv('gpon_rogue_sd_too_long_total') == 9
    handler('', rogue_ip)
    assert rv('gpon_rogue_sd_too_long_total') == 9


def test_authuptime_rejects_multi_dot_garbage():
    """M1 regression-locker. Previous regex [\\d.]+ was greedy across dots
    so '1.2.3 seconds' matched '1.2.3' and float() raised ValueError,
    which propagated up and short-circuited the rest of the fetch."""
    handler = next(h for k, _, h in c.PROBES if k == 'authuptime')
    handler('PON duration time : 1.2.3 seconds', IP)  # must not raise
    handler('PON duration time : ..... seconds', IP)  # must not raise either


def test_proc_uptime_rejects_multi_dot_garbage():
    """M1: same fix on /proc/uptime parser. Defensive -- the kernel
    format is fixed -- but cheap."""
    handler = next(h for k, _, h in c.PROBES if k == 'proc_uptime')
    handler('1.2.3 0.0', IP)  # must not raise


def test_eth0_mac_rejects_obvious_garbage():
    """M2 regression-locker. Previous regex [0-9a-fA-F:]{17} matched
    ':::::::::::::::::' and 17 hex chars with no colons. Strict 6-octet
    shape rejects both -- the gpon_mac_info series stays at whatever it
    was last set to (or unset entirely if never set)."""
    handler = next(h for k, _, h in c.PROBES if k == 'eth0_mac')
    test_ip = '10.0.96.1'
    handler(':::::::::::::::::\n', test_ip)  # 17 colons, no hex
    handler('00000000000000000\n', test_ip)  # 17 hex, no colons
    handler('zzzzz\n', test_ip)
    handler('', test_ip)
    # No mac_info series should have materialised for test_ip
    assert REGISTRY.get_sample_value('gpon_mac_info', {'ip': test_ip, 'mac': ':::::::::::::::::'}) is None
    # Real MAC still works
    handler('aa:bb:cc:dd:ee:ff\n', test_ip)
    assert REGISTRY.get_sample_value('gpon_mac_info', {'ip': test_ip, 'mac': 'aa:bb:cc:dd:ee:ff'}) == 1.0


def test_bind_address_validator_rejects_garbage_at_parse_time():
    """M4: validator catches typos and unresolvable hostnames during
    argparse instead of letting start_http_server raise gaierror later."""
    import argparse as _argparse
    with __import__('pytest').raises(_argparse.ArgumentTypeError):
        c._validate_bind_address('this.host.absolutely.does.not.exist.example')
    # Real values pass through unchanged
    assert c._validate_bind_address('127.0.0.1') == '127.0.0.1'
    assert c._validate_bind_address('0.0.0.0') == '0.0.0.0'


def test_handler_tolerates_garbage_input():
    """Every handler must no-op (not raise) on unrecognised input. Lets us keep
    the daemon running across firmware reshuffles instead of crashing on the
    first surprise line."""
    for _, _, handler in c.PROBES:
        handler('completely unrelated noise', IP)
        handler('', IP)


def test_proc_meminfo_parses_kb_to_bytes():
    """/proc/meminfo lines are 'Field: NNNN kB'. We translate to bytes (×1024)
    and only expose the four fields the dashboard formula needs (Total, Free,
    Buffers, Cached)."""
    handler = next(h for k, _, h in c.PROBES if k == 'proc_meminfo')
    test_ip = '10.0.92.1'
    def v(name):
        return REGISTRY.get_sample_value(name, {'ip': test_ip})
    handler(
        'MemTotal:          27528 kB\n'
        'MemFree:            1748 kB\n'
        'Buffers:            1800 kB\n'
        'Cached:             6384 kB\n'
        'Active:             7100 kB\n',  # extra field that should be ignored
        test_ip,
    )
    assert v('gpon_memory_total_bytes') == 27528 * 1024
    assert v('gpon_memory_free_bytes')  == 1748 * 1024
    assert v('gpon_memory_buffers_bytes') == 1800 * 1024
    assert v('gpon_memory_cached_bytes')  == 6384 * 1024


def test_eth0_mac_extracts_address():
    """/sys/class/net/eth0/address is one line, '24:43:e2:2d:55:10\\n'.
    The handler lowercases and exposes via the gpon_mac Info metric."""
    handler = next(h for k, _, h in c.PROBES if k == 'eth0_mac')
    test_ip = '10.0.93.1'
    handler('24:43:E2:2D:55:10\n', test_ip)
    assert REGISTRY.get_sample_value('gpon_mac_info',
        {'ip': test_ip, 'mac': '24:43:e2:2d:55:10'}) == 1.0


def test_proc_uptime_parses_first_float():
    """/proc/uptime: '<uptime_s> <idle_s>'. We expose only the first."""
    handler = next(h for k, _, h in c.PROBES if k == 'proc_uptime')
    test_ip = '10.0.92.2'
    handler('301991.29 284530.09\n', test_ip)
    assert REGISTRY.get_sample_value('gpon_system_uptime_seconds',
                                     {'ip': test_ip}) == 301991.29


def test_proc_stat_skips_first_contact_then_increments():
    """The cpu jiffy counters land on a Counter via manual delta tracking.
    First contact baselines silently (no phantom rate-spike on restart);
    second fetch advances by the per-mode delta."""
    handler = next(h for k, _, h in c.PROBES if k == 'proc_stat')
    test_ip = '10.0.92.3'
    def v(mode):
        return REGISTRY.get_sample_value('gpon_cpu_seconds_total',
                                         {'ip': test_ip, 'mode': mode})
    # First snapshot: device has run for a while, big absolute jiffies.
    handler('cpu  100000 0 50000 9000000 5 0 200 0 0\n'
            'cpu0 100000 0 50000 9000000 5 0 200 0 0\n'
            'intr 12345\n', test_ip)
    # All modes should be at zero (baseline absorbed).
    for mode in ('user', 'system', 'idle', 'iowait', 'softirq'):
        assert v(mode) == 0, f'{mode} must not jump on first contact'
    # Second snapshot: each mode advanced by a known delta in jiffies.
    handler('cpu  100100 0 50050 9000900 5 0 200 0 0\n', test_ip)
    # 100 jiffies / HZ=100 = 1 second; 50 jiffies = 0.5s; 900 jiffies = 9s.
    assert v('user')   == 1.0
    assert v('system') == 0.5
    assert v('idle')   == 9.0
    assert v('iowait') == 0.0  # unchanged
    assert v('softirq') == 0.0


def test_proc_net_dev_parses_eth0_counters():
    """/proc/net/dev's standard 16-column format. Two-step baseline-then-real
    so the absolutes land as deltas-from-zero on the per-{ip,iface} Counters."""
    handler = next(h for k, _, h in c.PROBES if k == 'net_dev')
    test_ip = '10.0.94.1'
    def v(name, iface):
        return REGISTRY.get_sample_value(name, {'ip': test_ip, 'iface': iface})
    header = ('Inter-|   Receive                                                |  Transmit\n'
              ' face |bytes    packets errs drop fifo frame compressed multicast'
              '|bytes    packets errs drop fifo colls carrier compressed\n')
    handler(header +
            '  eth0:       0       0    0    0    0     0          0         0'
            '        0       0    0    0    0     0       0          0\n', test_ip)
    handler(header +
            '  eth0:1000  100    2    3    0    0    0    0  '
            '500   50   1   4    0    0    0    0\n', test_ip)
    assert v('gpon_network_receive_bytes_total',    'eth0') == 1000
    assert v('gpon_network_receive_packets_total',  'eth0') == 100
    assert v('gpon_network_receive_errors_total',   'eth0') == 2
    assert v('gpon_network_receive_dropped_total',  'eth0') == 3
    assert v('gpon_network_transmit_bytes_total',   'eth0') == 500
    assert v('gpon_network_transmit_packets_total', 'eth0') == 50
    assert v('gpon_network_transmit_errors_total',  'eth0') == 1
    assert v('gpon_network_transmit_dropped_total', 'eth0') == 4


def test_proc_net_dev_handles_multiple_interfaces():
    """Each iface gets its own series; the SFP exposes lo, eth0, br0, pon0,
    nas0, eth0.2, eth0.3 -- iface name with a dot is valid."""
    handler = next(h for k, _, h in c.PROBES if k == 'net_dev')
    test_ip = '10.0.94.2'
    def v(name, iface):
        return REGISTRY.get_sample_value(name, {'ip': test_ip, 'iface': iface})
    text = (
        'Inter-|   Receive                                                |  Transmit\n'
        ' face |bytes    packets errs drop fifo frame compressed multicast'
        '|bytes    packets errs drop fifo colls carrier compressed\n'
        '  eth0: 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n'
        '   br0: 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n'
        'eth0.2: 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n'
    )
    handler(text, test_ip)
    handler(text.replace(
        '  eth0: 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0',
        '  eth0: 7 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0',
    ).replace(
        '   br0: 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0',
        '   br0: 9 2 0 0 0 0 0 0 0 0 0 0 0 0 0 0',
    ).replace(
        'eth0.2: 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0',
        'eth0.2: 0 0 0 0 0 0 0 0 11 3 0 0 0 0 0 0',
    ), test_ip)
    assert v('gpon_network_receive_bytes_total', 'eth0') == 7
    assert v('gpon_network_receive_bytes_total', 'br0')  == 9
    assert v('gpon_network_transmit_bytes_total', 'eth0.2') == 11


def test_proc_net_snmp_parses_tcp_and_udp():
    """/proc/net/snmp comes as alternating header/value pairs per protocol.
    Tcp.CurrEstab is special-cased as a Gauge; tracked Counters land
    deltas-from-zero after the baseline call."""
    handler = next(h for k, _, h in c.PROBES if k == 'net_snmp')
    test_ip = '10.0.95.1'
    def v_counter(proto, name):
        return REGISTRY.get_sample_value('gpon_snmp_total',
            {'ip': test_ip, 'protocol': proto, 'name': name})
    def v_estab():
        return REGISTRY.get_sample_value('gpon_tcp_current_established', {'ip': test_ip})
    baseline = (
        'Tcp: RtoAlgorithm RtoMin RtoMax MaxConn ActiveOpens PassiveOpens '
        'AttemptFails EstabResets CurrEstab InSegs OutSegs RetransSegs InErrs OutRsts\n'
        'Tcp: 1 200 120000 -1 0 0 0 0 0 0 0 0 0 0\n'
        'Udp: InDatagrams NoPorts InErrors OutDatagrams RcvbufErrors SndbufErrors\n'
        'Udp: 0 0 0 0 0 0\n'
    )
    handler(baseline, test_ip)
    real = (
        'Tcp: RtoAlgorithm RtoMin RtoMax MaxConn ActiveOpens PassiveOpens '
        'AttemptFails EstabResets CurrEstab InSegs OutSegs RetransSegs InErrs OutRsts\n'
        'Tcp: 1 200 120000 -1 5 1541 0 652 17 941540 1759675 88 0 450\n'
        'Udp: InDatagrams NoPorts InErrors OutDatagrams RcvbufErrors SndbufErrors\n'
        'Udp: 0 4989 0 0 0 0\n'
    )
    handler(real, test_ip)
    assert v_estab() == 17, 'CurrEstab should be a gauge, not a Counter'
    assert v_counter('Tcp', 'ActiveOpens')   == 5
    assert v_counter('Tcp', 'PassiveOpens')  == 1541
    assert v_counter('Tcp', 'RetransSegs')   == 88
    assert v_counter('Udp', 'NoPorts')       == 4989


def test_proc_net_sockstat_parses_tcp_states():
    handler = next(h for k, _, h in c.PROBES if k == 'net_sockstat')
    test_ip = '10.0.95.2'
    def v(name, **labels):
        return REGISTRY.get_sample_value(name, {'ip': test_ip, **labels})
    handler(
        'sockets: used 23\n'
        'TCP: inuse 4 orphan 0 tw 10 alloc 4 mem 1\n'
        'UDP: inuse 1 mem 0\n', test_ip)
    assert v('gpon_sockets_used') == 23
    assert v('gpon_tcp_sockets', state='inuse')  == 4
    assert v('gpon_tcp_sockets', state='tw')     == 10
    assert v('gpon_tcp_sockets', state='orphan') == 0
    assert v('gpon_udp_sockets_inuse') == 1


def test_conntrack_parses_count_and_max():
    handler = next(h for k, _, h in c.PROBES if k == 'conntrack')
    test_ip = '10.0.95.3'
    def v(name):
        return REGISTRY.get_sample_value(name, {'ip': test_ip})
    handler('20\n1916\n', test_ip)
    assert v('gpon_conntrack_entries') == 20
    assert v('gpon_conntrack_max') == 1916


def test_proc_net_igmp_counts_groups_per_interface():
    """Header lines look like `1\\teth0      :     1      V3` then
    indented group lines `\\t\\t\\tE0000001     1 0:00000000\\t\\t0`. We
    count group lines per iface."""
    handler = next(h for k, _, h in c.PROBES if k == 'net_igmp')
    test_ip = '10.0.95.4'
    def v(iface):
        return REGISTRY.get_sample_value('gpon_igmp_groups',
            {'ip': test_ip, 'iface': iface})
    handler(
        'Idx\tDevice    : Count Querier\tGroup    Users Timer\tReporter\n'
        '1\teth0      :     2      V3\n'
        '\t\t\tE0000001     1 0:00000000\t\t0\n'
        '\t\t\tEFFFFFFA     1 0:00000000\t\t0\n'
        '2\tbr0       :     1      V2\n'
        '\t\t\tE0000001     1 0:00000000\t\t0\n', test_ip)
    assert v('eth0') == 2
    assert v('br0') == 1


def test_proc_stat_handles_per_cpu_lines_after_aggregate():
    """The aggregate 'cpu ' line comes first. The handler must take only that
    one and ignore 'cpu0', 'cpu1' etc. so a future SMP firmware doesn't
    double-count."""
    handler = next(h for k, _, h in c.PROBES if k == 'proc_stat')
    test_ip = '10.0.92.4'
    def v(mode):
        return REGISTRY.get_sample_value('gpon_cpu_seconds_total',
                                         {'ip': test_ip, 'mode': mode})
    # baseline
    handler('cpu  0 0 0 0 0 0 0 0 0\ncpu0 0 0 0 0 0 0 0 0 0\n', test_ip)
    # advance the aggregate by 100 user-jiffies (=1s); per-cpu line shows
    # garbage that would tag along if we matched 'cpu' loosely.
    handler('cpu  100 0 0 0 0 0 0 0 0\n'
            'cpu0 999999 0 0 0 0 0 0 0 0\n', test_ip)
    assert v('user') == 1.0, 'per-CPU line must not be summed in'
