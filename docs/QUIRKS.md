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
- **`omcicli mib get NAME` and `omcicli mib getcurr CLASSID ENTITYID`
  work in O5.** Earlier revisions of this document claimed they ignore
  their argument and always dump the MIB table directory. That failure
  mode is specific to pre-O5 / broken-activation state. In O5 both
  syntaxes work: `omcicli mib get Anig`, `omcicli mib getcurr 312 0`,
  `omcicli mib dump qmap/conn/srvflow/tasks` all return real data.
  `omcicli mib getattr` is still the only verb that returns an empty
  error on this firmware family.
- **PM accumulation tables (`FecPmhd`, `EthPmHistoryData`,
  `EthPmDataDs/Us`, `GemPortPmhd`, `GpncPmhd`) are queryable but empty
  on every deployment we have data for.** Verified empty on V1.0-220923
  and V1.1.8-240408 against the Airtel/ALCL BNG, and on V1.1.6-240202
  against a different OLT vendor (Discord report). Cause is OLT-side:
  per ITU-T G.988, PM History MEs are not implicit -- the OLT must
  explicitly `create` ME instances before the ONU accumulates anything.
  Most ISP-side OLT configurations don't bother. **Treat the OMCI PM
  route as unavailable in practice on this hardware family.** The
  exporter's `gpon_alarm_*_raises_total` Counters compensate by
  tracking gauge transitions in-process; sub-fetch-interval events
  remain invisible (see TROUBLESHOOTING).
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

### Graveyard of seven wrong hypotheses

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
| 7 | `/bin/sfpapp` packet-redirect callback consuming BNG unicast | Direct STOP test 2026-05-12: booted V1.1.8, captured a 60s RUNNING-arm baseline (ds-gem +10.2k frames, ds-eth Unicast = 0), `kill -STOP 430` confirmed `State: T (stopped)` in `/proc/430/status`, held 300s with sfpapp frozen. STOPPED-arm result: ds-gem +229.7k frames, ds-eth Unicast snapshots `1, 0, 1, 0` -- noise floor, indistinguishable from running. The packet-redirect callback is not the consumer. |

### Methodology lesson

The seven wrong hypotheses were not a parade of careless guesses --
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
  module): instruction stream is **identical** between V1.0 and
  V1.1.8. `objdump -dr` diff produces 0 hunks. The 60 differing
  bytes total are all localised to `.strtab` as compiler
  `__func__.NNNN` counter increments (e.g. `__func__.44472` →
  `__func__.44474`) that bump on every rebuild. The kernel data
  plane module is NOT the bug.
- Offline diff of `omcidrv.ko` (the 109 KB OMCI driver): 1
  differing byte total, at offset 0x197DD in `.rodata` (0x67
  → 0x71). Almost certainly a build-stamp character. Disasm
  identical. Not the bug.
- Offline diff of `librtk.so` (universal Realtek DAL library
  linked by every userspace component): V1.1.8 is 3548 bytes
  larger and exports exactly one new dynamic symbol,
  `dal_rtl9602c_switch_l2_broadcast_macAddr_init` ("dal" =
  Driver Abstraction Layer; rtl9602c = the switch ASIC; the
  function name claims to initialise L2 broadcast MAC handling
  on the switch fabric). XREF check across the entire V1.1.8
  rootfs: zero binaries reference the symbol as an undefined
  import, zero binaries contain its name as a string outside
  librtk's own `.dynstr`, zero internal callers inside librtk.so
  itself (no jal/bal to its address, no relocation entries).
  The symbol is exported but unreferenced. Dead code from a
  partial feature port -- the 3.5 KB size growth is real but
  the new function is unreachable from anything that runs.
- **The kernel itself is NOT identical between firmwares.**
  Decompressed `uImage` payloads (LZMA, 2,979,900 bytes each)
  differ at 2.1 million byte positions. Most of that is
  address-shift cascade from inserting new code near the start
  of a built-in driver. The semantic changes, recovered via
  `strings` set-diff (comm -13 sorted), are tightly bounded:
  V1.1.8 adds an SFP application IPC channel
  (`send to user app (%d) fail (%d)`, `sfp_app init failed`,
  `Incorrect state %u`, `Unknown chip 0x%x`), an EEPROM mirror
  debug interface on `/proc/lan_sds/lan_sds_debug` with verbs
  `r <addr> <length>`, `w <addr> <byte_data>`, `dump_eeprom`,
  `dump_mirror` against `EEPROM Mirror(SRAM)` over `addr: 0~511`,
  and a kernel function `trtk_gponapp_omci_mirror_set` (no
  V1.0 equivalent). All new strings reference
  `drivers/net/rtl86900/sdk/src/module/lan_sds/lan_sds_main.c`.
  The built-in `re8686_rtl9602c` driver (in `/sys/module/` but
  not `/proc/modules` -- compiled into vmlinux, not a `.ko`) is
  the affected module. This **invalidates the earlier claim
  that "the bug is in userspace because kernel modules are
  byte-identical"**: the kernel-resident switch-fabric driver
  *is* different, the loadable `.ko` modules merely sit on top
  of an interface that may have changed underneath them.

**What this tells us:** the loadable kernel modules (`pf_rtk.ko`,
`omcidrv.ko`) and the OMCI MIB state post-hydration are
byte-identical, but the built-in `re8686_rtl9602c` / lan_sds
kernel driver got new code. The bug is therefore in **one of**:
the new kernel-side switch-fabric code path, the userspace
translation that feeds it, or an interaction between them.
sfpapp's packet-redirect callback was tested and refuted
(graveyard #7). Remaining possibilities, ordered by plausibility
once the kernel diff is taken into account:

1. **The ME-to-kernel translation in `omci_app` and the feature
   modules produces different kernel configuration for identical
   MEs.** `omci_app` hydrates the MIB and configures the kernel
   via ioctls into `pf_rtk`. If V1.1.8 interprets one of the
   identical MEs differently and writes a different forwarding /
   filter rule, you get identical OMCI state but different
   data-plane behaviour. **Status of the obvious test:** `/proc/omci/*`
   is not a usable comparison surface. Every node returns 0 bytes
   on plain `cat` on a healthy V1.0 in O5 with WAN up
   (`/tmp/sfp_v1.0_proc_omci_healthy.txt`), not just on a broken
   V1.1.8. And no userspace binary in either rootfs references
   the path -- grepped every file under `bin/`, `sbin/`, `lib/`,
   `usr/`, `etc/`; only `omcidrv.ko` mentions `/proc/omci`. The
   nodes are populated only on write-trigger, but no in-tree
   userspace component issues that trigger. **What an actual
   test of H1 needs:** either ftrace on the `omcidrv_wrapper_*`
   functions during OMCI hydration (compares kernel-side actions
   directly), or disassembly of the `_write_proc` / `_read_proc`
   handlers in `omcidrv.ko` to recover the syntax of the
   write-probe-read protocol for forwarding state.
2. **The new built-in `re8686_rtl9602c` / lan_sds kernel code
   programs the switch fabric differently.** V1.1.8's kernel
   adds a new SFP application IPC channel and a
   `trtk_gponapp_omci_mirror_set` function (see "Shown empirically"
   above). The data plane on this hardware runs in the **switch
   fabric**, not in the Linux netdev: on V1.0, `pon0` shows
   0 packets in `/proc/net/dev` while `br0` shows thousands of
   bridged frames. The bridge runs in hardware; the kernel only
   sees frames destined to itself. If the new kernel code
   adjusts how the switch's L2 forwarding table or broadcast/
   multicast forwarding is programmed, a wrong adjustment would
   drop unicast at the fabric level before any Linux netdev
   counter could see it. **Captured evidence supporting this**
   (back-to-back boots of both firmwares, 2026-05-12,
   `/tmp/sfp_v1.0_vs_v1.1.8_diff_summary.txt`):
   - On V1.1.8 the kernel runs a new thread `[sfp_main]` (PID 383)
     that does not exist on V1.0. This is the kernel worker
     introduced by lan_sds_main.c.
   - `/proc/lan_sds/` gains 5 new nodes on V1.1.8 (`dump_eeprom`,
     `dump_mirror`, `eeprom`, `mirror`, `sfp_app`), all -r--r--r--
     and all returning 0 bytes on cat. The shell cannot probe
     them; the kernel uses them as state surfaces internally.
   - `/proc/rtl8686gmac/dev_port_mapping` shows Port0's carrier
     reassigned: V1.0 maps it to `pon0.2`, V1.1.8 maps it to
     `eth0`. Port0 is neither the PON port (2) nor the CPU port
     (3); it is a switch port whose Linux-side carrier owner the
     new code reassigns at boot. Carrier mapping by itself
     doesn't drop packets, but it confirms the new code is
     reaching into netdev-to-switch-port relationships.
   - Everything else Linux-visible is identical: `brctl`,
     `ebtables`, `ifconfig`, `ip link`, `/proc/net/*`, lsmod,
     /sys/module/, all match between firmwares.
   The fact that the userspace bridge config is identical while
   the drop ratio is 30000:1 forces the bug to be somewhere the
   shell can't see. The new `[sfp_main]` thread is the leading
   suspect. **What would close it:** dump switch registers via
   `/proc/rtl8686gmac/hw_reg` on both firmwares (verb not
   recovered yet -- handler strings show `cmd %s` but no usage
   message), or get source / decompiled analysis of the new
   lan_sds_main.c functions in the built-in kernel.
3. **`/lib/features/internal/me_00001000.so` (new in V1.1.8) does
   something at runtime that affects the data plane.** Strings
   include `no_send_alarm` and `feature_api_register` hooks across
   most MIB tables. Plausible but unfalsifiable without
   instrumentation (no `kill -STOP` equivalent for a shared
   library that `omci_app` has already mmap'd at startup; would
   need an LD_PRELOAD stub that no-ops its registered handlers,
   or a rebuilt `omci_app` with the feature module unloaded).
   Tested only if (1) and (2) come back clean.

Note on busybox quirk: V1.1.8's busybox ships **without** `pidof`.
Any script that relies on `kill -STOP $(pidof X)` will silently
no-op (pidof returns "not found", `$(...)` becomes empty, `kill`
fails with "you need to specify whom to kill"). Use
`ps | awk '$NF=="X"{print $1}'` instead. This is how the first
attempt at the sfpapp test in row 7 of the graveyard table
appeared to "not change the ratio" -- the STOP never landed. The
second attempt with direct PID lookup is the one that actually
froze sfpapp and produced the refutation.

The TCONT / Scheduler / GalEthProf / TrafficDescriptor / Ont2g /
Anig / EthUni / Unig MEs were captured on V1.0 only
(`/tmp/sfp_v1.0_traffic_mes.txt`). V1.1.8 equivalents not
captured because the WAN-down cost of another V1.1.8 boot was
judged too high once rungs 1-3 had pinned the locus below OMCI.
A future investigator running rung 4 on V1.1.8 should add these
captures to the diff; if any of them deviates, the locus story
shifts back upward.

**What we have NOT shown:** which specific component is the bug.
The userspace differences (`omci_app` is 1.5 KB larger; every
`/lib/features/internal/*.so` grew by 99-259 bytes;
`me_00001000.so` added; `mib_ExtendedOnuGZTE.so` added) and the
kernel difference (new lan_sds_main.c functions described above)
are each consistent with a regression but also consistent with
a benign rebuild plus a couple of new features. **Consistent
with is not evidence of.** Without source, the locus is bounded
by exclusion: not in `pf_rtk.ko` (text-identical), not in
`omcidrv.ko` (1-byte `.rodata` shift), not in sfpapp's
packet-redirect callback (STOP test refutation), not in the
post-hydration OMCI MIB state (byte-identical). What's left is
the omci_app -> kernel translation, the new lan_sds kernel code,
and the new `me_00001000.so` feature module -- in roughly that
order of plausibility.

`dmesg | grep -iE "pf_rtk|gem|omci|drop"` is silent on V1.0:
baseline healthy behaviour produces no kernel error messages. If
anyone retries V1.1.8 in the future, the V1.1.8 dmesg should be
the next capture; the new kernel strings (`send to user app`,
`Incorrect state %u`, `Unknown chip 0x%x`, `sfp_app init failed`)
are also worth grepping for -- any of those firing is a direct
signal that the new lan_sds path is malfunctioning.

### Spoofable OMCI identity, and why it mattered here

The chain `OMCI_SW_VER1` NVRAM key (config store) →
`CircuitPack.Version` (both 0x101 and 0x106 bridge-port entities)
→ `SwImage.Active.Version` is **userspace-controllable end-to-end**
via the web UI Settings page, or `mib set OMCI_SW_VER1=...` +
reboot. On a fresh V1.0 boot with `OMCI_SW_VER1=V0.9-spooftest`,
the OMCI MIB advertised that fake string to the OLT through three
different MEs simultaneously, and WAN service continued normally.

This was the cleanest counter-evidence in the whole investigation.
"The BNG keys off the advertised firmware version" is a structurally
attractive theory for any service-profile-related break: it explains
why one firmware works and another doesn't, it matches the way some
operator BNGs do gate on vendor identity, and it's hard to falsify
without modifying the identity at the source. The NVRAM-controlled
spoof made it a 6-minute experiment instead of an unfalsifiable
worry, and ruled out the entire identity-keying class of theories
in one shot.

The auto-derived `SwImage.Version` one might expect (read from
actual flash partitions under `/proc/mtd`) does not exist on this
firmware. What's reported via OMCI for slot 0's running version
comes from NVRAM config, not from the slot's actual binary.
Capability worth preserving for any future "the BNG decides who
I am based on what I advertise" debugging on this hardware family.

### Operational guidance

**Empirical claim, true here**: V1.1.8-240408 on M110-SFP hardware
against an ALCL/Alcatel BNG (Airtel WAN, India) drops ~99.997% of
downstream unicast at the GEM-to-Ethernet path inside the ONU. WAN
does not work. V1.0-220923 on the same hardware against the same
BNG works without issue.

**Datapoints from elsewhere, not verified here**: a Discord user
reported V1.1.6-240202 working on a different OLT vendor entirely.
That's a useful bisection candidate (the regression may have
landed between V1.1.6 and V1.1.8), but it has not been tested on
our ALCL/Airtel BNG, and a different OLT vendor changes too many
variables to draw a clean conclusion about V1.1.6 here. If anyone
runs V1.1.6 on an ALCL OLT, the diagnostic ladder above will
classify it in the first 5 minutes.

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
