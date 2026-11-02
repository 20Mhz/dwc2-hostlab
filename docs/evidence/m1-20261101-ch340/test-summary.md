# Evidence bundle: Raspberry Pi Zero W Rev 1.1, kernel 6.12.47+rpt-rpi-v6

- hostlab revision: `99b377908453a7673a2908b0d7ba127a24b0cfdb`
- created 2026-11-01T23:59:16Z (preflight 2026-11-01T23:58:54Z)
- OS: Raspbian GNU/Linux 13 (trixie)
- primary session: `device`

**M1 complete: yes**

## M1 checks

| Check | Status | Detail |
|---|---|---|
| Physical Pi Zero W identified | PASS | Raspberry Pi Zero W Rev 1.1 (revision 9000c1) |
| dwc2 bound on the running kernel | PASS | 20980000.usb bound to dwc2 |
| A USB peripheral enumerates | PASS | 1:2 1a86:7523 USB Serial |
| usbmon and kprobe traces captured | PASS | 204 usbmon records, 1702 trace events, 1300 channel events |
| Analysis processes hardware traces | PASS | 100 URB instances, 100 with channel phases |
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
| H7 | PASS | 100/100 device URBs joined; channels seen [0, 1, 2, 3, 4, 5, 6, 7] (limit 8); 100/100 successful device control URBs with exactly one channel run per phase | analysis.json, dwc2-events.jsonl, usbmon.jsonl, sessions/device/session.json |
| H8 | FAIL | median usbmon submit->complete of device control URBs: 257000 ns with probes (n=100), 168500 ns without (n=100), ratio 1.525 | sessions/device/analysis.json, sessions/device-noprobe/analysis.json |
| H9 | PASS | 18 profile symbols traceable, 0 missing | probe-capabilities.json, preflight.json |
| H10 | PASS | hostlab_start: +241648 ns after the pre-write read, 63327 ns before the post-write read; hostlab_stop: +269462 ns after the pre-write read, 53511 ns before the post-write read | analysis.json, sessions/device/session.json |

## Sessions

- `device`: `bin/hostlab-ctl get-descriptor 1:2 --repeat 100` exit 0
- `device-noprobe`: `bin/hostlab-ctl get-descriptor 1:2 --repeat 100` exit 0
