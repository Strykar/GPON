"""Integration tests for fetch_and_update_metrics_via_ssh.

These mock paramiko's SSHClient so we can verify the per-channel exec loop,
the order of commands, and exception handling without touching the network.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from prometheus_client import REGISTRY  # noqa: E402

import paramiko  # noqa: E402
import gpon_exporter as c  # noqa: E402


class FakeStdout:
    def __init__(self, data=''):
        self.data = data

    def read(self):
        return self.data.encode()


class FakeClient:
    """Records every exec_command call. Configurable canned responses keyed by
    the exact command string; everything else returns empty output."""

    def __init__(self, responses=None, fail_on_connect=None,
                 fail_on_exec=None):
        self.calls = []
        self.connected = False
        self.closed = False
        self.responses = responses or {}
        self.fail_on_connect = fail_on_connect
        self.fail_on_exec = fail_on_exec

    def set_missing_host_key_policy(self, _policy):
        pass

    def connect(self, *_args, **_kwargs):
        if self.fail_on_connect is not None:
            raise self.fail_on_connect
        self.connected = True

    def exec_command(self, cmd, timeout=None):  # pylint: disable=unused-argument
        self.calls.append(cmd)
        if self.fail_on_exec is not None:
            raise self.fail_on_exec
        return None, FakeStdout(self.responses.get(cmd, '')), None

    def close(self):
        self.closed = True


def install_fake(monkeypatch, **kwargs):
    fake = FakeClient(**kwargs)
    monkeypatch.setattr(c.paramiko, 'SSHClient', lambda: fake)
    return fake


def test_fetch_iterates_every_probe_in_order(monkeypatch):
    """Every PROBES entry must reach exec_command exactly once, in order."""
    fake = install_fake(monkeypatch)
    c.fetch_and_update_metrics_via_ssh('1.2.3.4', 22, 'admin', 'pw')
    expected = [cmd for _, cmd, _ in c.PROBES]
    assert fake.calls == expected
    assert fake.connected
    assert fake.closed


def test_fetch_closes_client_even_on_exec_failure(monkeypatch):
    """If a per-probe exec_command blows up mid-fetch we still close the
    SSHClient. Otherwise we'd leak a paramiko connection."""
    boom = paramiko.SSHException('Channel closed.')
    fake = install_fake(monkeypatch, fail_on_exec=boom)
    with pytest.raises(paramiko.SSHException):
        c.fetch_and_update_metrics_via_ssh('1.2.3.4', 22, 'admin', 'pw')
    assert fake.closed


def test_fetch_propagates_authentication_error(monkeypatch):
    install_fake(monkeypatch,
                 fail_on_connect=paramiko.AuthenticationException('bad'))
    with pytest.raises(paramiko.AuthenticationException):
        c.fetch_and_update_metrics_via_ssh('1.2.3.4', 22, 'admin', 'pw')


def test_fetch_all_once_exits_on_authentication_failure(monkeypatch):
    """C2 regression-locker. Previously the retry-once logic caught
    AuthenticationException via `except Exception` and re-attempted the
    same bad credentials, doubling the failed-auth rate against the SFP.
    Combined with --interval 300 this hammered the device 24 times/hour
    on a wrong password -- exactly the case the systemd StartLimitBurst
    guard claims to prevent but never could because the daemon never
    crashed.

    Fix: AuthenticationException is fatal, fetch_all_once exits with a
    non-zero code, systemd's StartLimit* guard fires as designed."""
    install_fake(monkeypatch,
                 fail_on_connect=paramiko.AuthenticationException('bad'))
    monkeypatch.setattr(c, 'args', _stub_args(['10.0.0.1']))
    with pytest.raises(SystemExit) as exc:
        c.fetch_all_once()
    assert exc.value.code != 0


def test_fetch_all_once_exits_on_bad_host_key(monkeypatch):
    """BadHostKeyException is also fatal -- replaying against the SFP
    doesn't help, and after the C1 fix a key change isn't auto-trusted.
    Defensive: nothing in the codebase populates client.get_host_keys()
    before fetch today, so this is unreachable in practice. The fatal
    classification is future-proofing in case a load_system_host_keys()
    ever appears."""
    class FakeKey:
        def get_base64(self):
            return 'AAAA'
    bhke = paramiko.BadHostKeyException('h', FakeKey(), FakeKey())
    install_fake(monkeypatch, fail_on_connect=bhke)
    monkeypatch.setattr(c, 'args', _stub_args(['10.0.0.1']))
    with pytest.raises(SystemExit) as exc:
        c.fetch_all_once()
    assert exc.value.code != 0


def test_fetch_all_once_completes_remaining_devices_then_exits(monkeypatch):
    """Multi-device contract: device with bad credentials must NOT cause
    later devices in the cycle to be skipped. Collect fatal failures
    across all devices, exit only after the loop. This way device 2
    still gets a fair fetch and a valid up=1 if its creds are good."""
    # Two devices, only the first one's connect raises auth failure.
    # We need a fake that distinguishes by hostname.
    class SelectiveFake(FakeClient):
        def connect(self, hostname, **kwargs):
            if hostname == '10.0.0.1':
                raise paramiko.AuthenticationException('bad')
            self.connected = True

    monkeypatch.setattr(c.paramiko, 'SSHClient', lambda: SelectiveFake())
    monkeypatch.setattr(c, 'args', _stub_args(['10.0.0.1', '10.0.0.2']))
    with pytest.raises(SystemExit) as exc:
        c.fetch_all_once()
    assert exc.value.code != 0
    # Device 2 must have been attempted (its up label series exists)
    assert REGISTRY.get_sample_value('gpon_exporter_up', {'ip': '10.0.0.2'}) is not None


def _stub_args(hostnames):
    """Minimal args stub for fetch_all_once -- it reads .hostname/.port/
    .user/.password as parallel arrays plus .known_hosts for the policy."""
    class A:
        pass
    a = A()
    a.hostname    = hostnames
    a.port        = [22] * len(hostnames)
    a.user        = ['admin'] * len(hostnames)
    a.password    = ['pw'] * len(hostnames)
    a.known_hosts = ''  # disables persistence; the FakeClient ignores it anyway
    return a


def test_fetch_updates_gauges_from_canned_output(monkeypatch):
    """End-to-end: feed real diag output for ds-phy through the pipeline twice
    (baseline-then-real, so the Counter advances by the absolute as a delta
    instead of being silently absorbed on first contact) and verify the
    BIP/FEC counters land at the right values plus the Tx gauge."""
    baseline = {
        'diag pon get transceiver tx-power': 'Tx Power: 1.500000  dBm',
        'diag gpon show counter global ds-phy': (
            'BIP Error bits  : 0\nBIP Error blocks: 0\n'
            'FEC Correct codewords: 0\nFEC codewords Uncor: 0\n'
        ),
    }
    canned = {
        'diag pon get transceiver tx-power': 'Tx Power: 1.500000  dBm',
        'diag gpon show counter global ds-phy': (
            'BIP Error bits  : 7\n'
            'BIP Error blocks: 2\n'
            'FEC Correct codewords: 99\n'
            'FEC codewords Uncor: 3\n'
        ),
    }
    ip = '10.0.0.99'

    install_fake(monkeypatch, responses=baseline)
    c.fetch_and_update_metrics_via_ssh(ip, 22, 'admin', 'pw')
    install_fake(monkeypatch, responses=canned)
    c.fetch_and_update_metrics_via_ssh(ip, 22, 'admin', 'pw')

    def value(name):
        return REGISTRY.get_sample_value(name, {'ip': ip})

    assert value('gpon_tx_power_dbm') == 1.5
    assert value('gpon_ds_bip_error_bits_total') == 7
    assert value('gpon_ds_bip_error_blocks_total') == 2
    assert value('gpon_ds_fec_correct_codewords_total') == 99
    assert value('gpon_ds_fec_uncorrectable_codewords_total') == 3


def test_explain_exception_maps_common_failures():
    """The user-facing log explanations must hit the actionable hints, not
    fall through to bare class names."""

    class FakeKey:
        def get_base64(self):
            return 'AAAA'

    cases = [
        (paramiko.AuthenticationException('Authentication failed.'), 'auth failed'),
        (paramiko.BadHostKeyException('h', FakeKey(), FakeKey()), 'host key changed'),
        (paramiko.SSHException('Channel closed.'), 'channel closed'),
        # The other channel-closed wordings paramiko has used or might use:
        (paramiko.SSHException('Connection closed by peer.'), 'channel closed'),
        (paramiko.SSHException('EOF received.'), 'channel closed'),
        (ConnectionRefusedError(111, 'Connection refused'), 'connection refused'),
        (TimeoutError('connect timed out'), 'timed out'),
    ]
    for exc, needle in cases:
        out = c.explain_exception(exc, '1.2.3.4').lower()
        assert needle in out, f'expected {needle!r} in {out!r}'


def test_explain_exception_falls_back_to_class_name():
    """Unrecognised exceptions should still produce something legible."""

    class Weird(Exception):
        pass

    out = c.explain_exception(Weird('unknown'), '1.2.3.4')
    assert 'Weird' in out
    assert 'unknown' in out


def test_bind_address_default_is_loopback():
    """Default is 127.0.0.1 (loopback). Docker deployments override to
    0.0.0.0 via the compose command. Empty-string default was tried once
    and raised gaierror at start_http_server time, so the value must always
    resolve through socket.getaddrinfo."""
    assert c.args.bind_address == '127.0.0.1'
