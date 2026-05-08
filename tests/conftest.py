"""Shared pytest fixtures.

The exporter module parses argparse at import time and exits if required
args are missing. We satisfy that by faking sys.argv before any test
imports the module. Tests don't use the network; the values just need to
be syntactically valid.
"""
import sys

sys.argv = [
    'gpon_exporter.py',
    '--device', 'admin:unused@test:22',
    '--enable-omci',
]
