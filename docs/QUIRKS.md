# Quirks: HSGQ / ODI GPON SFP and the collector

A running log of non-obvious behaviour discovered while building and
debugging the collector and dashboard. Useful for future-you when the next
firmware revision lands or someone files a confused issue.

## The SFP itself

### Hardware and OS

- Realtek RTL960x SoC, BusyBox userland, Linux 2.6.30 kernel.
- BusyBox 1.12.4 (2022). Sparse: no `tr`, `sort`, `head`, `uname`,
  `find -P` or modern `find` flags. Use absolute commands and pipe-friendly
  one-liners.
- Filesystem layout:
  - `/etc` lives on the squashfs root, effectively read-only.
  - `/var` is `ramfs` (volatile).
  - `/var/config` is `jffs2`, used for persistent device config.
- Default credentials seen: `admin` / hex-string password (vendor-specific,
  varies per device).
- `mib show` exposes the running config including `SUSER_PASSWORD=...`. Do
  not pipe that anywhere public.

### Process layout

- `init` (BusyBox) is PID 1, started from `/etc/inittab`.
- `inetd` listens for SSH and forks `/bin/dropbear -i` per connection.
- `omci_app` is the OMCI daemon (singleton, talks to OLT).
- `boa` is the embedded HTTP server (port 80).
- `igmpd`, `pondetect`, `configd`, `rtkbosa` are the other interesting
  long-running processes.

### Boot scripts

- `/etc/init.d/rcS` runs `/etc/init.d/rc0` through `rc63` in order.
- Notable rc files:
  - `rc2`: mounts `/proc`, `/var`, `/sys`, `/dev`. Sets `PATH=.:PATH`.
  - `rc3`: starts `configd`, `omci_app`, drivers (`runomci.sh`,
    `runlansds.sh`, `runsdk.sh`).
  - `rc10`: TCP keepalive tuning, IP forwarding.
  - `rc32`: starts `rtkbosa` with the BOSA backup config.
  - `rc34`: enables the watchdog.

## SSH and dropbear

- Dropbear runs via inetd (`ssh stream tcp nowait root /bin/dropbear -i` in
  `/etc/inetd.conf`). There is no long-running daemon to kill or
  reconfigure with flags like `-I 0`. Tricks like the Dasan
  `killall dropbear && dropbear -I 0` script don't apply here.
- The device tolerates **only one** concurrent SSH session per client. A
  second connection while the first is still negotiating gets a TCP RST.
- Bare `diag` opens an interactive REPL that streams **about 16 MB of
  output before timing out**. The output is real (alarm dumps, register
  dumps, etc.) but unusable as a probe response. Always pass a full command
  path: `diag gpon get onu-state`, never just `diag`.

### Legacy SSH crypto

The dropbear on this device is from 2008-era source and only speaks
deprecated key-exchange, cipher, and host-key algorithms:

- KEX: `diffie-hellman-group1-sha1` (1024-bit DH + SHA-1)
- Cipher: `3des-cbc`
- Host key: `ssh-rsa` (SHA-1 signed)

Modern OpenSSH disables all three by default and you have to re-enable
them explicitly:

```text
Host odi
    KexAlgorithms +diffie-hellman-group1-sha1
    Ciphers +3des-cbc
    HostKeyAlgorithms +ssh-rsa
```

paramiko's defaults are looser than OpenSSH's and still include these
legacy algorithms, so the collector connects without explicit
configuration. If a future paramiko release tightens defaults further,
the symptom will be `paramiko.SSHException: no matching ... method
found`; the fix is to pass `disabled_algorithms={}` (no-op override) to
`SSHClient.connect()` or configure the `Transport` with an explicit
preferred-list before authentication.

The SFP itself can't speak modern crypto without a firmware that ships
a newer dropbear, which doesn't exist for this hardware. Treat the link
as cleartext on a trusted segment.

### Host-key persistence

The collector ships a `_LoggingHostKeyPolicy` that writes seen
fingerprints to `~/.config/gpon-exporter/known_hosts` (or wherever
`--known-hosts` points). Without persistence, every restart logs "first
contact" because paramiko forgets state. With persistence, a swapped
key produces a `host key changed` WARNING the next time the daemon
connects. The policy is descriptive only -- connections always proceed
-- because RejectPolicy on a homelab SFP would be more annoying than
useful. If you need actual key-change enforcement, swap to
`paramiko.RejectPolicy()` and pre-populate the file by hand.

## The diag CLI

`diag` is the Realtek diagnostic shell. The interesting subtrees:

- `diag pon get transceiver <bias-current|temperature|tx-power|rx-power|voltage>`:
  the optical readouts.
- `diag gpon get <onu-state|alarm-status|pps-cnt|rogue-sd-cnt|...>`: state
  and assorted counters.
- `diag gpon show counter global <category>`: the **gold mine** for
  `/stats.asp` parity. Categories: `active`, `ds-phy`, `ds-plm`, `ds-bw`,
  `ds-omci`, `ds-eth`, `ds-gem`, `us-phy`, `us-plm`, `us-omci`, `us-eth`,
  `us-gem`, `us-dbr`. This is what the Prometheus collector uses for the
  bulk of its metrics.
- `diag mib`: switch/MIB layer, mostly not what you want for GPON state.

### diag oddities

- **`diag gpon get pps-cnt` is reset-on-read.** Each invocation clears the
  underlying counter on the SoC. The collector originally exposed this as
  `gpon_pps_count` but the metric was dropped because no Prometheus query
  is meaningful over a non-monotonic gauge.
- **`diag gpon get alarm` and `diag gpon get alarm-status` produce
  identical output.** Either is fine; pick one. The collector uses
  `alarm-status` because the help text says it's the "current" alarm
  status.
- **The diag REPL prompt leaks into output.** Commands return their data
  followed by a literal `RTK.0> command:` prompt string on the same line.
  The collector parses around it, but if you script directly against
  `diag`, strip that suffix.
- **Most diag subcommands print parse errors as plain stderr-style text on
  stdout.** Example: `diag gpon get` (no subcommand) prints
  `^Incomplete command` (with leading whitespace). Don't grep for "error".

### What `omcicli` does and doesn't do

`omcicli` talks to `omci_app` over a local socket / shared memory.

- `omcicli get loidauth` and `omcicli get authuptime` work and are what
  the collector uses.
- **`omcicli get sn` is broken on V1.0-220923** -- it returns the same
  MIB table-of-contents that `omcicli mib get` does, ignoring the `sn`
  argument. The collector pulls the serial number from the running
  `omci_app` process's argv instead (`ps` -> `omci_app -s <SN>`), which
  is wedge-immune and works on every firmware that runs `omci_app` with
  `-s` (which is all of them). Don't restore `omcicli get sn` as a probe
  unless you've verified it actually works on the target firmware.
- **`omcicli get onuid` and `omcicli get state` do not exist on this
  firmware.** They print the `omcicli get` usage page.
- **`omcicli mib getcurr` and `omcicli mib get` ignore their argument and
  always dump the full MIB table list** (a TOC, not the contents).
  Useless for our purposes.
- **A hung `omcicli` client does NOT wedge `omci_app`**, despite the
  surface-level similarity to the diag-then-omcicli wedge. If a verb
  doesn't return data on this firmware, the omcicli command sits forever
  waiting for a response; the daemon stays healthy. The wedge condition
  documented below is specifically about `omci_app` itself becoming
  unresponsive, not about omcicli hanging.

### The omci_app wedge

The single most consequential quirk we found.

- Running an `omcicli` command immediately after a `diag` command in the
  same shell session, on at least firmware V1.0-220923, **wedges
  `omci_app` for several minutes**. Subsequent `omcicli` calls block
  indefinitely even from a fresh SSH connection.
- The wedge persists across SSH session teardown. Recovery is either to
  wait it out (sometimes minutes) or to power-cycle the SFP.
- **Cross-fetch is empirically safe.** Each fetch ends with `diag`, the
  SSH session closes, ~5 minutes pass, then the next fetch begins with
  `omcicli`. Technically `diag → omcicli` with a multi-minute gap.
  Continuous-runtime testing has not reproduced the wedge in this
  configuration, so the trigger appears to require *same-session,
  immediate* sequencing. The collector relies on this observation. If a
  future firmware turns out to wedge cross-session as well, the existing
  ordering is already wrong (the *next* fetch starts with `omcicli`
  immediately after `diag` in the previous one). The mitigation in that
  case isn't a probe-order flip -- it's stopping `omcicli` from running
  back-to-back-after-`diag` at all, e.g. by separating omci and diag
  fetches into different cycles.
- The collector mitigates this by:
  - Defaulting `--enable-omci` to **off**. With it off, only `diag`
    probes run and the wedge is impossible.
  - When `--enable-omci` is on, sequencing all `omcicli` probes
    **before** any `diag` probes. The reverse order is safe because
    `diag` does not depend on `omci_app`.
- If the collector logs `channel closed mid-fetch` on a fetch with
  `--enable-omci`, the wedge has happened anyway. Stop the collector,
  restart without `--enable-omci`, and power-cycle the SFP if you want the
  omcicli probes back.

## /stats.asp and the boa HTTP server

- The vendor's web UI is served by `boa` on port 80, embedding HTML pages
  baked into the binary (they are not on the filesystem; `find / -name
  '*.asp'` returns nothing).
- Login uses a challenge-based form POST with an `md5.js`-driven hash, not
  basic auth. Scripting the login flow is non-trivial.
- Issue #3 was opened against a web-scraping collector that broke when
  firmware 240408 reshuffled `/stats.asp`. Our SSH/diag-based collector
  doesn't touch `/stats.asp` and shouldn't have that fragility.
- HTTP/0.9 quirks: `curl` without `--http0.9` rejects the server's
  responses. If you script against `boa`, pass `--http0.9` explicitly.

## /proc on the SFP

The SFP runs Linux 2.6.30 with a sane-ish `/proc`, so the standard files
work for system stats. The collector reads:

- `/proc/stat` for CPU (Counter, jiffies/HZ=100, by mode).
- `/proc/meminfo` for RAM (Gauges in bytes; `MemTotal/Free/Buffers/Cached`
  is enough to reproduce the vendor web UI's "Memory Usage %" formula:
  `(Total - Free - Buffers - Cached) / Total`).
- `/proc/uptime` for `gpon_system_uptime_seconds` (distinct from the
  PON-authentication uptime that `omcicli get authuptime` reports).
- `/sys/class/net/eth0/address` for the LAN-side MAC.

### `/proc/loadavg` is bogus on this SoC

`cat /proc/loadavg` returns `2.00 2.00 2.00 1/64 N` -- all three averages
exactly 2.00 regardless of actual load, observed across days of uptime.
The kernel on this Realtek MIPS build either doesn't update the load
counter or has it pinned to whatever it was at boot. We don't expose a
`gpon_load_average` metric for that reason. Use the per-mode CPU counter
(`rate(gpon_cpu_seconds_total{mode!="idle"}[5m])`) instead, which works.

### BusyBox `head` and similar are missing

BusyBox 1.12.4 on this firmware is sparse. Reading the first line of
`/proc/stat` with `head -1` returns `sh: head: not found`. The collector
handlers parse the full file in Python and pick the line they need
(`startswith('cpu ')`) rather than relying on shell composition.

## Counter and metric oddities

### Counter values

- **`gpon_ds_gem_idle` saturates at 2^32-1 within hours of uptime.** The
  underlying counter wraps or sticks. The dashboard charts only
  `gpon_ds_gem_non_idle` as the link-utilisation indicator.
- **All `gpon_us_omci_*` metrics except `gpon_us_omci_transmitted` are
  flat** on this firmware. The metrics are emitted but not charted.
- **All device counters update simultaneously, once per
  `--interval`.** Between fetches, every gauge is constant. This produces
  a synchronised step pattern across all rate panels: identical timing,
  different magnitudes. Solution: use a rate window at least 2-3× the
  fetch interval (the dashboard uses 15-minute windows for a 5-minute
  default `--interval`).
- **Some counters reset on device reboot** (downward step in the gauge).
  This is normal counter-reset behaviour; the dashboard's
  `clamp_min(delta(...), 0)` swallows the negative and continues.

### ONU state output format

- Literal device output: `ONU state: Operation State(O5)`.
- The state code is a **letter `O`** followed by a digit, not zero-five.
  See <https://hack-gpon.org/gpon-auth/> for the ITU-T G.984.3 state
  meanings (O1 Initial, O2 Standby, O3 Serial Number, O4 Ranging, O5
  Operation, O6 Intermittent LODS, O7 Emergency Stop).
- The collector regex `\(O(\d)\)` extracts the digit. If the device emits
  a state outside O1–O7 the gauge is set to `0` (sentinel for "parser
  did not recognise") and a DEBUG log is written. Don't interpret a `0`
  reading as state O0 (there is no such state); it means firmware drift.

### Alarm output format

- Literal output of `diag gpon get alarm-status`:

  ```text
  Alarm LOS, status: clear
  Alarm LOF, status: clear
  Alarm LOM, status: clear
  Alarm SF, status: clear
  Alarm SD, status: clear
  Alarm TX Too Long, status: clear
  Alarm TX Mismatch, status: clear
  ```

- Status is `clear` or some non-`clear` token (we have not observed a
  raised alarm to know the exact wording for "raised"). The collector
  treats any non-`clear` value as `1`.
- Severity, worst first: LOS catastrophic, LOF/LOM/SF severe, SD warning,
  TX Too Long / TX Mismatch are config warnings.

### Per-fetch CPU and timing

- A full fetch takes **about 3.2 seconds wall-clock** (22 SSH channels,
  one per probe, mostly I/O wait on the device's slow SSH).
- Actual collector-process CPU is a small fraction of that wall time. Paramiko spends most of its budget in `select()`. Steady-state CPU
  averaged across fetch and idle is roughly 0.02–0.03% of one core, which
  matches `process_cpu_seconds_total` rate observations.

## Collector design choices and why

### Cumulative counters as real Counters via `_AbsoluteCounter`

- `prometheus_client`'s `Counter` class only supports `.inc(delta)`, not
  `.set(absolute_value)`. The device, however, only exposes absolute
  running totals. To keep the metric type correct (Prometheus Counter,
  `_total` suffix, `rate()`-friendly) we wrap `Counter` in an
  `_AbsoluteCounter` adapter that lets handlers call
  `.labels(ip=).set(absolute)` the same way they would on a Gauge. The
  adapter remembers the previous absolute per label tuple, computes the
  delta, and feeds `Counter.inc(delta)`.
- Counter resets (SFP reboot, observed as a downward step in the absolute
  value) are detected and skipped: we rebase to the new baseline rather
  than back-decrementing the Prometheus counter. `rate()` then sees a
  clean monotonic series and the next fetch's increment is computed
  against the post-reboot value.
- This replaced an earlier design that exposed cumulative values as
  `Gauge` and used `clamp_min(delta(metric[15m]), 0) / 900` in the
  dashboard. The Counter design is type-correct, removes the
  "metric might not be a counter" info hint in Grafana, and lets every
  panel use plain `rate(metric_total[15m])`.

### One SSH connection per fetch, channel per probe

- We tried stitching every probe into one `client.exec_command(big script)`
  to avoid the per-probe channel handshake. It works for diag-only
  sequences but the moment an `omcicli` follows a `diag` in the same
  shell, we hit the omci_app wedge described above. Per-probe channels
  reuse the same SSH connection (same auth, same TCP), avoiding the
  per-connection auth tax while keeping each probe in its own shell.

### `--enable-omci` off by default

- Three useful but optional metrics: `gpon_pon_uptime_seconds`, the LOID
  auth state, and the SFP serial-number Info. They come from `omcicli`
  probes that, on at least V1.0-220923, can wedge `omci_app` if interleaved
  with `diag` (see above). Off-by-default is the safe choice; users who
  understand the trade-off can opt in.

### Persistent SSH not implemented

- Suggested in passing as an optimisation (open one SSH connection at
  startup and reuse it forever, saving ~500 ms of auth per fetch). For a
  5-minute interval the savings are cosmetic, and a long-lived connection
  brings keepalive, dead-channel detection, and reconnect logic. Skipped.

## Dashboard design choices and why

### `rate(metric_total[15m])` for cumulative counters

- Counters are exposed as proper Prometheus `Counter` type via the
  `_AbsoluteCounter` wrapper (see "Cumulative counters as real Counters"
  above), so plain `rate()` is correct and reset-aware. The dashboard
  used `clamp_min(delta(metric[15m]), 0) / 900` in the pre-v1.0.0 design
  when the same metrics were Gauges; that workaround is gone.

### 15-minute rate windows

- The collector defaults to fetching every 5 minutes. A 15-minute window
  contains 2-3 fetches' worth of deltas, smoothing out the synchronised
  step pattern that a `[5m]` window produces. If you change `--interval`,
  scale the window accordingly.

### `sum(increase(...[1h]))` on activation events

- The activation-events stat panel uses `sum(increase(...[1h]))` rather
  than bare `increase(...[1h])`. The wrapper folds any historical series
  with a different label set into a single cell, which matters during
  exporter-restart migrations: a renamed/added label leaves the previous
  series queryable until retention ages it out, and a `[1h]` lookback
  scoops both the stale and current series and renders them side-by-side.
  Safe given the single-device-per-exporter `$instance` filter; multi-
  device-per-exporter setups want `sum by(ip)` instead.

### Tx/Rx power as threshold-banded timeseries, not heatmap

- We tried a heatmap reimagining of the PON Tx/Rx Power panel. Looked
  great for Tx (some natural variance, distribution visible) and broke
  ugly on Rx (with all samples at one identical value, Grafana fills the
  whole y-range with cells, presenting as a solid block). The threshold-
  banded timeseries works on any data shape and immediately telegraphs
  "is the link inside spec or not?"

### Activation churn as a stat, not timeseries

- `gpon_activation_sn_requests` and `_ranging_requests` are flat zero in
  healthy operation. A timeseries chart of two flat zero lines is
  uninformative. A stat panel ("0 events" green, non-zero red) conveys
  the same thing more compactly and gets attention when something fires.

### Mixed-magnitude series on a secondary y-axis

- `Downstream Ethernet frames/sec`, `Downstream BWMAP/sec`, and
  `PLOAM messages/sec` chart traffic counts (thousands/sec) and error
  counts (typically 0) on the same panel. Auto-scaling y-axis hides the
  errors at the bottom. Pinning the error series to the right y-axis
  gives them their own scale, so a single FCS error is visible against
  thousands of unicast frames per second.

## Verified firmware

| Build | Variant | Date | Status |
| --- | --- | --- | --- |
| `V1.0-220923` | M110 SFU | 2022-09-23 | Verified end-to-end |
| `M110_sfp_HSGQ_SFU_240408.tar` | M110 SFU | 2024-04-08 | Untested, expected to work (issue #3 build) |
| `M114_sfp_ODI_231021_HGU.tar` | M114 HGU | 2023-10-21 | Untested |
| `V1.1.3_sfp_HSGQ_HGU_250620.tar` | HGU V1.1.3 | 2025-06-20 | Untested |

## Things still worth investigating

- Whether the `omci_app` wedge is fixed in the 240408 / V1.1.3 firmwares.
  If yes, `--enable-omci` could plausibly become default-on for those
  builds.
- Whether `gpon_ds_gem_idle` saturation is behaviour or bug. A 64-bit
  counter (or counter-rollover detection on the device) would let us
  compute a real idle/non-idle ratio.
- Whether `omcicli mib getcurr` works in any form on any firmware. If it
  ever does, we get FEC PMHD (ME 312) and BIP / HEC PM data via the
  proper OMCI G.984.4 PMHD interface, more semantically clean than
  scraping diag counter dumps.
