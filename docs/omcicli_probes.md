# omcicli probe one-liners

Goal: find out which `omcicli` verbs and OMCI ME reads work on **other**
HSGQ / ODI Realtek RTL960x firmware variants. On the firmware this
project is built against (V1.0-220923, M110 SFU, Sep 2022), `omcicli`
returns the MIB table-of-contents for almost every verb instead of the
requested data. If a newer firmware fixes that, several useful metrics
become reachable that we currently can't expose.

## Before you start

1. **SSH into your SFP in a fresh session** (e.g. `ssh admin@192.168.1.1`).
2. **Do not run `diag` in this session before any `omcicli` command below.**
   `diag`-then-`omcicli` wedges the OMCI daemon for several minutes;
   recovery may need a power-cycle. `omcicli`-only is safe.
3. The one-liners below all use a filter that prints output **only if the
   firmware returned real data**. If a verb is broken or doesn't exist,
   the one-liner prints nothing.
4. Each command is read-only. No `set`/`apply`/`add`/`del`/`reset`.

## What's safe to share, what isn't

The probes split into two groups:

- **PII-free probes** -- output is counter values, uptime, auth counts,
  status codes. Nothing identifying. Safe to paste back as-is.
- **PII-bearing probes** -- output may contain your SFP's GPON serial,
  your subscriber LOID, your ISP's OLT vendor / equipment ID, your
  fiber's optical power readings, distance to OLT, or in the worst case
  a PLOAM credential. The one-shot loop below **does not run these**.
  They live as separate one-liners in the
  [PII-bearing probes](#pii-bearing-probes-redact-before-sharing) section
  so you can run them deliberately and redact before sharing.

For both groups, please also share your firmware version: `cat /etc/version`.

## PII-free probes

Output here is safe to paste verbatim. Counter values, status codes,
uptime, auth counts -- nothing that identifies your line, your gear, or
your ISP.

```sh
omcicli get authuptime 2>&1 | grep -Ev '^(TableId \[|Usage:|$)'   # PON auth uptime in seconds
omcicli get loidauth   2>&1 | grep -Ev '^(TableId \[|Usage:|$)'   # auth status + counts
omcicli get cflag      2>&1 | grep -Ev '^(TableId \[|Usage:|$)'   # config flag value
omcicli get onuid      2>&1 | grep -Ev '^(TableId \[|Usage:|$)'   # OLT-assigned ONU ID
omcicli get state      2>&1 | grep -Ev '^(TableId \[|Usage:|$)'   # registration state code
omcicli mib getcurr 312 0 2>&1 | grep -Ev '^(TableId \[|Usage:|$)' # FEC PMHD: corrected/uncorrectable
omcicli mib getcurr 321 0 2>&1 | grep -Ev '^(TableId \[|Usage:|$)' # GEM-port PMHD
omcicli mib getcurr 322 0 2>&1 | grep -Ev '^(TableId \[|Usage:|$)' # MAC bridge port PMHD
omcicli mib getcurr  24 0 2>&1 | grep -Ev '^(TableId \[|Usage:|$)' # Eth PMHD-3
```

### One-shot version (PII-free, paste once and share output verbatim)

If you'd rather paste once and walk away:

```sh
for spec in \
  'auth|get authuptime' \
  'loid_auth|get loidauth' \
  'cflag|get cflag' \
  'onuid|get onuid' \
  'state|get state' \
  'fec-pmhd|mib getcurr 312 0' \
  'gem-pmhd|mib getcurr 321 0' \
  'macbr-pmhd|mib getcurr 322 0' \
  'eth-pmhd|mib getcurr 24 0'; do
    label=${spec%%|*}; cmd=${spec#*|}
    out=$(omcicli $cmd 2>&1 | grep -Ev '^(TableId \[|Usage:|$)')
    if [ -n "$out" ]; then
      echo "=== [$label] omcicli $cmd ==="
      echo "$out"
    fi
done
echo "--- end of report ---"
```

If a command appears stuck for more than ~15 seconds, press Ctrl-C once
and report which command was running.

## PII-bearing probes (redact before sharing)

Run these one at a time, look at the output, and decide whether to
share. Most testers won't need to run all of them; the broken/missing
**verb name** alone is the signal we need (we only need real data if a
fix is found).

If you do choose to share output:

- **GPON serial number** (e.g. `DSNW282D5510`) -- replace the trailing
  hex ID with `XXXXXXXX`, keep the vendor prefix.
- **LOID** (subscriber identifier) -- replace with `<redacted-loid>`.
- **OLT vendor / equipment ID** -- vendor name is usually fine; redact
  the equipment ID if you're not sure.
- **Distance to OLT** -- round to the nearest 100 m.
- **Optical Tx/Rx levels** -- these are unique-ish per fiber install;
  share the *units and field names* but redact the values.
- **Anything resembling a password, hex token, or base64 string** in
  ANI-G or ONU-G output -- redact entirely. ONU-G can carry the PLOAM
  challenge response on a working-firmware read; that's a credential.

```sh
omcicli get sn            2>&1 | grep -Ev '^(TableId \[|Usage:|$)'   # serial number
omcicli get loid          2>&1 | grep -Ev '^(TableId \[|Usage:|$)'   # subscriber LOID
omcicli mib getcurr  46 0 2>&1 | grep -Ev '^(TableId \[|Usage:|$)'   # OLT-G: OLT vendor/equipment ID
omcicli mib getcurr 256 0 2>&1 | grep -Ev '^(TableId \[|Usage:|$)'   # ONU-G: vendor/serial/version (POSSIBLE PLOAM cred)
omcicli mib getcurr 257 0 2>&1 | grep -Ev '^(TableId \[|Usage:|$)'   # ONU2-G: equipment ID, OMCC version
omcicli mib getcurr 263 0 2>&1 | grep -Ev '^(TableId \[|Usage:|$)'   # ANI-G: distance to OLT, Tx/Rx, signal level
```

The shape of the data (which fields exist, what types they are) is what
the project needs; the actual values aren't. A line like
`MeName: ANI-G\n  vendor_id: <redacted>\n  distance_m: <redacted>\n  ...`
is just as useful as the unredacted version.
