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
- **`/var/config/lastgood.xml` and `lastgood_hs.xml` store the same secrets
  in plaintext** -- `SUSER_PASSWORD`, `E8BDUSER_PASSWORD`, `LOID_PASSWD`,
  and `GPON_PLOAM_PASSWD` all sit there as `<Value Name="..." Value="..."/>`
  entries, alongside genuinely useful config (`WAN_MODE`, `OMCC_VER`,
  VLAN settings). The file is the persistent backing store for `mib show`.
  Do not write a probe that reads this file as a generic key/value source;
  the failure mode of a careless `<Value>` regex is to publish those
  passwords as a Prometheus label, which then ends up in your scrape
  history and any Grafana snapshot. If a future probe genuinely needs one
  field from here, parse for *that single field by name* and discard
  everything else, with a unit test asserting no labelled metric carries
  one of the known-secret field names.

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
contact" because paramiko forgets state. With persistence:

- **First contact** to a new host: WARNING is logged and the
  fingerprint is recorded in the store.
- **Subsequent connect with a different key**: WARNING is logged but
  the on-disk fingerprint is **not** updated. Every fetch and every
  daemon restart re-emits the warning until the operator hand-edits
  (or deletes) the known-hosts file. This is the C1-fix shape -- an
  earlier version of the code persisted the new fingerprint
  unconditionally, which silently ratified key swaps after a single
  warning.

**Honest scope**: the policy is descriptive, not enforcing. Connections
still proceed in both branches above (we let them through because
`RejectPolicy` on a homelab SFP would be more annoying than useful), so
on an active MITM with a swapped key the password is still sent to the
attacker on every fetch. What the fix gives you is a **durable signal**
in the journal -- an operator can grep the log and notice. For real
key-change enforcement, swap `_LoggingHostKeyPolicy` for
`paramiko.RejectPolicy()` in the source and pre-populate the file by
hand. A `--strict-host-keys` flag wiring this in is on the
deferred-improvements list.

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
- **`omcicli mib getcurr`, `omcicli mib get`, and `omcicli mib dump`
  ignore their argument and always dump the full MIB table list** (a
  TOC, not the contents). Useless for our purposes. Verified across
  numeric class IDs, table names, comma syntax, and explicit `entityId`
  -- every form falls through to the directory listing. `omcicli mib
  getattr` is the only verb that doesn't fall through; it instead
  returns an empty error and no data. **This closes the OMCI route for
  Performance Monitoring (PM) accumulations on this firmware** -- the
  PM tables (`FecPmhd`, `EthPmHistoryData`, `Anig`, etc.) exist in the
  TOC but their contents are unreachable via `omcicli`. The exporter's
  `gpon_alarm_*_raises_total` Counters compensate by tracking gauge
  transitions in-process; sub-fetch-interval events remain firmware-
  invisible (see TROUBLESHOOTING).
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
- **Separate-session probing is also empirically safe.** Targeted
  discovery probes -- ~10 `omcicli mib` invocations across two
  back-to-back interactive SSH sessions while the production
  exporter's diag-driven fetches were still running on a 5-minute
  cycle -- did not wedge `omci_app`. Production fetches landed cleanly
  in 3.82-3.84s before, during, and after the probe sweep. Strengthens
  the "same-session, immediate" hypothesis above; it doesn't license
  enabling `omcicli` probes back-to-back in the loop, because the same
  session is the failure mode.
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

## Dual-slot firmware images and rollback

The SFP carries TWO firmware images in flash and can boot from either,
selected by the bootloader at every boot. The MTD layout (from `cat
/proc/mtd`):

```text
mtd0  "boot"    256K  u-boot
mtd1  "env"     8K    bootloader env (primary)
mtd2  "env2"    8K    bootloader env backup
mtd3  "config"  240K  /var/config jffs2
mtd4  "k0"      1.3M  kernel slot 0     \
mtd5  "r0"      2.5M  rootfs slot 0     /  slot 0 firmware
mtd6  "k1"      1.3M  kernel slot 1     \
mtd7  "r1"      2.5M  rootfs slot 1     /  slot 1 firmware
mtd12 "linux"   1.3M  bootloader-mapped active kernel
mtd13 "rootfs"  2.5M  bootloader-mapped active rootfs
```

The active firmware version of each slot is recorded in env variables
`sw_version0` and `sw_version1`. **The variable the bootloader reads to
pick a slot is `sw_commit`, not `sw_active`.** `sw_active` is a *status*
variable -- the bootloader writes it during boot to reflect which slot
actually got loaded. From `mtd1`:

```text
boot_by_commit=if itest.s ${sw_commit} == 0;then run set_act0;run b0;else run set_act1;run b1;fi
```

So **the correct manual rollback CLI is**:

```sh
nv setenv sw_commit 0 && reboot   # boot slot 0 (the older image)
nv setenv sw_commit 1 && reboot   # boot slot 1
```

Setting `sw_active` directly does nothing useful -- the bootloader
overwrites it on next boot. The vendor's web UI image-switch button
sets `sw_commit` for you and is the easier path; the CLI route exists
for headless deployments.

### V1.1.8-240408: data-plane regression localised to userspace OMCI

**Symptom.** ONU reaches O5 cleanly: registered with the OLT, ranged,
all seven alarm gauges clear, GEM port mappings present. But no service
traffic reaches the host. Direct measurement on an ALCL/Alcatel BNG
(Airtel WAN, India): 32,095 frames received at the GEM layer over 15
minutes of V1.1.8 runtime, **1** of which surfaced on Ethernet. PPPoE
and IPoE static both fail at the BNG. LLDP/MNDP from the BRAS shows up
in MikroTik `/tool/torch` (link-local multicast takes a different path
than service unicast), which is what initially made this look like
host-side SerDes when it isn't.

V1.0-220923 in the same fibre and OLT environment delivered 232,260
unicast Ethernet frames in 6 minutes post-rollback. Same BNG, same
fibre, same SFP hardware, working firmware vs broken firmware.

### The diagnostic ladder

For any future "ONU O5, no service traffic" report on this hardware,
walk this ladder top-to-bottom. Each rung **strictly subsumes** the
diagnostic territory of every hypothesis below it; the next person
should not chase any specific theory until they have run all four:

1. **Does the ONU reach O5?** If not, the regression is at the
   ranging / mackey / optical / BOSA / host-side SerDes layer
   (anything that would prevent O5 in the first place).
2. **Is `diag gpon show counter global ds-gem` Non Idle climbing?**
   If yes, the OLT is pushing traffic and frames are reaching the
   GEM layer. Kills every "BNG refuses service profile" / "vendor
   identity mismatch" / "OLT-side provisioning" hypothesis.
3. **Compare `DS GEM Non Idle` to `diag gpon show counter global
   ds-eth` Total Unicast.** If non-idle is in the thousands and
   unicast is near zero, the SFP is dropping BNG traffic between
   GEM de-encapsulation and Ethernet handoff. Localises the bug
   to the GEM → Ethernet path inside the ONU. Kills every OMCI-
   config and OMCI-identity hypothesis, because those would
   either drop at GEM (filter denies) or pass to Ethernet (filter
   permits); they cannot produce a 32000:1 ratio between the two.
4. **Diff the post-hydration ME state between the working and
   broken firmware.** Pull `omcicli mib get` on every VLAN /
   MAC-bridge / EVTO / GEM-mapping ME on both, normalise instance
   numbers, diff. If identical, the bug is not in what the OLT
   pushed or how the ONU received it; it is in what compiled code
   does with the frames afterward.

In this case, rungs 1, 2, 3 confirmed in 30 seconds; rung 4
confirmed in ~5 minutes of dumps. Together they conclusively place
the bug below the OMCI layer, in compiled binary code, with no need
for source.

### Graveyard of six wrong hypotheses

Every line below was a load-bearing theory backed by a real
firmware diff or a real config field. Each ladder rung above
killed at least one. Preserved here in roughly the order they
were generated, with which rung killed them:

| # | Hypothesis | What killed it |
|---|---|---|
| 1 | Missing `mib_Me{242,243,350,370,373}.so` files in V1.1.8 | Rung 4: extracted firmware tarballs show the files present in `/lib/omci/` on both V1.0 and V1.1.8. The runtime `find / -name "mib_Me*.so"` returning empty was a busybox `find` traversal quirk plus my misread. The "MIB_Table_Init Init fail, error code is:1" log we saw means the `.so` loaded and its `init()` returned 1 -- a runtime state error, not a missing file. |
| 2 | `mac_check` / `mackeyVerify` gating the service-profile push | Rung 1+2: web UI shows `Mackey Status: success` and ONU reaches O5. Verification passes. |
| 3 | OMCI "Send alarm notify fail: EthUni" cascade as breaker | Rung 1: was a transient observed during one bad activation. Gone once O5 is reached. |
| 4 | `/etc/runlansds.sh` `LAN_SDS_MODE` → `LAN_SPEED_MODE` flash-key rename | Rung 2 (and a direct trace): `config_xmlconfig.sh -b` runs before `runlansds.sh` in `rc3` and writes the new key from `config_default.xml`. Verified live on V1.1.8: `flash get LAN_SPEED_MODE=0`, `/proc/lan_sds/lan_sds_cfg = mode 1(Fiber 1G)` identical to V1.0. Real diff, theoretical trap, doesn't actually trigger. |
| 5 | EVTO interpretation drift in V1.1.8's rebuilt VLAN-handling `.so` files | Rung 4: OLT-pushed `ExtVlanTagOperCfgData` is byte-identical between V1.0 and V1.1.8 (same 7 INDEX rules, same filters, same treatments). MikroTik already runs Manual → Transparent Mode which bypasses EVTO entirely. |
| 6 | `CircuitPack.Version` / `SwImage.Active.Version` as the BNG's service-profile discriminator | Rung 3 + spoof test: set `OMCI_SW_VER1=V0.9-spooftest` in NVRAM on V1.0, reboot. WAN delivered 232k unicast frames in 6 min with the fake string advertised in both `CircuitPack.Version` and `SwImage.Active.Version`. The BNG does not key off OMCI-reported firmware version. |

### Methodology lesson

The six wrong hypotheses were not a parade of careless guesses --
each one was load-bearing on a real diff, a real config field, a
real ME definition, or a real identity mechanism documented in
G.988. The pattern that produced them is universal to "X changed
between versions, here's a story for how X could matter" reasoning.

The corpus of things that changed between V1.0 and V1.1.8 is
enormous: a userspace daemon got bigger, multiple feature modules
got rebuilt, a new ME plugin appeared, a config-default value
flipped, an init script's flash key was renamed, a vendor-specific
ME got added, a packet-redirect agent was introduced. Every single
one of those was a real change someone could spin a story around.

The corpus of subsystems that can produce a 32000:1 drop between
two adjacent counters in a Linux network stack is small. **Diff-
driven hypotheses are unbounded in count. Counter-driven hypotheses
are bounded by where the counters disagree.** The right theory came
from "I have a 32095:1 ratio, what can produce that?" not from "I
found a thing that changed, here's how it could matter."

### Where the bug actually lives: what we have shown vs what we have inferred

**Shown empirically:**

- Live ME captures on both firmwares after O5 + OLT hydration:
  every VLAN / MAC-bridge / EVTO / GEM-mapping ME sampled
  (`ExtVlanTagOperCfgData`, `VlanTagFilterData`, `MacBriServProf`,
  `MacBriPortCfgData`, `MacBridgePortFilterPreassign`,
  `Map8021pServProf`, `VEIP`, `GemPortCtp`, `GemIwTp`) is
  byte-identical between V1.0 and V1.1.8, modulo a trivial
  wildcard difference in `PrivateVlanCfg.ManualTagVid` (65535
  vs 0; `ManualMode=0` on both, so the value is moot).
- Offline diff of `pf_rtk.ko` (the 120 KB data-plane bridge
  module) via `strings -a` + filtered `readelf -s`: byte-identical
  strings (1541 each, same set), byte-identical symbol table
  (884 each, only difference is compiler-generated `__func__.NNNN`
  counter suffixes that always differ between builds). The kernel
  data plane module is NOT the bug.
- Offline diff of `omcidrv.ko` (the 109 KB OMCI driver): also
  byte-identical strings, also byte-identical symbol table.
  Not the bug.

**What this tells us:** if both the kernel data plane AND the
OMCI MIB state post-hydration are byte-identical between
firmwares, the bug must lie in the **translation between them**
-- something userspace does after OMCI hydration that programs
the kernel differently, or actively interferes with the data
plane at runtime. The bug is therefore in userspace, but the
specific mechanism is not pinned down. Three possibilities,
ordered by plausibility:

1. **The ME-to-kernel translation in `omci_app` and the feature
   modules produces different kernel configuration for identical
   MEs.** `omci_app` hydrates the MIB and configures the kernel
   via ioctls into `pf_rtk` or writes under `/proc/omci/`. If
   V1.1.8 interprets one of the identical MEs differently and
   writes a different forwarding / filter rule, you get identical
   OMCI state but different data-plane behaviour. **Test for the
   next investigator:** dump `/proc/omci/*` post-hydration on
   both versions and diff. V1.0 baseline saved at
   `/tmp/sfp_v1.0_proc_baseline.txt` (captured 2026-05-12). Note:
   the proc nodes are write-probe-read with an unknown verb
   vocabulary; plain `cat` returns empty. Reverse-engineering
   `omci_app` for the verb list is the gating step before any
   meaningful diff is possible.
2. **`/bin/sfpapp` actively filters or consumes data-plane
   frames.** V1.1.8 ships a new 5 KB binary that registers a
   packet-redirect callback (`ptk_redirect_userApp_reg`) in the
   kernel; V1.0 doesn't (the binary is absent, `runlansds.sh`'s
   `sfpapp &` call was a silent no-op). If its filter is too
   broad, it intercepts BNG unicast for vendor-control LOID
   processing and drops or mis-handles it. **Test:**
   `kill -STOP $(pidof sfpapp)` on V1.1.8 and watch
   `diag gpon show counter global ds-eth | grep Unicast` climb.
   ~5 min of WAN downtime, fully reversible with `kill -CONT`.
3. **`/lib/features/internal/me_00001000.so` (new in V1.1.8) does
   something at runtime that affects the data plane.** Strings
   include `no_send_alarm` and `feature_api_register` hooks across
   most MIB tables. Plausible but unfalsifiable without
   instrumentation. Tested only if (1) and (2) come back clean.

**What we have NOT shown:** that any specific userspace component
is the bug. The userspace differences we found (`omci_app` is
1.5 KB larger; every `/lib/features/internal/*.so` grew by 99-259
bytes; `me_00001000.so` added; `mib_ExtendedOnuGZTE.so` added)
are consistent with a regression but also consistent with a
benign global rebuild plus a couple of new features. **Consistent
with is not evidence of.** Without source, the locus is at least
pinned to userspace by the byte-identity of the kernel modules
and the OMCI MIB; the mechanism remains open.

`dmesg | grep -iE "pf_rtk|gem|omci|drop"` is silent on V1.0:
baseline healthy behaviour produces no kernel error messages. If
anyone retries V1.1.8 in the future, the V1.1.8 dmesg should be
the next capture; any output is a signal.

### Sub-finding: ONU identity is fully spoofable from userspace

Useful capability to preserve from the investigation: the chain
`OMCI_SW_VER1` NVRAM key (config store) → `CircuitPack.Version`
(both 0x101 and 0x106 bridge-port entities) → `SwImage.Active.Version`
is **userspace-controllable end-to-end** via the web UI Settings
page, or `mib set OMCI_SW_VER1=...` + reboot. On a fresh V1.0 boot
with `OMCI_SW_VER1=V0.9-spooftest`, the OMCI MIB advertised that
fake string to the OLT through three different MEs simultaneously,
and WAN service continued normally (the spoof test that refuted
hypothesis 6).

The auto-derived SwImage one might expect (read from actual flash
partitions under `/proc/mtd`) does not exist on this firmware.
What's reported via OMCI for slot 0's running version comes from
NVRAM config, not from the slot's actual binary. Useful capability
for any future "the BNG decides who I am based on what I advertise"
debugging on this hardware family, even though it did not help here.

### Operational guidance

**Empirical claim, true here**: V1.1.8-240408 on M110-SFP hardware
against an ALCL/Alcatel BNG (Airtel WAN, India) drops ~99.997% of
downstream unicast at the GEM-to-Ethernet path inside the ONU. WAN
does not work. V1.0-220923 on the same hardware against the same
BNG works without issue.

**Untested claim, plausible**: V1.1.8's `pf_rtk.ko` / userspace OMCI
stack regression may not exercise on every OLT vendor's traffic
patterns. A different OLT might use different GEM port numbers,
priority mappings, or VLAN handling that doesn't hit whatever code
change broke. Discord users have reported V1.1.6-240202 working on
a different OLT vendor entirely. Whether V1.1.8 universally breaks
against all OLTs, or specifically against ALCL-pattern OLTs, would
need data points from other deployments.

For this deployment, stay on V1.0-220923. If you must test a newer
firmware, run the diagnostic ladder above within the first 5 minutes
of activation, and roll back if the ratio looks anything like the
32000:1 we hit. The dual-image architecture (documented above
under "Dual-slot firmware images and rollback") gives a fast path
back: `nv setenv sw_commit OTHER_SLOT && reboot`, or the web UI's
image-switch button if the SFP is responsive.

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
