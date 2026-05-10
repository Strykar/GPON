"""Guard against accidentally exposing SFP-side credentials as Prometheus
labels.

Several SFP files store secrets in plaintext (see docs/QUIRKS.md):

- ``/var/config/lastgood.xml`` and ``lastgood_hs.xml`` -- contain
  ``SUSER_PASSWORD``, ``E8BDUSER_PASSWORD``, ``LOID_PASSWD``,
  ``GPON_PLOAM_PASSWD`` as ``<Value Name="..." Value="..."/>`` entries.
- ``mib show`` output -- same secrets as ``KEY=value`` lines.

A future probe that reads any of these files with a generic key/value
regex would land those secrets as label values on a Prometheus metric,
where they get persisted in scrape history and Grafana snapshots.

This test feeds canary versions of each known-leak source through every
registered probe handler and asserts no metric label, label name, or
metric name in the global registry contains the canary string. If it
fires, either:

- a new probe widened its parser to scoop up secrets, or
- a new credential-bearing field name was added to the upstream firmware
  config and the input fixtures here need an update.

Either way, fix it before merging.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from prometheus_client import REGISTRY  # noqa: E402

import gpon_exporter as c  # noqa: E402


# Canary strings -- distinct, unmistakable, would never appear in legitimate
# metric data. If any of these turn up as a label value, name, or metric name,
# the probe leaked.
CANARY_PASSWORD     = 'CANARY-PW-DO-NOT-LEAK-d3ba9f1c'
CANARY_LOID_PW      = 'CANARY-LOID-d3ba9f1c'
CANARY_PLOAM_PW     = 'CANARY-PLOAM-d3ba9f1c'
CANARY_E8BD_PW      = 'CANARY-E8BD-d3ba9f1c'
ALL_CANARIES = (CANARY_PASSWORD, CANARY_LOID_PW, CANARY_PLOAM_PW, CANARY_E8BD_PW)

# Known-sensitive field names the firmware uses (per docs/QUIRKS.md). If any
# of these literal field names turns up as a metric label *name*, that's also
# a smell -- even an empty-string value gives away the field exists, and
# the next field-value extension would publish the value.
SENSITIVE_FIELD_NAMES = (
    'SUSER_PASSWORD', 'E8BDUSER_PASSWORD',
    'LOID_PASSWD', 'LOID_PASSWD_OLD',
    'GPON_PLOAM_PASSWD',
    'password', 'passwd', 'secret', 'apikey', 'api_key',
)


def _lastgood_xml_canary():
    """A faithful-shaped slice of /var/config/lastgood.xml with secrets
    replaced by canary tokens. If a probe ever cat's this file with a
    generic key/value parser, the canaries will land in metrics."""
    return (
        '<Config Name="ROOT">\n'
        ' <Dir Name="MIB_TABLE">\n'
        '  <Value Name="WAN_MODE" Value="7"/>\n'
        '  <Value Name="LAN_IP_ADDR" Value="192.168.1.1"/>\n'
        '  <Value Name="SUSER_NAME" Value="admin"/>\n'
        f'  <Value Name="SUSER_PASSWORD" Value="{CANARY_PASSWORD}"/>\n'
        '  <Value Name="E8BDUSER_NAME" Value="administrator"/>\n'
        f'  <Value Name="E8BDUSER_PASSWORD" Value="{CANARY_E8BD_PW}"/>\n'
        f'  <Value Name="LOID_PASSWD" Value="{CANARY_LOID_PW}"/>\n'
        f'  <Value Name="GPON_PLOAM_PASSWD" Value="{CANARY_PLOAM_PW}"/>\n'
        '  <Value Name="OMCI_SW_VER1" Value="V1.0-220923"/>\n'
        ' </Dir>\n'
        '</Config>\n'
    )


def _mib_show_canary():
    """`mib show` output style -- one KEY=value per line, with the same
    secret field names embedded."""
    return (
        'WAN_MODE=7\n'
        'LAN_IP_ADDR=192.168.1.1\n'
        'SUSER_NAME=admin\n'
        f'SUSER_PASSWORD={CANARY_PASSWORD}\n'
        f'E8BDUSER_PASSWORD={CANARY_E8BD_PW}\n'
        f'LOID_PASSWD={CANARY_LOID_PW}\n'
        f'GPON_PLOAM_PASSWD={CANARY_PLOAM_PW}\n'
        'OMCI_SW_VER1=V1.0-220923\n'
    )


def _ps_with_canary_argv():
    """`ps` output where omci_app's command line carries a canary in a
    position other than the SN. handle_sn currently regex-matches `-s
    <token>` -- if the regex is widened to match other args, a canary
    in the password slot would leak."""
    return (
        f'  321 admin    22616 S    omci_app -s DSNW282D5510 '
        f'-pw {CANARY_PASSWORD} -f off 0 -m enable_wq -d err\n'
    )


CANARY_INPUTS = [
    ('lastgood.xml',  _lastgood_xml_canary()),
    ('mib show',      _mib_show_canary()),
    ('ps with arg',   _ps_with_canary_argv()),
]


def _all_metric_text():
    """Return every metric name + every label name + every label value
    currently in the default registry, joined into one big string. We
    grep this for canaries; any hit means a probe leaked."""
    parts = []
    for collector in list(REGISTRY._collector_to_names):  # pylint: disable=protected-access
        try:
            metrics = collector.collect()
        except Exception:  # pylint: disable=broad-exception-caught
            continue
        for metric in metrics:
            parts.append(metric.name)
            for sample in metric.samples:
                parts.append(sample.name)
                for k, v in sample.labels.items():
                    parts.append(k)
                    parts.append(str(v))
    return '\n'.join(parts)


def test_no_handler_leaks_credentials_from_known_sources():
    """Feed each canary input through every registered probe handler.
    Assert no canary string lands in any metric name, label name, or
    label value."""
    test_ip = '10.0.99.99'
    for source_name, payload in CANARY_INPUTS:
        for probe_key, _, handler in c.PROBES:
            try:
                handler(payload, test_ip)
            except Exception as e:  # pylint: disable=broad-exception-caught
                # A handler may legitimately reject malformed input; that's
                # fine. We're checking the success path didn't leak.
                _ = (probe_key, e)
        text = _all_metric_text()
        for canary in ALL_CANARIES:
            assert canary not in text, (
                f'CREDENTIAL LEAK: canary {canary!r} from {source_name!r} '
                f'landed in registered metrics. A probe handler is parsing '
                f'output it should not. See docs/QUIRKS.md for the list of '
                f'sensitive sources.'
            )


def test_no_metric_label_name_matches_a_known_secret_field():
    """Independent of input, scan every label name across all collectors
    and fail if any matches a known sensitive field name (even
    case-insensitively). Catches a probe that, say, exposed
    `gpon_config_info{SUSER_PASSWORD=""}` as a benign-looking metric."""
    label_names = set()
    for collector in list(REGISTRY._collector_to_names):  # pylint: disable=protected-access
        try:
            metrics = collector.collect()
        except Exception:  # pylint: disable=broad-exception-caught
            continue
        for metric in metrics:
            for sample in metric.samples:
                label_names.update(sample.labels.keys())
    lower = {n.lower() for n in label_names}
    for forbidden in SENSITIVE_FIELD_NAMES:
        assert forbidden.lower() not in lower, (
            f'CREDENTIAL LEAK SMELL: label name {forbidden!r} appears on a '
            f'metric. Even an empty value advertises the field exists; the '
            f'next change to populate it would leak the secret. Use a '
            f'different label name or drop the metric.'
        )
