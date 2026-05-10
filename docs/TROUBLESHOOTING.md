# Troubleshooting

The first thing to run when something looks wrong:

```sh
python3 gpon_exporter.py --diagnose \
  --device "admin:$ONU_SSH_PASSWORD@192.168.1.1"
```

This connects, prints firmware version, and runs every probe with its raw
output. Paste the result into a bug report.

## Common symptoms

| Log line | Likely cause |
| -------- | ------------ |
| `auth failed` (or `fatal: auth failed ... -- exporter will exit at end of cycle`) | Wrong credentials in `--device user:password@host`. The exporter now exits on auth failure rather than retrying (which would burn auth attempts at the SFP every cycle); systemd's `StartLimitBurst` then trips and stops restarting after 5 attempts in 60s. Fix the credentials and `systemctl restart odi`. Some firmwares lock the account after a few failures; if you've gone past that, reboot the SFP to clear before restarting. |
| `connection refused` | sshd not running on the SFP. Check `/etc/inetd.conf` or that the boot scripts aren't disabling it. |
| `connect timed out` | Routing/IP issue. `ping 192.168.1.1` from the host running the collector. |
| `channel closed by ... mid-fetch` | Another SSH session is holding the slot, or `omci_app` got wedged (see [`--enable-omci` caveat](../README.md#--enable-omci-caveat)). |
| Gauges suddenly 0 (alarms, optical readings, ONU state, sockets) | The OLT just deauthenticated you, or the parser regex missed on a new firmware build. Check `gpon_onu_state` (should be `5`). For Counter-typed metrics (`gpon_*_total`), an SFP reboot rebases via `_AbsoluteCounter` rather than visibly dropping to 0 -- they stay at their last value and the next fetch's increment is from the post-reboot baseline. |
| `gpon_exporter_up == 0` for a host | Last fetch failed; check the collector log for the explanation line. |
| `/metrics` reachable but no `gpon_*` lines | Collector is running but no fetch has completed yet. Wait one `--interval`. |
| Some gauges frozen at one value while others update | Likely a parser regression on a new firmware. Run `--diagnose` and file an issue with the output. |
| Alarms panel stays green during a known fibre flap or unplug | Expected gauge behaviour, not a bug. The alarm gauges read SFP state at scrape time (every `--interval`, default 5 min). An event shorter than the fetch interval lands between two scrapes and is invisible to the gauges. The Activation events panel will catch it (counter-based, accumulates regardless of when we sample). For events longer than `--interval` that we sampled at least once, query `gpon_alarm_*_raises_total` directly -- the exporter increments these Counters on every clear -> raised transition observed and they survive between scrapes. Sub-fetch-interval events are unrecoverable on this firmware -- `diag gpon get alarm-history` doesn't exist, and `omcicli mib getcurr` returns the directory listing instead of PM data on at least M110 V1.0-220923. See QUIRKS for the firmware probe results. |
| `gpon_exporter_fetch_seconds` consistently above ~10s | The SFP is heavily loaded or the link is degraded. |
| Dashboard rate panels show identical step patterns | Prometheus scrape interval is much longer than `--interval`, or rate window is too short. The dashboard uses 15-minute windows by default; raise to `[30m]` for longer collector intervals. |

## Self-health metrics worth alerting on

- `gpon_exporter_up == 0 for 10m`: collector can't reach the SFP.
- `rate(gpon_exporter_fetch_failures_total[15m]) > 0.1`: flapping
  connection. (Real Counter, so `rate()` is the right function here.)
- `max(gpon_alarm_los, gpon_alarm_lof, gpon_alarm_lom, gpon_alarm_sf, gpon_alarm_sd) == 1`:
  any line-side alarm currently raised. Use `max()` rather than `or`,
  since PromQL `or` is set-union on labels, not boolean.
- `sum(rate(gpon_alarm_los_raises_total[1h])) > 0`: at least one LOS
  transition observed in the last hour. Catches medium-duration flaps
  (anything we sampled at least once); won't fire for sub-fetch-interval
  blips. Substitute the alarm key for `los` to alert on other rises.
- `rate(gpon_ds_fec_uncorrectable_codewords_total[15m]) > 0`: FEC can't
  recover everything; expect bit errors upstream.
- `gpon_onu_state != 5`: ONU not in Operation state. `0` means the parser
  couldn't recognise the state output, which usually points at a firmware
  change.

## Recovering from the omci_app wedge

If you turned on `--enable-omci` and the collector starts logging
`channel closed mid-fetch`, the OMCI daemon on the SFP is stuck.
Power-cycle the SFP (unplug and reinsert) and start the collector again
without `--enable-omci`.

Background on the wedge mechanism, why we sequence omcicli before diag,
and which firmwares are suspected of fixing it lives in
[QUIRKS.md](QUIRKS.md#the-omci_app-wedge).
