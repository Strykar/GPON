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

## What to share back

For each command, paste either:

- the output (means the verb works on your firmware -- useful), or
- the literal text "no output" (means the verb is broken or missing on your
  firmware).

Plus the firmware version: `cat /etc/version`.

## The one-liners

### Sanity check (these work on V1.0-220923; if they print nothing, omcicli is unreachable on your firmware too)

```sh
omcicli get authuptime 2>&1 | grep -Ev '^(TableId \[|Usage:|$)'
omcicli get loidauth   2>&1 | grep -Ev '^(TableId \[|Usage:|$)'
```

### Verbs that should work but are broken on V1.0-220923

```sh
omcicli get sn     2>&1 | grep -Ev '^(TableId \[|Usage:|$)'
omcicli get loid   2>&1 | grep -Ev '^(TableId \[|Usage:|$)'
omcicli get cflag  2>&1 | grep -Ev '^(TableId \[|Usage:|$)'
```

### Verbs that don't exist on V1.0-220923

```sh
omcicli get onuid  2>&1 | grep -Ev '^(TableId \[|Usage:|$)'
omcicli get state  2>&1 | grep -Ev '^(TableId \[|Usage:|$)'
```

### OMCI ME reads (broken on V1.0-220923; firmware fix here would be huge)

ME numbers are from ITU-T G.988. If any of these print real data, that
firmware can expose proper OMCI metrics (distance to OLT, optical signal
level, FEC PMHD, GEM-port PMHD) that aren't accessible today.

```sh
omcicli mib getcurr  46 0 2>&1 | grep -Ev '^(TableId \[|Usage:|$)'  # OLT-G  -- OLT vendor/equipment ID
omcicli mib getcurr 256 0 2>&1 | grep -Ev '^(TableId \[|Usage:|$)'  # ONU-G  -- ONU vendor/serial/version
omcicli mib getcurr 257 0 2>&1 | grep -Ev '^(TableId \[|Usage:|$)'  # ONU2-G -- equipment ID, OMCC version
omcicli mib getcurr 263 0 2>&1 | grep -Ev '^(TableId \[|Usage:|$)'  # ANI-G  -- PON Tx/Rx, distance, signal level
omcicli mib getcurr 312 0 2>&1 | grep -Ev '^(TableId \[|Usage:|$)'  # FEC PMHD
omcicli mib getcurr 321 0 2>&1 | grep -Ev '^(TableId \[|Usage:|$)'  # GEM-port PMHD
omcicli mib getcurr 322 0 2>&1 | grep -Ev '^(TableId \[|Usage:|$)'  # MAC bridge port PMHD
omcicli mib getcurr  24 0 2>&1 | grep -Ev '^(TableId \[|Usage:|$)'  # Eth PMHD-3
```

### One-shot version (run all 15 in one paste, with section labels)

If you'd rather paste once and walk away:

```sh
for spec in \
  'auth|get authuptime' \
  'loid_auth|get loidauth' \
  'sn|get sn' \
  'loid|get loid' \
  'cflag|get cflag' \
  'onuid|get onuid' \
  'state|get state' \
  'olt-g|mib getcurr 46 0' \
  'onu-g|mib getcurr 256 0' \
  'onu2-g|mib getcurr 257 0' \
  'ani-g|mib getcurr 263 0' \
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
and report which command was running. (The script will abort; just paste
what was printed.)
