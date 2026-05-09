# GPON

Notes, firmware, a Prometheus collector, and a Grafana dashboard for the
HSGQ / ODI (Realtek RTL960x) GPON SFP.

![HSGQ XPON-Stick GPON SFP](docs/hsgq_onu.png)

![Grafana dashboard screenshot](docs/screenshot.png)

> Upgrading from a pre-v1.0.0 install? See [docs/MIGRATION.md](docs/MIGRATION.md).

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

`dashboard.json` is organised into six rows (Attenuation, ONU status,
Laser/CPU/RAM/Voltage/Temperature, Temperature & GPON signal metrics, GPON
counters, Collector health). Per-row and per-panel documentation, plus
the rationale for each PromQL query shape, lives in
[docs/COVERAGE.md](docs/COVERAGE.md).

The dashboard's `$instance` and `$ip` template variables filter by scrape
target and SFP IP for multi-device setups. Default time range is `now-15m`.

## Troubleshooting

For the typical "something looks wrong" workflow -- `--diagnose` first,
the symptom-to-cause table, recommended alerts, and recovering from the
`omci_app` wedge -- see [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).
Firmware-level quirks worth knowing live in [docs/QUIRKS.md](docs/QUIRKS.md).

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

## Contributing

Bug reports, parser fixes for new firmware, dashboard improvements all
welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for what info to include
in an issue, the test/lint expectations, and what kinds of changes I'll
likely push back on. Project conduct is in
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) -- short version: be civil, keep
technical discussion technical.

## Firmware downloads

Firmware tarballs and the spec/manual PDFs are on the
[Releases](https://github.com/Strykar/GPON/releases) page:

| File | Variant | Date | Status |
| --- | --- | --- | --- |
| `M110_sfp_ODI_220923_SFU.tar` | M110 SFU V1.0-220923 | 2022-09-23 | Verified end-to-end |
| `M110_sfp_HSGQ_SFU_240408.tar` | M110 SFU V1.1.8-240408 | 2024-04-08 | Untested |
| `M114_sfp_ODI_231021_HGU.tar` | M114 HGU V1.7.1-231021 | 2023-10-21 | Untested |
| `V1.1.3_sfp_HSGQ_HGU_250620.tar` | HGU V1.1.4-250620 | 2025-06-20 | Untested |
| `*.pdf` | Spec sheets, user manual, ONU activation paper | n/a | -- |
