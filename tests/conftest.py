"""Shared pytest fixtures.

The exporter no longer parses argv at import time, but the rest of the
module still expects args + PROBES to be populated before handlers are
called. We do that once here for the whole suite. Tests don't use the
network; the values just need to be syntactically valid.
"""
import gpon_exporter


def pytest_configure(config):  # pylint: disable=unused-argument
    if gpon_exporter.args is None:
        gpon_exporter.init_args([
            '--device', 'admin:unused@test:22',
            '--enable-omci',
        ])
