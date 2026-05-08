<!--
Thanks for contributing. Before filling this in, please skim QUIRKS.md
(in the repo root). It documents the firmware and collector behaviours
that have already cost time to figure out -- especially the omci_app
wedge, the Gauge-vs-Counter design choice, and the dashboard's
`clamp_min(delta(metric[Nm]), 0) / N` query convention. If your change
runs into something that QUIRKS.md should explain, add to it.
-->

## Summary

<!-- One or two sentences on what this changes and why. -->

## Checklist

- [ ] I read [QUIRKS.md](../QUIRKS.md) and my change does not contradict
      a documented quirk (or my change updates QUIRKS.md to reflect new
      behaviour).
- [ ] `pylint gpon_exporter.py` still scores 10/10.
- [ ] `pytest tests/` is green. If I fixed a bug, I added a regression
      test that fails without my fix.
- [ ] If my change touches user-facing behaviour, the relevant section of
      `README.md` is updated.
- [ ] If my change touches the dashboard, the `dashboard.json` was
      re-imported into Grafana and inspected before commit.

## Test notes

<!--
How did you verify this works?

For collector changes: paste relevant `--diagnose` output, the failing
test that now passes, or `--once` exit code against a real SFP.

For dashboard changes: a screenshot of the affected panel(s) helps a lot.

If you couldn't test against real hardware, say so explicitly.
-->
