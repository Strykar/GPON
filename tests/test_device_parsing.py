"""Test the --device user:password@host[:port] parser. Run in subprocesses
to keep init_args() state isolated between cases."""
import os
import subprocess
import sys


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _parse(device_args, env=None):
    """Spawn a subprocess that calls init_args() with the given --device
    args, then prints the resulting (host, port, user, password) tuple list.
    Returns the parsed list, or raises CalledProcessError on argparse error."""
    code = (
        f"import sys; sys.path.insert(0, {REPO_ROOT!r}); "
        "import gpon_exporter as c; "
        f"c.init_args({device_args!r}); "
        "print(repr(list(zip(c.args.hostname, c.args.port, c.args.user, c.args.password))))"
    )
    e = dict(os.environ)
    if env:
        e.update(env)
    out = subprocess.check_output([sys.executable, '-c', code], cwd=REPO_ROOT,
                                  stderr=subprocess.STDOUT, timeout=10, env=e)
    return eval(out.decode().strip())  # pylint: disable=eval-used


def test_full_form():
    parsed = _parse(['--device', 'admin:secret@192.168.1.1:22'])
    assert parsed == [('192.168.1.1', 22, 'admin', 'secret')]


def test_default_port_is_22():
    parsed = _parse(['--device', 'admin:secret@192.168.1.1'])
    assert parsed == [('192.168.1.1', 22, 'admin', 'secret')]


def test_password_from_env():
    parsed = _parse(['--device', 'admin@192.168.1.1'],
                    env={'ONU_SSH_PASSWORD': 'fromenv'})
    assert parsed == [('192.168.1.1', 22, 'admin', 'fromenv')]


def test_multiple_devices():
    parsed = _parse([
        '--device', 'admin:p1@10.0.0.1',
        '--device', 'user2:p2@10.0.0.2:2222',
    ])
    assert parsed == [
        ('10.0.0.1', 22, 'admin', 'p1'),
        ('10.0.0.2', 2222, 'user2', 'p2'),
    ]


def test_missing_password_no_env_errors():
    """No embedded password and no ONU_SSH_PASSWORD env -> argparse error."""
    e = dict(os.environ)
    e.pop('ONU_SSH_PASSWORD', None)
    code = (
        f"import sys; sys.path.insert(0, {REPO_ROOT!r}); "
        "import gpon_exporter as c; "
        "c.init_args(['--device', 'admin@host'])"
    )
    proc = subprocess.run([sys.executable, '-c', code], cwd=REPO_ROOT,
                          env=e, capture_output=True, timeout=10, check=False)
    assert proc.returncode != 0
    assert b'no password' in proc.stderr or b'no password' in proc.stdout
