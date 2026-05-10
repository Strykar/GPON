"""Lock in that every metric the dashboard queries is actually exposed
by the exporter.

L6 of the adversarial review: a panel that references a metric the
exporter no longer emits (typo, rename, accidental delete) imports
cleanly into Grafana and silently renders "No data". The PromQL
itself is valid; only the missing series turns the panel grey.

Catch it at PR time: walk dashboard.json, pull every `expr` field,
extract every gpon_* and process_* identifier, and set-diff against
what the exporter's registry actually exposes (with the right
_total / _info suffix per metric type).

Out of scope: label-name mismatches (e.g. dashboard filters on
ip=~"$ip" but the metric labels it as ipaddr). Catching those would
need scraping a live exporter; the cost-benefit doesn't justify the
test infrastructure.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from prometheus_client import REGISTRY  # noqa: E402

import gpon_exporter as c  # noqa: E402  -- imports populate the registry


# Identifier shape: gpon_<lowercase_underscores> or process_<lowercase_underscores>.
_METRIC_NAME_RE = re.compile(r'\b((?:gpon|process)_[a-z_]+)\b')

# Known identifiers that match the regex but are NOT metric names: PromQL
# label values that happen to look like metric prefixes. These appear in
# selectors like {job="gpon_exporter"} and would otherwise produce false
# positives in the dashboard-vs-registry diff.
_KNOWN_LABEL_VALUES = {
    'gpon_exporter',  # the Prometheus job_name in prometheus.yml
}

DASHBOARD_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'dashboard.json',
)


def _exposed_metric_names():
    """Return every metric name a Prometheus scrape of the exporter
    would see. prometheus_client appends _total to Counter and _info to
    Info -- the dashboard references the wire name, so we have to add
    those suffixes back when iterating.

    Iterating .samples instead of (.name, .type) would only catch
    metrics that have actually been written to (via .inc/.set/.info);
    a Counter that's never been incremented still appears on /metrics
    at value 0 but has no Sample yet at iteration time. We need the
    metric *declaration*, not its current data."""
    out = set()
    for metric in REGISTRY.collect():
        name = metric.name
        if metric.type == 'counter':
            out.add(name + '_total')
        elif metric.type == 'info':
            out.add(name + '_info')
        else:
            out.add(name)
    return out


def _dashboard_metric_refs():
    """Extract every gpon_*/process_* identifier from every panel target's
    expr in dashboard.json."""
    with open(DASHBOARD_PATH, encoding='utf-8') as f:
        d = json.load(f)
    refs = set()

    def walk(panels):
        for p in panels:
            for t in p.get('targets', []):
                expr = t.get('expr', '') or ''
                refs.update(_METRIC_NAME_RE.findall(expr))
            if 'panels' in p:
                walk(p['panels'])
    walk(d.get('panels', []))
    return refs - _KNOWN_LABEL_VALUES


def test_every_dashboard_metric_is_exposed():
    """Every gpon_*/process_* identifier the dashboard queries must
    appear on /metrics. Failure means a renamed-or-removed metric is
    still being charted -- fix the dashboard query or restore the
    metric before merging."""
    exposed = _exposed_metric_names()
    referenced = _dashboard_metric_refs()
    missing = referenced - exposed
    if missing:
        # Surface the diff clearly; ordering helps a reader scan.
        nearest = {}
        for m in sorted(missing):
            # Try to find a near match in exposed (off-by-suffix is the
            # commonest cause -- e.g. someone querying gpon_foo when the
            # exporter exposes gpon_foo_total).
            candidates = [e for e in exposed if e.startswith(m) or m.startswith(e)]
            nearest[m] = candidates[:3]
        raise AssertionError(
            'Dashboard references metrics the exporter does not expose:\n'
            + '\n'.join(
                f'  - {m}    (similar exposed: {nearest[m] or "none"})'
                for m in sorted(missing)
            )
            + '\n'
            'Either update the panel query or restore the metric in '
            'gpon_exporter.py.'
        )
