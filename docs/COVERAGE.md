# Dashboard metric coverage

The dashboard pulls device data from two on-SFP sources:

- **`diag`** — Realtek's diagnostic CLI. Always available; no special config.
- **`omcicli`** — the OMCI client that talks to the on-device `omci_app`
  daemon. Only invoked when the exporter is started with `--enable-omci`,
  which is **off by default** because of the `omci_app` wedge documented
  in [QUIRKS.md](QUIRKS.md).

Self-health metrics (`gpon_exporter_*`, `process_*`) come from the
exporter process itself, not the SFP. They are listed in [README.md](README.md#self-health-metrics)
and not duplicated here.

## Metrics from `diag`

| Probe command | Metric(s) | Dashboard panel(s) |
| --- | --- | --- |
| `cat /etc/version` | `gpon_firmware_info{version}` | Firmware |
| `cat /proc/stat` | `gpon_cpu_seconds_total{mode}` | SFP CPU |
| `cat /proc/meminfo` | `gpon_memory_total_bytes`, `_free_bytes`, `_buffers_bytes`, `_cached_bytes` | SFP RAM |
| `cat /proc/uptime` | `gpon_system_uptime_seconds` | (not charted yet) |
| `cat /sys/class/net/eth0/address` | `gpon_mac_info{mac}` | SFP MAC |
| `diag pon get transceiver tx-power` | `gpon_tx_power_dbm` | Signal Tx power, Laser Tx (1310 nm) |
| `diag pon get transceiver rx-power` | `gpon_rx_power_dbm` | Signal Rx power, Laser Rx (1490 nm) |
| `diag pon get transceiver temperature` | `gpon_temperature_celsius` | SFP (commercial) SoC temperature, SoC temperature (gauge) |
| `diag pon get transceiver voltage` | `gpon_voltage_volts` | SFP voltage |
| `diag pon get transceiver bias-current` | `gpon_bias_current_amperes` | Bias current |
| `diag gpon get onu-state` | `gpon_onu_state` | ONU state |
| `diag gpon get alarm-status` | `gpon_alarm_los`, `_lof`, `_lom`, `_sf`, `_sd`, `_tx_too_long`, `_tx_mismatch` | Alarms |
| `diag gpon show counter global active` | `gpon_activation_sn_requests_total`, `gpon_activation_ranging_requests_total` | Activation events (last 1h) |
| `diag gpon show counter global ds-phy` | `gpon_ds_bip_error_bits_total`, `_blocks_total`, `gpon_ds_fec_correct_codewords_total`, `gpon_ds_fec_uncorrectable_codewords_total` | Downstream BIP errors/sec, FEC corrected codewords/sec, FEC uncorrectable codewords/sec |
| `diag gpon show counter global ds-plm` | `gpon_ds_ploam_received_total`, `_crc_errors_total` | PLOAM messages/sec (DS series) |
| `diag gpon show counter global ds-bw` | `gpon_ds_bwmap_received_total`, `_crc_errors_total` | Downstream BWMAP/sec |
| `diag gpon show counter global ds-omci` | `gpon_ds_omci_received_total`, `_processed_total` | OMCI message rate |
| `diag gpon show counter global ds-eth` | `gpon_ds_ethernet_unicast_total`, `_multicast_total`, `_fcs_errors_total` | Downstream Ethernet frames/sec |
| `diag gpon show counter global ds-gem` | `gpon_ds_gem_non_idle_total` | Downstream GEM data frame rate |
| `diag gpon show counter global us-plm` | `gpon_us_ploam_transmitted_total` | PLOAM messages/sec (US transmitted series) |

## Metrics from `omcicli`

| Probe command | Metric(s) | Dashboard panel(s) |
| --- | --- | --- |
| _none_ | _none_ | _none_ |

The default dashboard does not chart any `omcicli`-sourced metrics. The
exporter still emits the following when `--enable-omci` is passed; you can
add panels for them if your firmware tolerates the wedge condition (see
[QUIRKS.md](QUIRKS.md#the-omci_app-wedge)):

| Probe command | Metric(s) emitted |
| --- | --- |
| `omcicli get authuptime` | `gpon_pon_uptime_seconds` |
| `omcicli get loidauth` | `gpon_loid_auth_status`, `gpon_loid_auth_attempts`, `gpon_loid_auth_success` |
| `omcicli get sn` | `gpon_device_info{serial_number}` |

## Other diag metrics emitted but not charted

A handful of `diag`-sourced metrics are exposed at `/metrics` but not on
the default dashboard, mostly because they're flat zero or otherwise
uninformative on a healthy link. Listed for completeness:

- `diag gpon get rogue-sd-cnt`: `gpon_rogue_sd_too_long_total`, `gpon_rogue_sd_mismatch_total`
- `diag gpon show counter global ds-phy` (additional): `gpon_ds_fec_correct_bits_total`, `_bytes_total`, `gpon_ds_superframe_los_total`, `gpon_ds_plen_fail_total`, `_correct_total`
- `diag gpon show counter global ds-plm` (additional): `gpon_ds_ploam_processed_total`, `_overflow_total`, `_unknown_total`
- `diag gpon show counter global ds-bw` (additional): `gpon_ds_bwmap_overflow_total`, `_invalid0_total`, `_invalid1_total`
- `diag gpon show counter global ds-omci` (additional): `gpon_ds_omci_bytes_total`, `_dropped_total`, `_crc_errors_total`
- `diag gpon show counter global ds-eth` (additional): `gpon_ds_ethernet_multicast_forwarded_total`, `_multicast_leaked_total`
- `diag gpon show counter global ds-gem` (additional): `gpon_ds_gem_idle_total` (saturates at 2^32-1, see QUIRKS), `_los_total`, `_over_interleave_total`, `_mis_packet_length_total`, `_multi_flow_match_total`, `gpon_ds_hec_correct_total`
- `diag gpon show counter global us-phy`: `gpon_us_boh_total`
- `diag gpon show counter global us-plm` (additional): `gpon_us_ploam_processed_total`, `_urgent_total`, `_urgent_processed_total`, `_normal_total`, `_normal_processed_total`, `_serial_number_total`, `_nomsg_total`
- `diag gpon show counter global us-omci`: `gpon_us_omci_processed_total`, `gpon_us_omci_transmitted_total`, `gpon_us_omci_bytes_total`
- `diag gpon show counter global us-gem`: `gpon_us_gem_blocks_total`, `_bytes_total`
- `diag gpon show counter global us-dbr`: `gpon_us_dbr_total`

## Dashboard layout

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
  the parser's "I don't recognise this output" sentinel -- read it as
  "firmware drift", not "state O0".
- **Alarms**: seven cells, one per alarm bit. Green `OK` = clear, red
  `RAISED` = active. Severity, worst first: Loss of Signal (catastrophic),
  Loss of Frame / Loss of MAC / Signal Fail (severe), Signal Degraded
  (warning), TX Too Long / TX Mismatch (config issues).
- **Activation events (last 1h)**: `sum(increase(gpon_activation_sn_requests_total[1h])) +
  sum(increase(gpon_activation_ranging_requests_total[1h]))`. The `sum()`
  wrapper folds any historical series with a different label set into a
  single cell -- needed because exporter restarts that change labels leave
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
- **CPU**: `100 * rate(process_cpu_seconds_total[5m])` -- converts seconds
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
  upstream -- a real alert signal.
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
  (see [QUIRKS](QUIRKS.md)).
- **OMCI message rate** (`DS received`, `DS processed`): downstream OMCI
  management message rate. Healthy steady-state is ~10 msg/s.

### Collector health (collapsed)

Self-metrics about the exporter process, not the SFP. Useful to confirm
"is the collector working?" before assuming the SFP is the problem.

- **Collector status**: `gpon_exporter_up`. 1 = last fetch succeeded.
- **Firmware**: `gpon_firmware_info`'s `version` label, displayed via
  `legendFormat={{version}}`.
- **Last fetch**: `time() - gpon_exporter_last_fetch_timestamp` -- seconds
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
moment, once per fetch -- see [QUIRKS](QUIRKS.md) for the timing detail).
If you change `--interval`, scale the window proportionally.

### `sum(increase(...[1h]))` on the activation panel

Wrapping `increase()` in `sum()` collapses any stale time series with a
different label set into a single cell. Without it, an exporter restart
that changes labels (or adds an `ip` value) leaves the previous series
queryable for the retention window -- and a `[1h]` lookback scoops both up
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

`process_*` metrics come from prometheus_client and exist for _any_
exporter Prometheus scrapes, so they need `job="gpon_exporter"` to
disambiguate. `gpon_*` metric names are unique to this exporter, so the
filter is redundant noise on those queries and we don't include it.

### Activation churn as a stat, not timeseries

`gpon_activation_sn_requests_total` and `_ranging_requests_total` are flat
zero in healthy operation. A timeseries chart of two flat zero lines is
uninformative. A stat panel ("0 events" green, non-zero red) conveys the
same thing more compactly and gets attention only when something fires.

## Metric reference (by category)

About 75 metrics in total. The probe → metric → panel mapping above is
the source of truth; this section organises the same set by category for
readers who want a quick "what kinds of things does this expose?".

All cumulative device counters carry the `_total` suffix and Prometheus
type `counter`. Gauges, Info, and the temperature/voltage/optical readouts
do not.

### Optical readouts (Gauge)

| Metric | What it is |
| --- | --- |
| `gpon_temperature_celsius` | SFP SoC temperature |
| `gpon_voltage_volts` | Supply voltage |
| `gpon_tx_power_dbm` | Tx optical power |
| `gpon_rx_power_dbm` | Rx optical power |
| `gpon_bias_current_amperes` | Laser bias current. Device reports mA; the parser scales to amperes for Prometheus base-unit convention. |

### State and alarms (Gauge)

| Metric | What it is |
| --- | --- |
| `gpon_onu_state` | 1=O1 Initial .. 5=O5 Operation .. 7=O7 Emergency Stop. `0` means the parser didn't recognise the state output (probable firmware change). |
| `gpon_alarm_los`, `_lof`, `_lom`, `_sf`, `_sd`, `_tx_too_long`, `_tx_mismatch` | Alarm gauges. `1` = raised, `0` = clear. |

### Downstream PHY counters (Counter)

| Metric | What it counts |
| --- | --- |
| `gpon_ds_bip_error_bits_total` | BIP-8 error bits detected downstream |
| `gpon_ds_bip_error_blocks_total` | BIP-8 error blocks detected |
| `gpon_ds_fec_correct_bits_total` | Bits corrected by FEC |
| `gpon_ds_fec_correct_bytes_total` | Bytes corrected by FEC |
| `gpon_ds_fec_correct_codewords_total` | Codewords FEC successfully corrected |
| `gpon_ds_fec_uncorrectable_codewords_total` | Codewords FEC could not recover |
| `gpon_ds_superframe_los_total` | Superframe-level loss-of-signal events |
| `gpon_ds_plen_fail_total` | Packet-length validation failures |
| `gpon_ds_plen_correct_total` | Packet-length validations passed |

### Downstream PLOAM (Counter)

| Metric | What it counts |
| --- | --- |
| `gpon_ds_ploam_received_total` | PLOAM messages received |
| `gpon_ds_ploam_crc_errors_total` | PLOAM messages with CRC failures |
| `gpon_ds_ploam_processed_total` | PLOAM messages handed off to upper layers |
| `gpon_ds_ploam_overflow_total` | PLOAM queue overflow events |
| `gpon_ds_ploam_unknown_total` | PLOAM messages of unrecognised type |

### Downstream BWMAP (Counter)

| Metric | What it counts |
| --- | --- |
| `gpon_ds_bwmap_received_total` | Bandwidth-map messages from the OLT |
| `gpon_ds_bwmap_crc_errors_total` | BWMAP messages with CRC failures |
| `gpon_ds_bwmap_overflow_total` | BWMAP queue overflow events |
| `gpon_ds_bwmap_invalid0_total` | Invalid-type-0 BWMAP entries |
| `gpon_ds_bwmap_invalid1_total` | Invalid-type-1 BWMAP entries |

### Downstream OMCI (Counter)

| Metric | What it counts |
| --- | --- |
| `gpon_ds_omci_received_total` | OMCI messages received |
| `gpon_ds_omci_bytes_total` | OMCI bytes received |
| `gpon_ds_omci_processed_total` | OMCI messages processed |
| `gpon_ds_omci_dropped_total` | OMCI messages dropped |
| `gpon_ds_omci_crc_errors_total` | OMCI CRC failures |

### Downstream Ethernet (Counter)

| Metric | What it counts |
| --- | --- |
| `gpon_ds_ethernet_unicast_total` | Unicast frames |
| `gpon_ds_ethernet_multicast_total` | Multicast frames |
| `gpon_ds_ethernet_multicast_forwarded_total` | Multicast frames forwarded by the SFP |
| `gpon_ds_ethernet_multicast_leaked_total` | Multicast frames not forwarded as expected |
| `gpon_ds_ethernet_fcs_errors_total` | FCS errors |

### Downstream GEM and HEC (Counter)

| Metric | What it counts |
| --- | --- |
| `gpon_ds_gem_idle_total` | Idle GEM frames (saturates at 2^32-1, see [QUIRKS](QUIRKS.md)) |
| `gpon_ds_gem_non_idle_total` | Data-carrying GEM frames; the link-utilisation indicator |
| `gpon_ds_gem_los_total` | GEM-level loss-of-signal events |
| `gpon_ds_gem_over_interleave_total` | GEM over-interleave events |
| `gpon_ds_gem_mis_packet_length_total` | GEM packet-length mismatches |
| `gpon_ds_gem_multi_flow_match_total` | GEM multi-flow match events |
| `gpon_ds_hec_correct_total` | HEC corrections applied |

### Upstream (Counter)

| Metric | What it counts |
| --- | --- |
| `gpon_us_boh_total` | Burst overhead transmitted |
| `gpon_us_dbr_total` | Dynamic bandwidth report messages |
| `gpon_us_ploam_transmitted_total` | Upstream PLOAM messages transmitted |
| `gpon_us_ploam_processed_total` | Upstream PLOAM messages processed |
| `gpon_us_ploam_urgent_total` | Urgent PLOAM messages |
| `gpon_us_ploam_urgent_processed_total` | Urgent PLOAM messages processed |
| `gpon_us_ploam_normal_total` | Normal PLOAM messages |
| `gpon_us_ploam_normal_processed_total` | Normal PLOAM messages processed |
| `gpon_us_ploam_serial_number_total` | Serial-number PLOAM messages |
| `gpon_us_ploam_nomsg_total` | "No message" upstream PLOAM slots |
| `gpon_us_omci_transmitted_total` | Upstream OMCI messages transmitted |
| `gpon_us_omci_processed_total` | Upstream OMCI messages processed |
| `gpon_us_omci_bytes_total` | Upstream OMCI bytes |
| `gpon_us_gem_blocks_total` | Upstream GEM blocks |
| `gpon_us_gem_bytes_total` | Upstream GEM bytes |

### Activation and rogue (Counter)

| Metric | What it counts |
| --- | --- |
| `gpon_activation_sn_requests_total` | Serial-number requests from the OLT |
| `gpon_activation_ranging_requests_total` | Ranging requests from the OLT |
| `gpon_rogue_sd_too_long_total` | "SD too long" rogue detections |
| `gpon_rogue_sd_mismatch_total` | "SD mismatch" rogue detections |

### `--enable-omci` extras (Gauge / Info)

| Metric | Type | What it is |
| --- | --- | --- |
| `gpon_pon_uptime_seconds` | Gauge | Authenticated PON uptime in seconds |
| `gpon_loid_auth_status` | Gauge | LOID authentication state (1 = authenticated) |
| `gpon_loid_auth_attempts` | Gauge | LOID authentication attempts |
| `gpon_loid_auth_success` | Gauge | LOID authentication successes |
| `gpon_device_info{serial_number=...}` | Info | SFP serial number, exposed as a label |

### SFP system stats (Counter / Gauge)

Sourced from `/proc/stat`, `/proc/meminfo`, and `/proc/uptime` on the
SFP. CPU is exposed as a Counter so `rate()` gives per-mode utilisation
the same way node_exporter does. Memory is split into the four fields
the vendor web UI uses to compute its "Memory Usage %" number:
`100 * (Total - Free - Buffers - Cached) / Total`.

| Metric | Type | What it is |
| --- | --- | --- |
| `gpon_cpu_seconds_total{mode=...}` | Counter | CPU jiffies/HZ on the SFP, by mode (`user`, `nice`, `system`, `idle`, `iowait`, `irq`, `softirq`). HZ assumed 100 (kernel default for the Realtek MIPS 2.6.30 build). |
| `gpon_memory_total_bytes` | Gauge | Total RAM (`MemTotal` from /proc/meminfo, scaled to bytes) |
| `gpon_memory_free_bytes` | Gauge | Free RAM (`MemFree`) |
| `gpon_memory_buffers_bytes` | Gauge | Buffer cache (`Buffers`) |
| `gpon_memory_cached_bytes` | Gauge | Page cache (`Cached`) |
| `gpon_system_uptime_seconds` | Gauge | Seconds since the SFP booted. Distinct from `gpon_pon_uptime_seconds`, which is PON authentication uptime. |
| `gpon_mac_info{mac=...}` | Info | LAN-side MAC of the SFP (eth0), from `/sys/class/net/eth0/address`. Matches the vendor web UI's "MAC Address" field. |

### Self-health metrics

| Metric | Type | What it is |
| --- | --- | --- |
| `gpon_exporter_up` | Gauge | `1` if last fetch succeeded, `0` if it failed. Per device. |
| `gpon_exporter_fetch_seconds` | Gauge | Wall-clock seconds for the last fetch attempt. |
| `gpon_exporter_last_fetch_timestamp` | Gauge | Unix timestamp of the last successful fetch. |
| `gpon_exporter_fetch_failures_total` | Counter | Fetch failures since the daemon started. |
| `gpon_firmware_info{version=...}` | Info | Firmware version captured from the SFP's `/etc/version`. |
| `gpon_exporter_info{version=...}` | Info | This exporter's version (pulled from `__version__` in the source so it can't drift from CI/Docker pins). |
