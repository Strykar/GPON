# Migration

## v0.x → v1.0.0

If you ran the pre-v1.0.0 collector (then named `hsgq_prometheus_collector`),
the renames below will break any external alerting rules, recording rules,
or dashboard forks. The shipped `dashboard.json` is migrated; only your
own copies need attention.

| Old | New |
| --- | --- |
| `gpon_collector_*` self-metrics | `gpon_exporter_*` |
| `gpon_bias_current_mA` | `gpon_bias_current_amperes` (value scaled, not just renamed) |
| Cumulative counters (`gpon_ds_*`, `gpon_us_*`, `gpon_activation_*`, `gpon_rogue_sd_*`) | Same names with `_total` suffix; type changed from Gauge to Counter. Use `rate(metric_total[15m])`. |
| `--hostname H --port N --user U --password P` (positional, repeatable) | `--device user:password@host[:port]` (URL-form, repeatable) |
| `GPON_PASSWORD` env var | `ONU_SSH_PASSWORD` |
| Default bind address `0.0.0.0` | `127.0.0.1` (use `--bind-address 0.0.0.0` to opt back in) |

The same table is on the
[v1.0.0 release page](https://github.com/Strykar/GPON/releases/tag/v1.0.0)
for users who land there first.
