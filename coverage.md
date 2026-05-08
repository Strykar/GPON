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
| `diag pon get transceiver tx-power` | `gpon_tx_power_dbm` | Signal Tx power, Laser Tx (1310 nm) |
| `diag pon get transceiver rx-power` | `gpon_rx_power_dbm` | Signal Rx power, Laser Rx (1490 nm) |
| `diag pon get transceiver temperature` | `gpon_temperature_celsius` | SFP SoC Temperature, SoC temperature gauge |
| `diag pon get transceiver voltage` | `gpon_voltage_volts` | SFP voltage |
| `diag pon get transceiver bias-current` | `gpon_bias_current_amperes` | Bias current |
| `diag gpon get onu-state` | `gpon_onu_state` | ONU State |
| `diag gpon get alarm-status` | `gpon_alarm_los`, `_lof`, `_lom`, `_sf`, `_sd`, `_tx_too_long`, `_tx_mismatch` | Alarms |
| `diag gpon show counter global active` | `gpon_activation_sn_requests_total`, `gpon_activation_ranging_requests_total` | Activation events (last 1h) |
| `diag gpon show counter global ds-phy` | `gpon_ds_bip_error_bits_total`, `_blocks_total`, `gpon_ds_fec_correct_codewords_total`, `gpon_ds_fec_uncorrectable_codewords_total` | Downstream BIP errors, FEC corrected codewords, FEC uncorrectable codewords |
| `diag gpon show counter global ds-plm` | `gpon_ds_ploam_received_total`, `_crc_errors_total` | PLOAM messages |
| `diag gpon show counter global ds-bw` | `gpon_ds_bwmap_received_total`, `_crc_errors_total` | Downstream BWMAP |
| `diag gpon show counter global ds-omci` | `gpon_ds_omci_received_total`, `_processed_total` | OMCI message rate |
| `diag gpon show counter global ds-eth` | `gpon_ds_ethernet_unicast_total`, `_multicast_total`, `_fcs_errors_total` | Downstream Ethernet frames |
| `diag gpon show counter global ds-gem` | `gpon_ds_gem_non_idle_total` | Downstream GEM data frame rate |
| `diag gpon show counter global us-plm` | `gpon_us_ploam_transmitted_total` | PLOAM messages (US line) |

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
