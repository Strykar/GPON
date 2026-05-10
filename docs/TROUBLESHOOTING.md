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
| All metrics suddenly 0 | Either the SFP rebooted (counters reset) or the OLT just deauthenticated you (check `gpon_onu_state`; should be `5`). |
| `gpon_exporter_up == 0` for a host | Last fetch failed; check the collector log for the explanation line. |
| `/metrics` reachable but no `gpon_*` lines | Collector is running but no fetch has completed yet. Wait one `--interval`. |
| Some gauges frozen at one value while others update | Likely a parser regression on a new firmware. Run `--diagnose` and file an issue with the output. |
| `gpon_exporter_fetch_seconds` consistently above ~10s | The SFP is heavily loaded or the link is degraded. |
| Dashboard rate panels show identical step patterns | Prometheus scrape interval is much longer than `--interval`, or rate window is too short. The dashboard uses 15-minute windows by default; raise to `[30m]` for longer collector intervals. |

## Self-health metrics worth alerting on

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

## Recovering from the omci_app wedge

If you turned on `--enable-omci` and the collector starts logging
`channel closed mid-fetch`, the OMCI daemon on the SFP is stuck.
Power-cycle the SFP (unplug and reinsert) and start the collector again
without `--enable-omci`.

Background on the wedge mechanism, why we sequence omcicli before diag,
and which firmwares are suspected of fixing it lives in
[QUIRKS.md](QUIRKS.md#the-omci_app-wedge).
