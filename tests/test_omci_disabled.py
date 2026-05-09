"""Test that --enable-omci=off actually excludes the omcicli probes from
PROBES. init_args() mutates module-level state, so toggling it in-process
would pollute the rest of the suite. Spawn a fresh interpreter instead --
slow but isolated.
"""
import os
import subprocess
import sys


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _check_probes(extra_args):
    """Import gpon_exporter in a subprocess, run init_args with the given
    extras, return the list of PROBES keys."""
    argv = ['--device', 'u:p@t:22'] + list(extra_args)
    code = (
        "import sys; "
        "sys.path.insert(0, " + repr(REPO_ROOT) + "); "
        "import gpon_exporter as c; "
        "c.init_args(" + repr(argv) + "); "
        "print('|'.join(k for k, _, _ in c.PROBES))"
    )
    out = subprocess.check_output(
        [sys.executable, '-c', code],
        cwd=REPO_ROOT,
        stderr=subprocess.STDOUT,
        timeout=15,
    )
    return out.decode().strip().split('|')


def test_omci_probes_present_when_enabled():
    keys = _check_probes(['--enable-omci'])
    assert 'authuptime' in keys
    assert 'loidauth' in keys
    assert 'sn' in keys


def test_omci_probes_absent_when_disabled():
    keys = _check_probes([])  # no --enable-omci
    # 'sn' was an omcicli probe historically but moved to DIAG_PROBES once we
    # switched to parsing the omci_app argv from `ps` (omcicli is broken on
    # V1.0-220923; QUIRKS has the detail).
    omci_only = {'authuptime', 'loidauth'}
    leaked = omci_only & set(keys)
    assert not leaked, f'omcicli probes leaked into PROBES with --enable-omci off: {leaked}'
    # Diag probes must still be there
    assert 'firmware' in keys
    assert 'bias_current' in keys
    assert 'sn' in keys, 'sn should now be a diag probe, present without --enable-omci'
