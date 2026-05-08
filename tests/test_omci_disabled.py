"""Test that --enable-omci=off actually excludes the omcicli probes from
PROBES. The conftest.py used by the rest of the suite sets --enable-omci
on at module-import time, so we can't toggle it within the same Python
process. Spawn a fresh interpreter instead, which is clean and slow but
fine for this single coverage gap.
"""
import os
import subprocess
import sys


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _check_probes(extra_args):
    """Import gpon_exporter in a subprocess with the given argv, return the
    list of PROBES keys it constructs."""
    code = (
        "import sys; "
        "sys.argv = ['gpon_exporter.py', '--device', 'u:p@t:22'] + "
        + repr(list(extra_args)) + "; "
        "sys.path.insert(0, " + repr(REPO_ROOT) + "); "
        "import gpon_exporter as c; "
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
    omci_only = {'authuptime', 'loidauth', 'sn'}
    leaked = omci_only & set(keys)
    assert not leaked, f'omcicli probes leaked into PROBES with --enable-omci off: {leaked}'
    # Diag probes must still be there
    assert 'firmware' in keys
    assert 'bias_current' in keys
