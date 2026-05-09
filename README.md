# GPON

Notes, firmware, a Prometheus collector, and a Grafana dashboard for the
HSGQ / ODI (Realtek RTL960x) GPON SFP.

![HSGQ XPON-Stick GPON SFP](docs/hsgq_onu.png)

![Grafana dashboard screenshot](docs/screenshot.png)

## Breaking changes in v1.0.0

If you ran the pre-v1.0.0 collector, the renames below will break any
external alerting rules, recording rules, or dashboard forks. The shipped
`dashboard.json` is migrated; only your own copies need attention.

| Old | New |
| --- | --- |
| `gpon_collector_*` self-metrics | `gpon_exporter_*` |
| `gpon_bias_current_mA` | `gpon_bias_current_amperes` (value scaled, not just renamed) |
| Cumulative counters (`gpon_ds_*`, `gpon_us_*`, `gpon_activation_*`, `gpon_rogue_sd_*`) | Same names with `_total` suffix; type changed from Gauge to Counter. Use `rate(metric_total[15m])`. |
| `--hostname H --port N --user U --password P` (positional, repeatable) | `--device user:password@host[:port]` (URL-form, repeatable) |
| `GPON_PASSWORD` env var | `ONU_SSH_PASSWORD` |
| Default bind address `0.0.0.0` | `127.0.0.1` (use `--bind-address 0.0.0.0` to opt back in) |

## Scope

The collector talks to the SFP over SSH and runs Realtek's `diag` and
`omcicli` utilities. That makes it stable across firmware revisions because
the CLI output format barely changes, in contrast to web-scraping collectors
that break whenever the vendor reshuffles `/stats.asp` (see issue #3).

If you cannot SSH to your SFP, the Lua web-scraper at
[Anime4000/RTL960x discussion #466](https://github.com/Anime4000/RTL960x/discussions/466)
is the alternative.

Verified on firmware **V1.0-220923** (M110 SFU). The 240408 SFU and
2023-10-21 / V1.1.3-2025-06-20 HGU builds are available as
[Releases](https://github.com/Strykar/GPON/releases) and should work given
the diag CLI compatibility, but I haven't tested those personally.

## How to use

The shortest path from "fresh repo" to "metrics in Grafana":

```sh
# 1. install runtime deps
pip install -r requirements.txt

# 2. run the exporter against your SFP
export ONU_SSH_PASSWORD='your-password-here'
python3 gpon_exporter.py --device admin@192.168.1.1
# /metrics now serves on http://127.0.0.1:8114/metrics

# 3. point Prometheus at it
#    scrape_configs:
#      - job_name: gpon_exporter
#        static_configs:
#          - targets: ['127.0.0.1:8114']

# 4. import dashboard.json into Grafana, pick your Prometheus as the datasource.
```

For long-running deployments use the systemd unit ([systemd](#systemd)) or
the Docker compose file ([Docker / Podman](#docker--podman)) instead of
running by hand. Multi-ONU and Proxmox/LXC are documented further down.

If something doesn't look right, the first thing to run is
`gpon_exporter.py --diagnose` -- it prints raw probe output and is the
fastest way to tell whether the SFP, the credentials, or the parser is
the issue. See [Troubleshooting](#troubleshooting).

## What it exposes

About 75 metrics. All `gpon_*` device counters come from one of three SFP
commands: `diag pon get transceiver ...`, `diag gpon ...`, and (optionally)
`omcicli get ...`.

### Optical readouts

| Metric | What it is |
| --- | --- |
| `gpon_temperature_celsius` | SFP SoC temperature |
| `gpon_voltage_volts` | Supply voltage |
| `gpon_tx_power_dbm` | Tx optical power |
| `gpon_rx_power_dbm` | Rx optical power |
| `gpon_bias_current_amperes` | Laser bias current. Device reports mA; the parser scales to amperes for Prometheus base-unit convention. |

### State and alarms

| Metric | What it is |
| --- | --- |
| `gpon_onu_state` | 1=O1 Initial .. 5=O5 Operation .. 7=O7 Emergency Stop. `0` means the parser didn't recognise the state output (probable firmware change). |
| `gpon_alarm_los`, `_lof`, `_lom`, `_sf`, `_sd`, `_tx_too_long`, `_tx_mismatch` | Alarm gauges. `1` = raised, `0` = clear. |

### Downstream PHY counters (BIP / FEC / superframe / PLEN)

`gpon_ds_bip_error_bits`, `gpon_ds_bip_error_blocks`,
`gpon_ds_fec_correct_bits`, `gpon_ds_fec_correct_bytes`,
`gpon_ds_fec_correct_codewords`, `gpon_ds_fec_uncorrectable_codewords`,
`gpon_ds_superframe_los`, `gpon_ds_plen_fail`, `gpon_ds_plen_correct`.

### Downstream PLOAM / BWMAP / OMCI / Ethernet / GEM

`gpon_ds_ploam_received`, `_crc_errors`, `_processed`, `_overflow`, `_unknown`;
`gpon_ds_bwmap_received`, `_crc_errors`, `_overflow`, `_invalid0`, `_invalid1`;
`gpon_ds_omci_received`, `_bytes`, `_processed`, `_dropped`, `_crc_errors`;
`gpon_ds_ethernet_unicast`, `_multicast`, `_multicast_forwarded`,
`_multicast_leaked`, `_fcs_errors`;
`gpon_ds_gem_idle`, `_non_idle`, `_los`, `_over_interleave`,
`_mis_packet_length`, `_multi_flow_match`, `gpon_ds_hec_correct`.

### Upstream

`gpon_us_boh`, `gpon_us_dbr`,
`gpon_us_ploam_transmitted`, `_processed`, `_urgent`, `_urgent_processed`,
`_normal`, `_normal_processed`, `_serial_number`, `_nomsg`,
`gpon_us_omci_transmitted`, `_processed`, `_bytes`,
`gpon_us_gem_blocks`, `_bytes`.

### Activation and rogue counters

`gpon_activation_sn_requests`, `gpon_activation_ranging_requests`,
`gpon_rogue_sd_too_long`, `gpon_rogue_sd_mismatch`.

### `--enable-omci` extras

`gpon_pon_uptime_seconds`,
`gpon_loid_auth_status`, `_attempts`, `_success`,
`gpon_device_info{serial_number=...}`.

### Self-health metrics

| Metric | What it is |
| --- | --- |
| `gpon_exporter_up` | `1` if last fetch succeeded, `0` if it failed. Per device. |
| `gpon_exporter_fetch_seconds` | Wall-clock seconds for the last fetch attempt. |
| `gpon_exporter_last_fetch_timestamp` | Unix timestamp of the last successful fetch. |
| `gpon_exporter_fetch_failures_total` | Counter of fetch failures since the daemon started. |
| `gpon_firmware_info{version=...}` | Firmware version captured from the SFP's `/etc/version`. |
| `gpon_exporter_info{version=...}` | This exporter's version (pulled from `__version__` in the source so it can't drift from CI/Docker pins). |

All cumulative device counters (BIP, FEC, BWMAP, Ethernet, GEM, OMCI, PLOAM,
activation, rogue-SD) are exposed as Prometheus **Counters** with a `_total`
suffix on the metric name. Use `rate(metric_total[15m])` in dashboards and
alerts. Counter resets (SFP reboot) are handled correctly by `rate()` without
state in the collector.

## Running it

### Requirements

```sh
pip install -r requirements.txt   # paramiko, prometheus_client
```

### Standalone

```sh
python3 gpon_exporter.py \
  --device "admin:$ONU_SSH_PASSWORD@192.168.1.1:22" \
  --webserver-port 8114
```

The `--device` flag takes a `user:password@host[:port]` connection string;
port defaults to 22 if omitted. Password may also be left out, in which
case `ONU_SSH_PASSWORD` from the environment is used:

```sh
export ONU_SSH_PASSWORD='your-password-here'
python3 gpon_exporter.py --device admin@192.168.1.1
```

The env-var path is preferable so the password doesn't appear in `ps`
output. Multiple ONUs: pass `--device` once per ONU. Each can have its own
embedded password, or all can fall back to the same env var:

```sh
python3 gpon_exporter.py \
  --device admin:p1@10.0.0.1 \
  --device admin:p2@10.0.0.2:2222
```

The metrics endpoint binds to `127.0.0.1` by default (loopback only). If
your Prometheus runs on a different host, pass `--bind-address 0.0.0.0`.
Inside Docker/Podman the compose file already does this for you.

### systemd

The repo's `odi.service` is the canonical example: env-file based password,
no `After=sshd.service` (the collector is an SSH client, not a server),
sensible `RestartSec`/`StartLimit*`. Edit the hostname, port, and user
inline; drop it in `/etc/systemd/system/`; create the credentials file
at `/etc/gpon-exporter/credentials` containing exactly:

```sh
ONU_SSH_PASSWORD=your-password-here
```

then:

```sh
sudo install -m 0600 -o root -g root /dev/null /etc/gpon-exporter/credentials
sudo "$EDITOR" /etc/gpon-exporter/credentials
sudo systemctl daemon-reload
sudo systemctl enable --now odi
```

The 0600 + root-owned permissions matter — systemd does not enforce them,
and a 0644 file with `ONU_SSH_PASSWORD=...` is a foot-gun on a multi-user host.

### Docker / Podman

```sh
cp .env.example .env       # then fill in credentials
docker compose up -d       # or: podman-compose up -d
```

The same `Dockerfile` and `docker-compose.yml` work under both runtimes. The
container has a `HEALTHCHECK` that pulls `/metrics` so `docker ps` shows
healthy/unhealthy state. The host port is bound to `127.0.0.1:8114` by
default; switch to `8114:8114` in the compose file if Prometheus runs on
another machine.

**The compose file is single-device.** Multiple SFPs need either
`docker compose -p` per device with separate `.env` files, or a manual
multi-service rewrite. Same caveat as Standalone above.

### Proxmox (LXC)

Create a Debian 12 unprivileged LXC, then inside it:

```sh
apt install -y python3-pip git
git clone https://github.com/Strykar/GPON.git
cd GPON
pip install --break-system-packages -r requirements.txt
cp odi.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now odi
```

Make sure the LXC has a route to the SFP's management IP.

## CLI flags

| Flag | Default | What it does |
| --- | --- | --- |
| `--device user:password@host[:port]` | required | ONU connection string. Repeatable for multi-ONU. Port defaults to 22 if omitted. Password may be omitted to fall back on the `ONU_SSH_PASSWORD` env var. |
| `--webserver-port N` | `8114` | Port for the Prometheus metrics endpoint. |
| `--bind-address ADDR` | `127.0.0.1` | Loopback by default. Use `0.0.0.0` for Docker or to expose to the network. |
| `--interval SEC` | `300` | Seconds between fetches. |
| `--enable-omci` | off | Probe `omcicli` for uptime, LOID, serial. See caveat below. |
| `--once` | off | Run a single fetch and exit. Exit code = number of devices whose fetch failed (0 = all good, useful for cron). |
| `--diagnose` | off | Print a verbose probe report and exit. First thing to run when filing a bug. |
| `--log-level LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `--known-hosts PATH` | `~/.config/gpon-exporter/known_hosts` | File for persisting SSH host-key fingerprints. WARNING is logged on first contact and on key change. Pass `''` to disable persistence (still logs fingerprints). |

## `--enable-omci` caveat

The omcicli probes deliver three useful metrics
(`gpon_pon_uptime_seconds`, the LOID auth state, and the SFP serial number),
but they're off by default. On at least firmware V1.0-220923, running any
`omcicli` command immediately after a `diag` command wedges the on-device
`omci_app` daemon for several minutes; recovery may need a power-cycle. The
collector sequences `omcicli` *before* `diag` to dodge this, but the wedge
behaviour is firmware-dependent. If you turn it on and the log shows
`channel closed mid-fetch`, switch it off again.

## Grafana dashboard

`dashboard.json` is organised into six rows. Two are collapsed by default
(Attenuation and Collector health) since they're reference / diagnostic
content; the rest are open.

The dashboard's `$instance` and `$ip` template variables filter by scrape
target and SFP IP for multi-device setups. Default time range is `now-15m`,
which covers about three fetch cycles at the default 5-minute interval.

### Attenuation (collapsed)

Static reference content, three side-by-side text panels:

- **Common attenuation sources**: link-budget cheat sheet (loss per km on
  1310/1490 nm, splice loss, connector loss, splitter loss formula). Plus
  references to UISP's GPON design guide, an ONU registration-state primer,
  and the EPON/GPON/XG-PON activation paper.
- **Tech specs**: HSGQ XPON-Stick form factor, electrical, and mode tables.
- **Features**: wavelengths, GPON/EPON line rates, MSA/DDM compliance, ITU-T
  standards, laser-class and RoHS marks.

### ONU status

Three stat panels at the top of the dashboard. Designed to answer "is the
link healthy right now?" at a glance.

- **ONU state**: integer 1-7 (O1..O7). 5 (Operation) is the only good
  state; anything else means we're not actually carrying traffic. `0` is
  the parser's "I don't recognise this output" sentinel — read it as
  "firmware drift", not "state O0".
- **Alarms**: seven cells, one per alarm bit. Green `OK` = clear, red
  `RAISED` = active. Severity, worst first: Loss of Signal (catastrophic),
  Loss of Frame / Loss of MAC / Signal Fail (severe), Signal Degraded
  (warning), TX Too Long / TX Mismatch (config issues).
- **Activation events (last 1h)**: `sum(increase(gpon_activation_sn_requests_total[1h])) +
  sum(increase(gpon_activation_ranging_requests_total[1h]))`. The `sum()`
  wrapper folds any historical series with a different label set into a
  single cell — needed because exporter restarts that change labels leave
  stale series queryable for the retention window. Healthy operation is
  zero events; non-zero usually means the OLT bounced you.

### Laser / CPU / RAM / Voltage / Temperature

Six gauges. Instant readouts of the values that matter for "is this SFP
operating inside spec?". Each gauge uses the metric directly with
`legendFormat={{instance}}` so multi-device setups label cleanly.

- **Laser Tx (1310 nm)**: `gpon_tx_power_dbm`. Factory range 0.5–5.0 dBm.
  Green inside spec, orange near the edges, red out of spec.
- **Laser Rx (1490 nm)**: `gpon_rx_power_dbm`. Factory range -27 to -8 dBm.
  Same colour banding. Below -27 dBm = link is too dark to operate.
- **SFP voltage**: `gpon_voltage_volts`. Nominal 3.3 V; thresholds at
  3.135 V (-5%) and 3.465 V (+5%).
- **RAM**: `process_resident_memory_bytes`. Healthy steady-state is
  ~50 MiB; thresholds at 100 / 200 MiB flag a leak.
- **CPU**: `100 * rate(process_cpu_seconds_total[5m])` — converts seconds
  per second to percent of one core. Threshold 5/20% flags a runaway loop.
- **SoC temperature**: `gpon_temperature_celsius`. Commercial-temp range
  0–70 °C; thresholds at 60 and 80 °C.

### Temperature & GPON signal metrics

The same four optical metrics as the gauges above, but as timeseries to
show movement over the dashboard window. Filled lines, smooth interpolation,
threshold red/orange zones drawn behind the line so out-of-spec excursions
are obvious without checking the y-axis.

- **SFP (commercial) SoC temperature**: temperature trend.
- **Signal Tx power**: Tx power trend with threshold bands.
- **Signal Rx power**: Rx power trend with threshold bands.
- **Bias current**: `gpon_bias_current_amperes`. Climbing bias current
  with flat or falling Tx power is the textbook laser-aging signature.

### GPON counters (diag-only)

All cumulative-counter timeseries with `rate(metric_total[15m])`. The
counter type and rate-window choices are explained under [query
conventions](#query-conventions) below.

- **FEC corrected codewords/sec** (`corrected`): rate of FEC correction
  events. Non-zero is normal.
- **FEC uncorrectable codewords/sec** (`uncorrectable`): rate of codewords
  the FEC layer couldn't recover. Non-zero means bit errors are leaking
  upstream — a real alert signal.
- **Downstream BIP errors/sec** (`error bits` + `error blocks`): bit
  interleaved parity error counters. Like FEC uncorrectable, this is the
  link-quality canary.
- **Downstream Ethernet frames/sec** (`unicast`, `multicast`, `FCS errors`):
  three series; the FCS errors series is pinned to a secondary y-axis
  because it's typically zero against thousands of frames per second on
  the primary axis.
- **Downstream BWMAP/sec** (`received`, `CRC errors`): bandwidth-map
  messages from the OLT, plus CRC errors on the same. Same secondary-y-axis
  treatment for the error series.
- **PLOAM messages/sec** (`DS received`, `DS CRC errors`, `US transmitted`):
  PLOAM (Physical Layer Operation, Administration and Maintenance) message
  counts per direction. CRC errors on secondary y-axis.
- **Downstream GEM data frame rate** (`non-idle frames`):
  `gpon_ds_gem_non_idle` rate. The idle counter saturates at 2^32-1 so the
  dashboard charts only the non-idle side as the link-utilisation indicator
  (see QUIRKS).
- **OMCI message rate** (`DS received`, `DS processed`): downstream OMCI
  management message rate. Healthy steady-state is ~10 msg/s.

### Collector health (collapsed)

Self-metrics about the exporter process, not the SFP. Useful to confirm
"is the collector working?" before assuming the SFP is the problem.

- **Collector status**: `gpon_exporter_up`. 1 = last fetch succeeded.
- **Firmware**: `gpon_firmware_info`'s `version` label, displayed via
  `legendFormat={{version}}`.
- **Last fetch**: `time() - gpon_exporter_last_fetch_timestamp` — seconds
  since the last successful fetch. Red if it grows past one fetch interval.
- **Daemon uptime**: `time() - process_start_time_seconds`.
- **Fetch duration**: `avg_over_time(gpon_exporter_fetch_seconds[5m])`.
  Steady-state ~3 s on a healthy link; sustained climb suggests SFP load
  or link degradation.
- **Fetch failure rate**: `rate(gpon_exporter_fetch_failures_total[15m])`.
  Real Counter, real `rate()`. Persistently non-zero means the daemon is
  flapping, not just a one-off.

## Query conventions

A few patterns repeat across the dashboard; they're collected here so the
panels themselves stay terse.

### `rate(metric_total[15m])` for cumulative counters

Every cumulative device counter is exposed as a Prometheus **Counter** type
with a `_total` suffix (e.g. `gpon_ds_bwmap_received_total`). The
exporter's `_AbsoluteCounter` wrapper translates the device's absolute
running counters into proper `Counter.inc(delta)` calls, with explicit
counter-reset handling for SFP reboots. Because the metric type is real
Counter, `rate()` is the right function and there's no "metric might not
be a counter" hint in Grafana.

The 15-minute window matches the 5-minute fetch cadence: each window
covers 2-3 actual fetches, smoothing out the synchronised step pattern a
shorter window would show (every counter on the device updates at the same
moment, once per fetch — see QUIRKS for the timing detail). If you change
`--interval`, scale the window proportionally.

### `sum(increase(...[1h]))` on the activation panel

Wrapping `increase()` in `sum()` collapses any stale time series with a
different label set into a single cell. Without it, an exporter restart
that changes labels (or adds an `ip` value) leaves the previous series
queryable for the retention window — and a `[1h]` lookback scoops both up
and renders them as separate panels. The `sum()` is safe given the
single-device-per-exporter `$instance` filter; multi-device setups with
shared exporters would want `sum by(ip)` instead.

### Threshold-banded timeseries instead of heatmap for Tx/Rx

We tried a heatmap reimagining of the Tx/Rx Power panels. Looked great for
Tx (some natural variance, distribution visible) and broke ugly on Rx
(samples mostly at one identical value, Grafana fills the whole y-range
with cells, presenting as a solid block). Threshold-banded timeseries
works on any data shape and immediately telegraphs "is the link inside
spec?" without reading the y-axis numbers.

### Secondary y-axis for mixed-magnitude series

`Downstream Ethernet frames/sec`, `Downstream BWMAP/sec`, and
`PLOAM messages/sec` chart traffic counts (thousands/sec) and error counts
(typically 0) on the same panel. Auto-scaling y-axis hides the errors at
the bottom of the chart. The error series is pinned to a secondary y-axis
so a single FCS / CRC error is visible against thousands of unicast
frames per second on the primary axis.

### `process_*` queries keep the `job=` filter; `gpon_*` queries drop it

`process_*` metrics come from prometheus_client and exist for *any*
exporter Prometheus scrapes, so they need `job="gpon_exporter"` to
disambiguate. `gpon_*` metric names are unique to this exporter, so the
filter is redundant noise on those queries and we don't include it.

### Activation churn as a stat, not timeseries

`gpon_activation_sn_requests_total` and `_ranging_requests_total` are flat
zero in healthy operation. A timeseries chart of two flat zero lines is
uninformative. A stat panel ("0 events" green, non-zero red) conveys the
same thing more compactly and gets attention only when something fires.

## Troubleshooting

The first thing to run when something looks wrong:

```sh
python3 gpon_exporter.py --diagnose \
  --device "admin:$ONU_SSH_PASSWORD@192.168.1.1"
```

This connects, prints firmware version, and runs every probe with its raw
output. Paste the result into a bug report.

### Common symptoms

| Log line | Likely cause |
| -------- | ------------ |
| `auth failed` | Wrong credentials in `--device user:password@host`. Some firmwares lock the account after a few failures; reboot the SFP to clear. |
| `connection refused` | sshd not running on the SFP. Check `/etc/inetd.conf` or that the boot scripts aren't disabling it. |
| `connect timed out` | Routing/IP issue. `ping 192.168.1.1` from the host running the collector. |
| `channel closed by ... mid-fetch` | Another SSH session is holding the slot, or `omci_app` got wedged (see `--enable-omci` caveat). |
| All metrics suddenly 0 | Either the SFP rebooted (counters reset) or the OLT just deauthenticated you (check `gpon_onu_state`; should be `5`). |
| `gpon_exporter_up == 0` for a host | Last fetch failed; check the collector log for the explanation line. |
| `/metrics` reachable but no `gpon_*` lines | Collector is running but no fetch has completed yet. Wait one `--interval`. |
| Some gauges frozen at one value while others update | Likely a parser regression on a new firmware. Run `--diagnose` and file an issue with the output. |
| `gpon_exporter_fetch_seconds` consistently above ~10s | The SFP is heavily loaded or the link is degraded. |
| Dashboard rate panels show identical step patterns | Prometheus scrape interval is much longer than `--interval`, or rate window is too short. The dashboard uses 15-minute windows by default; raise to `[30m]` for longer collector intervals. |

### Self-health metrics worth alerting on

- `gpon_exporter_up == 0 for 10m`: collector can't reach the SFP.
- `rate(gpon_exporter_fetch_failures_total[15m]) > 0.1`: flapping
  connection. (Real Counter, so `rate()` is the right function here.)
- `max(gpon_alarm_los, gpon_alarm_lof, gpon_alarm_lom, gpon_alarm_sf, gpon_alarm_sd) == 1`:
  any line-side alarm. Use `max()` rather than `or`, since PromQL `or` is
  set-union on labels, not boolean.
- `rate(gpon_ds_fec_uncorrectable_codewords_total[15m]) > 0`: FEC can't
  recover everything; expect bit errors upstream.
- `gpon_onu_state != 5`: ONU not in Operation state. `0` means the parser
  couldn't recognise the state output, which usually points at a firmware
  change.

### Recovering from the omci_app wedge

If you turned on `--enable-omci` and the collector starts logging
`channel closed mid-fetch`, the OMCI daemon on the SFP is stuck. Power-cycle
the SFP (unplug and reinsert) and start the collector again without
`--enable-omci`.

### Firmware quirks worth knowing

These are oddities I've found while building the collector. The full set,
with reproduction notes and design rationale, lives in [docs/QUIRKS.md](docs/QUIRKS.md).
Highlights only:

- **`diag gpon get pps-cnt` is reset-on-read** on V1.0-220923. Each call
  clears the counter, so the value bounces wildly. The collector used to
  expose it as `gpon_pps_count` but the metric was dropped because no
  Prometheus query made sense over a non-monotonic gauge.
- **`gpon_ds_gem_idle` saturates at 2^32-1** within hours of uptime, so an
  idle/non-idle ratio doesn't work. The dashboard charts non-idle alone as
  the link-utilisation signal.
- **All `_us_omci_*` metrics except `gpon_us_omci_transmitted` are flat**
  on this firmware. The metric is emitted; the dashboard ignores it.
- **`omcicli` after `diag` wedges `omci_app`** as documented above. The
  collector sequences omcicli first when `--enable-omci` is on.

## Development

### Tests

Parser unit tests and a fetch-pipeline integration test live in `tests/`:

```sh
pip install pytest
pytest tests/ -v
```

The tests use canned `diag` output captured from a real device, so they run
without network access.

### Linting

```sh
pip install pylint
pylint gpon_exporter.py
```

The repository ships a `.pylintrc` with `max-line-length=120` and the usual
docstring lints disabled.

### CI

`.github/workflows/ci.yml` runs `pylint` and `pytest` on every push and
pull request, then performs a multi-arch (amd64 + arm64) Docker build smoke
test. No image is pushed; that's deliberate.

## Firmware downloads

Firmware tarballs and the spec/manual PDFs are on the
[Releases](https://github.com/Strykar/GPON/releases) page:

| Release | Variant | Date |
| --- | --- | --- |
| `firmware/sfu-v1.0-220923` | M110 SFU V1.0 | 2022-09-23 (verified) |
| `firmware/sfu-240408` | M110 SFU | 2024-04-08 |
| `firmware/hgu-231021` | M114 HGU | 2023-10-21 |
| `firmware/hgu-v1.1.3-250620` | HGU V1.1.3 | 2025-06-20 |
| `docs` | Spec sheets and manual | n/a |
