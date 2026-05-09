# Contributing

Thanks for taking the time. This project is small enough that a
one-page guide covers it.

## Before filing a bug

Run `--diagnose` first:

```sh
python3 gpon_exporter.py --diagnose \
  --device "admin:$ONU_SSH_PASSWORD@192.168.1.1"
```

It connects, prints firmware version, and runs every probe with raw
output. Paste the output (redact your password if you embed it) into
the issue. That's worth more than any description, because most bugs
turn out to be parser drift on a firmware variant, and the raw text
tells me immediately whether it's that or something else.

If `--diagnose` won't even connect, include:

- Firmware version (web UI -> system info, or `cat /etc/version` over SSH)
- The exact `--device` invocation you tried (with the password redacted)
- The full `WARNING`/`ERROR` log lines
- Output of `ssh -vv admin@<sfp>` so I can see which crypto algorithms are
  being negotiated

Read [docs/QUIRKS.md](docs/QUIRKS.md) before filing -- a fair number of
"this doesn't work" reports are documented quirks (the `omci_app` wedge,
the legacy SSH crypto, the diag REPL leak, etc.).

## Pull requests

### Scope

One fix or one feature per PR. Don't bundle "fix bug + drive-by refactor".
Reviewing a 5-line bug fix mixed with 200 lines of unrelated cleanup is
how mistakes ship.

### Tests

If your change is in a parser, add or update a test in
`tests/test_parsers.py`. Canned diag output captured from a real device
lives there already; follow the existing patterns.

If your change is in `fetch_and_update_metrics_via_ssh` or the SSH layer,
update `tests/test_fetch_pipeline.py`.

Run the suite locally:

```sh
pip install pytest
pytest tests/ -v
```

CI (`.github/workflows/ci.yml`) runs pylint + pytest + a multi-arch
Docker build smoke test on every push and PR. PRs need green CI to
merge.

### Style

- `pylint gpon_exporter.py` should stay at 10.00. The repo ships a
  `.pylintrc` with the relevant lints disabled.
- Match the existing code style: type hints are not used consistently
  yet, so don't add them in unrelated lines.
- Comments explain *why*, not *what*. The "what" is in the code. If a
  line is non-obvious, a one-line comment with the reason is great; a
  paragraph rehashing the code is noise.

### Commit messages

- Imperative mood ("fix X", "add Y", not "fixes X" or "added Y").
- Plain English. No emojis, no AI-flavoured phrasing ("It is worth noting
  that", "This change introduces", em dashes, etc.).
- First line under ~72 chars, blank line, then a paragraph if the change
  needs context.

## What goes where

- `gpon_exporter.py`: the collector. One file by design.
- `dashboard.json`: the Grafana dashboard.
- `README.md`: how to install, configure, and use.
- `docs/QUIRKS.md`: non-obvious device or collector behaviour, with repro
  notes. Things that would surprise a future maintainer go here.
- `docs/COVERAGE.md`: which dashboard panel uses which probe and metric.
- `tests/`: pytest suite.
- `firmware/`, `docs/`: tarballs and PDFs, mirrored on the Releases page.

## What I'll likely push back on

- Adding a config-file format. The CLI flags are deliberate and small.
- Adding a metrics whitelist. If a metric is noisy, fix the dashboard,
  don't gate the collector.
- Adding a long-lived SSH connection. See QUIRKS for the rationale.
- New runtime dependencies beyond `paramiko` + `prometheus_client`.

## Code of conduct

[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Short version: be civil,
keep technical discussion technical.
