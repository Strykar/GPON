"""Tests for _LoggingHostKeyPolicy.

Regression locker for the C1 issue: previously the policy persisted the
new fingerprint on the key-change branch unconditionally. A single
transient swap silently became the trusted record; subsequent connects
saw `prev == (attacker_kt, attacker_fp)` and emitted no warning.

The fix restricts persistence to the first-contact branch. Key-change
warnings stay durable until the operator hand-edits the known-hosts
file. These tests pin both behaviours.
"""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gpon_exporter as c  # noqa: E402


class _FakeKey:
    """Stand-in for paramiko.PKey -- we only need get_name() and
    get_fingerprint() for the policy."""
    def __init__(self, name, fp_bytes):
        self._name = name
        self._fp = fp_bytes

    def get_name(self):
        return self._name

    def get_fingerprint(self):
        return self._fp


class _FakeClient:
    """Stand-in for paramiko.SSHClient. The policy only calls
    client.get_host_keys().add(host, kt, key); we capture the calls so a
    test could assert against them if needed."""
    def __init__(self):
        self.added = []

    def get_host_keys(self):
        return self

    def add(self, host, kt, key):
        self.added.append((host, kt, key))


LEGIT  = _FakeKey('ssh-rsa', b'\x01' * 8)
ATTACK = _FakeKey('ssh-rsa', b'\x02' * 8)


def _read_store(path):
    with open(path, encoding='utf-8') as f:
        return [line.rstrip('\n') for line in f if line.strip()]


# --------------------------------------------------------------------------
# C1 fix: persistence only on first contact
# --------------------------------------------------------------------------

def test_first_contact_persists_fingerprint(tmp_path, caplog):
    """First connect to a new host: WARNING logged, fingerprint written
    to the store. This is the only path that should write to disk."""
    store = tmp_path / 'known_hosts'
    p = c._LoggingHostKeyPolicy(str(store))
    with caplog.at_level(logging.WARNING):
        p.missing_host_key(_FakeClient(), 'h.example', LEGIT)
    assert any('first SSH connect' in r.message for r in caplog.records)
    assert store.exists()
    lines = _read_store(store)
    assert len(lines) == 1
    assert lines[0].startswith('h.example ssh-rsa ')


def test_key_change_warns_but_does_not_persist(tmp_path, caplog):
    """Key change: WARNING logged, but the on-disk record stays at the
    original (legitimate) fingerprint. This is the C1 fix -- previously
    the new key was silently ratified on disk."""
    store = tmp_path / 'known_hosts'
    p = c._LoggingHostKeyPolicy(str(store))
    p.missing_host_key(_FakeClient(), 'h.example', LEGIT)
    legit_line = _read_store(store)[0]

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        p.missing_host_key(_FakeClient(), 'h.example', ATTACK)
    assert any('changed' in r.message for r in caplog.records)
    # On-disk record unchanged: still the legitimate fingerprint
    assert _read_store(store) == [legit_line]


def test_key_change_rewarns_across_restart(tmp_path, caplog):
    """Regression-locker for the C1 disk-state ratification bug. Original
    code stored the swapped fingerprint, so the *next* policy instance
    loaded from the same path had prev == attacker_fp and emitted no
    warning -- the swap became silent across restarts.

    Now: every fresh policy instance pointed at the same store re-warns
    on the swapped fingerprint until the operator hand-edits the file."""
    store = tmp_path / 'known_hosts'
    # Round 1: legitimate first contact
    c._LoggingHostKeyPolicy(str(store)).missing_host_key(_FakeClient(), 'h', LEGIT)

    # Round 2: simulated daemon restart, attacker presents new key
    p2 = c._LoggingHostKeyPolicy(str(store))
    with caplog.at_level(logging.WARNING):
        p2.missing_host_key(_FakeClient(), 'h', ATTACK)
    assert any('changed' in r.message for r in caplog.records)

    # Round 3: another simulated restart, attacker still on the link.
    # The original (broken) code was silent here; the fix must re-warn.
    p3 = c._LoggingHostKeyPolicy(str(store))
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        p3.missing_host_key(_FakeClient(), 'h', ATTACK)
    assert any('changed' in r.message for r in caplog.records), \
        'C1 regression: warning was silent on subsequent restart'


def test_no_persistence_when_store_path_is_none(tmp_path, caplog):
    """`--known-hosts ''` path: policy still logs first-contact and
    key-change warnings but never touches disk. Used by the docker-
    compose deployment where USER nobody's $HOME is /nonexistent."""
    p = c._LoggingHostKeyPolicy(None)
    with caplog.at_level(logging.WARNING):
        p.missing_host_key(_FakeClient(), 'h', LEGIT)
        p.missing_host_key(_FakeClient(), 'h', ATTACK)
    msgs = [r.message for r in caplog.records]
    assert any('first SSH connect' in m for m in msgs)
    assert any('changed' in m for m in msgs)
    # No file should have been created anywhere we can see
    assert not list(tmp_path.iterdir())


def test_first_contact_is_persisted_across_restart(tmp_path):
    """Sanity: legitimate first contact really does persist. The
    key-change-no-persist behaviour must not break the working path."""
    store = tmp_path / 'known_hosts'
    c._LoggingHostKeyPolicy(str(store)).missing_host_key(_FakeClient(), 'h', LEGIT)
    p2 = c._LoggingHostKeyPolicy(str(store))
    # The reload populates _known with the legit fingerprint
    assert 'h' in p2._known  # pylint: disable=protected-access
