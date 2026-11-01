# M1 hardware bring-up log

First session on physical hardware, 2026-11-01 (times are local, UTC−3).
Everything here was observed on the Pi unless marked "source" (read from
the kernel tree in [references.md](references.md)).

## Setup

- Board: Raspberry Pi Zero W Rev 1.1 (`9000c1`), on Wi-Fi, reached by SSH
  from the Mac mini M4 (dedicated key, alias `pizero`).
- OS: Raspberry Pi OS Lite 32-bit, Raspbian 13 (trixie), kernel
  `6.12.47+rpt-rpi-v6` (raspberrypi/linux `359f37f`), not the 6.18 tree M0
  analysed. Differences: [references.md](references.md#the-kernel-actually-running-on-the-target).
- Instruments: none attached. No USB peripheral attached during the session.
- Tools deployed with `scripts/deploy.sh`; the bundle records the exact commit.

## What was run

| # | Step | Result | Artifact |
|---|---|---|---|
| 1 | Read-only inventory: `config.txt`, bound driver, modules, kernel config, tracefs | dwc_otg bound to `20980000.usb`; `dwc2.ko`, `usbmon.ko` present; KPROBES/KPROBE_EVENTS/FUNCTION_TRACER on, DYNAMIC_DEBUG off, HIST_TRIGGERS off; no host-side USB tracepoints (only `gadget`) | `states/dwc_otg.json` |
| 2 | Source and binary check of the kprobe profile against 6.12.47 | prototypes, `hc_num` offset 0, halt enum unchanged; 18/18 symbols in shipped `dwc2.ko` | [references.md](references.md#the-kernel-actually-running-on-the-target) |
| 3 | `hostlab-preflight` under dwc_otg (baseline) | ready: no (overlay, binding, device) | `states/dwc_otg.json` |
| 4 | `enable-dwc2.sh --apply`, reboot (20:17) | back in ~2 min; dwc2 bound; `dr_mode=host` | — |
| 5 | Preflight under dwc2 | buffer DMA, 8 channels, core 2.80a, 18/18 kprobe targets, usbmon present | `preflight.json`, `probe-capabilities.json` |
| 6 | Root-hub sessions (GET_DESCRIPTOR(DEVICE) to the root hub ×20, later ×100 for the bundle, `--allow-hub`) | first attempt: all 18 kprobes failed with EINVAL (defect 1); after the fix: 18 registered, events recorded, all removed at teardown, `trace_clock` restored | `sessions/roothub/` |
| 7 | `enable-dwc2.sh --revert`, reboot (20:28) | `config.txt` MD5 identical to the pre-apply backup; dwc_otg bound | `states/reverted.json` |
| 8 | `enable-dwc2.sh --apply`, reboot (20:31) | dwc2 bound again | `states/reapplied.json` |
| 9 | `m1-run.sh` smoke test against the root hub (refused by policy, as intended) | script ran end to end; exposed defects 4 and 5 | — (deleted) |
| 10 | Partial evidence bundle | [evidence/m1-20261101-partial](evidence/m1-20261101-partial/test-summary.md) | — |

The Pi was left with the dwc2 overlay applied (hostlab block in
`config.txt`, backups `config.txt.hostlab-*` next to it).

## Defects found on hardware

None of these showed up in unit tests, emulation or the Docker smoke test.
Each now has a regression test, except that for defect 1 only the write
path and the warning are tested: tracefs's seek behaviour cannot be
reproduced on a regular file.

1. **Every kprobe registration failed with EINVAL.** Python's `open(path,
   "a")` seeks to the end of the file; tracefs's `kprobe_events` rejects
   `lseek(SEEK_END)`. Writes now use `os.open(O_WRONLY|O_APPEND)`. Plain
   `"w"` would have been worse: `O_TRUNC` on `kprobe_events` deletes every
   kprobe on the system. The session also exited 0 with no warning; it now
   warns when any probe fails to register.
2. **URB addresses are recycled.** usbfs allocates a URB per request and
   the slab returns the same address: 20 consecutive transfers shared one
   address (100 of 100 in the bundle's session), and the analyser merged
   them into one URB. Timelines are now
   per instance (submit→giveback), paired between usbmon and kprobes by
   address and submit time. The M0 statement "usbmon id = URB pointer gives
   exact joins" holds only within one URB lifetime.
3. **`enable-dwc2.sh --apply` would have done nothing.** Trixie's stock
   `config.txt` has `dtoverlay=dwc2,dr_mode=host` under `[cm5]`; the old
   `grep '^dtoverlay=dwc2'` took it as enabled. The script now evaluates
   `config.txt` sections, and `--revert` removes only its own block instead
   of restoring an older backup over later edits.
4. **False "unstable" verdict.** The case-insensitive pattern `BUG:`
   matched `mmc_debug:0` in the boot log. Oops markers are now
   case-sensitive word matches.
5. **H8 judged on the wrong data.** The smoke run compared root-hub URBs of
   a refused command (n=8) and reported FAIL. H8 now needs successful device
   control URBs, exit 0 on both runs and at least 30 samples each, otherwise
   INCONCLUSIVE.

6. **Pre-trace URBs counted as join failures.** usbmon starts before
   ftrace; six root-hub URBs from that gap were reported as unpaired, and
   device URBs there would have failed H7. Join checks now cover only the
   window between the start and stop markers.

Also fixed: `hostlab-mon` takes `-h`, not `--help` (preflight reported it
as not runnable); macOS tar extended attributes in the deploy archive;
preflight treated unreadable root-only tracefs files as failures for
non-root users; `/proc/config.gz` is absent until `modprobe configs`, so
the probe falls back to `/boot/config-RELEASE`.

## Observations

- Root-hub control URBs never reach `_dwc2_hcd_urb_enqueue` (usbcore handles
  them), so a root-hub-only session exercises usbcore probes, usbmon and the
  clock checks but no channel events.
- IRQ-side giveback to BH giveback for root-hub URBs: median 42 µs, range
  38–58 µs (n=104, `analysis.json`).
- One usbfs URB address carried all 100 GET_DESCRIPTOR requests of the
  root-hub session (`consistency.address_reuse_max` = 100).
- usbmon starts about 0.3 s before ftrace; URBs submitted in that gap have
  no kprobe events and are excluded from the join checks.
- ftrace marker timestamps fell 240–270 µs after the CLOCK_MONOTONIC read
  before the write and 54–62 µs before the read after it. The bracket
  (~0.3 ms) is wide because Python on the ARM1176 is slow; it bounds, rather
  than measures, the clock difference.
- Boot with the dwc2 overlay took about 2 minutes to SSH availability
  (load average ~5 at login), the same as with dwc_otg.

## Definition of done

| Criterion | Status | Evidence |
|---|---|---|
| Physical Pi Zero W identified | PASS | `preflight.json` board, revision `9000c1` |
| DWC2 binding verified on the running kernel | PASS | `preflight.json` `driver_binding` |
| A benign USB peripheral enumerates | **open** (none attached) | — |
| usbmon and kprobe traces captured | partial: captured for root-hub traffic; no dwc2 channel events without a device | `sessions/roothub/` |
| Analysis pipeline processes actual hardware traces | partial: 110 root-hub URB instances; no channel phases | `analysis.json` |
| Evidence supports the relevant hypotheses | H1, H2, H9, H10 PASS; H7, H8 need the device run | `hypothesis-results.json` |
| Recovery procedures validated | PASS | `states/reverted.json`, `states/reapplied.json` |

**M1 is partially complete.**

## Next action

Attach an ordinary USB peripheral (keyboard, mouse or flash drive) to the
Zero W's **USB** port through an OTG adapter, then:

```
ssh pizero
cd ~/dwc2-hostlab
bin/hostlab-ctl list                       # note BUS:DEV of the device, e.g. 1:2
sudo scripts/pi/m1-run.sh 1:2
cat evidence/m1-*/test-summary.md
```

Copy the bundle back with `scp -r pizero:dwc2-hostlab/evidence/m1-DATE
docs/evidence/` and commit it. M1 is complete when every row of its
`test-summary.md` M1 table is PASS.
