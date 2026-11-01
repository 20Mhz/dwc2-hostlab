# Evidence bundle: Raspberry Pi Zero W Rev 1.1, kernel 6.12.47+rpt-rpi-v6

- hostlab revision: `16744ac61b1669fa43d130bd2366b93e2d2e07f7`
- created 2026-11-01T23:44:13Z (preflight 2026-11-01T23:37:27Z)
- OS: Raspbian GNU/Linux 13 (trixie)
- primary session: `roothub`

**M1 complete: no**

## M1 checks

| Check | Status | Detail |
|---|---|---|
| Physical Pi Zero W identified | PASS | Raspberry Pi Zero W Rev 1.1 (revision 9000c1) |
| dwc2 bound on the running kernel | PASS | 20980000.usb bound to dwc2 |
| A USB peripheral enumerates | FAIL | only root hubs present; attach a test device |
| usbmon and kprobe traces captured | INCONCLUSIVE | 224 usbmon records, 318 trace events, 0 channel events |
| Analysis processes hardware traces | INCONCLUSIVE | 110 URB instances, 0 with channel phases |
| System remains stable | PASS | 0 problem lines in the filtered kernel log |
| Recovery procedure validated | PASS | after --revert + reboot: driver dwc_otg, hostlab block absent; after --apply + reboot: driver dwc2 |

## Hypotheses (docs/plan.md)

| ID | Status | Detail | Evidence |
|---|---|---|---|
| H1 | PASS | host_dma=1 dma_desc_enable=0 -> buffer_dma | probe-capabilities.json, preflight.json |
| H2 | PASS | params.host_channels=8 hw_params.host_channels=8 | probe-capabilities.json |
| H3 | BLOCKED | needs E3 cancel sweep against a device plus wire capture (E8, logic analyzer); not part of M1 | - |
| H4 | BLOCKED | needs the MMAP-buffer transfer variant with a canary (M4 API item); not implemented | - |
| H5 | BLOCKED | port reset (E6) is an M3 experiment; no reset was issued in M1 | - |
| H6 | BLOCKED | E6 variant; M3 | - |
| H7 | INCONCLUSIVE | no device URBs or no channel events recorded; 0/0 device URBs joined; channels seen [] (limit 8); 0/0 successful device control URBs with exactly one channel run per phase | analysis.json, dwc2-events.jsonl, usbmon.jsonl, sessions/roothub/session.json |
| H8 | INCONCLUSIVE | no session of the same command without kprobes (--no-ftrace) | - |
| H9 | PASS | 18 profile symbols traceable, 0 missing | probe-capabilities.json, preflight.json |
| H10 | PASS | hostlab_start: +432714 ns after the pre-write read, 72486 ns before the post-write read; hostlab_stop: +293699 ns after the pre-write read, 58441 ns before the post-write read | analysis.json, sessions/roothub/session.json |

## Sessions

- `roothub`: `bin/hostlab-ctl get-descriptor 1:1 --allow-hub --repeat 100` exit 0
