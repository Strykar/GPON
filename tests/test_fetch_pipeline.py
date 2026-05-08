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


def test_fetch_updates_gauges_from_canned_output(monkeypatch):
    """End-to-end: feed real diag output for ds-phy through the pipeline and
    verify the BIP/FEC gauges land at the right values."""
    canned = {
        'diag pon get transceiver tx-power': 'Tx Power: 1.500000  dBm',
        'diag gpon show counter global ds-phy': (
            'BIP Error bits  : 7\n'
            'BIP Error blocks: 2\n'
            'FEC Correct codewords: 99\n'
            'FEC codewords Uncor: 3\n'
        ),
    }
    install_fake(monkeypatch, responses=canned)
    ip = '10.0.0.99'
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
