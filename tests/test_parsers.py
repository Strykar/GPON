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


def test_rogue_sd_two_counters():
    """First contact populates both counters with the device's running totals."""
    handler = next(h for k, _, h in c.PROBES if k == 'rogue_sd')
    # Use a unique IP so the per-test Counter starts fresh (Counters are
    # monotonic; we can't reset them between tests sharing one label set).
    rogue_ip = '10.0.99.1'
    def rv(name):
        return REGISTRY.get_sample_value(name, {'ip': rogue_ip})
    handler('SD too long count: 3\n\nSD mismatch count: 7', rogue_ip)
    assert rv('gpon_rogue_sd_too_long_total') == 3
    assert rv('gpon_rogue_sd_mismatch_total') == 7


def test_ds_phy_block_parses_bip_and_fec():
    """The /stats.asp parity check: BIP-8 errors, FEC corrected/uncorrectable
    codewords, and superframe LOS all live under 'show counter global ds-phy'."""
    handler = next(h for k, _, h in c.PROBES if k == 'ds_phy')
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
    handler(text, IP)
    assert value('gpon_ds_bip_error_bits_total') == 4
    assert value('gpon_ds_bip_error_blocks_total') == 1
    assert value('gpon_ds_fec_correct_bytes_total') == 100
    assert value('gpon_ds_fec_correct_codewords_total') == 5
    assert value('gpon_ds_fec_uncorrectable_codewords_total') == 2
    assert value('gpon_ds_plen_correct_total') == 999


def test_ds_plm_block():
    handler = next(h for k, _, h in c.PROBES if k == 'ds_plm')
    text = (
        'Total RX PLOAMd    : 78020\n'
        'CRC Err RX PLOAM   : 0\n'
        'Proc RX PLOAMd     : 12484\n'
        'Overflow Rx PLOAM  : 0\n'
        'Unknown Rx PLOAM   : 0\n'
    )
    handler(text, IP)
    assert value('gpon_ds_ploam_received_total') == 78020
    assert value('gpon_ds_ploam_processed_total') == 12484


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


def test_serial_number_info_metric():
    handler = next(h for k, _, h in c.PROBES if k == 'sn')
    handler('SerialNumber: DSNW282D5510', IP)
    assert value('gpon_device_info', serial_number='DSNW282D5510') == 1.0


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
    backwards). The matched line updates as normal; the missing one stays."""
    handler = next(h for k, _, h in c.PROBES if k == 'rogue_sd')
    rogue_ip = '10.0.99.2'
    def rv(name):
        return REGISTRY.get_sample_value(name, {'ip': rogue_ip})
    handler('SD too long count: 5\n\nSD mismatch count: 7', rogue_ip)
    assert rv('gpon_rogue_sd_too_long_total') == 5
    assert rv('gpon_rogue_sd_mismatch_total') == 7
    # Subsequent fetch: too_long incremented to 8, mismatch line missing.
    handler('SD too long count: 8', rogue_ip)
    assert rv('gpon_rogue_sd_too_long_total') == 8   # advanced
    assert rv('gpon_rogue_sd_mismatch_total') == 7   # untouched


def test_rogue_sd_preserves_counters_on_unparseable_input():
    handler = next(h for k, _, h in c.PROBES if k == 'rogue_sd')
    rogue_ip = '10.0.99.3'
    def rv(name):
        return REGISTRY.get_sample_value(name, {'ip': rogue_ip})
    handler('SD too long count: 9\n\nSD mismatch count: 11', rogue_ip)
    assert rv('gpon_rogue_sd_too_long_total') == 9
    handler('', rogue_ip)
    assert rv('gpon_rogue_sd_too_long_total') == 9


def test_handler_tolerates_garbage_input():
    """Every handler must no-op (not raise) on unrecognised input. Lets us keep
    the daemon running across firmware reshuffles instead of crashing on the
    first surprise line."""
    for _, _, handler in c.PROBES:
        handler('completely unrelated noise', IP)
        handler('', IP)
