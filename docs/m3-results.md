# M3 results: Pi Zero W, dwc2, CH340 test device

Measured on 2026-11-01/02 on the hardware of [m1-bringup.md](m1-bringup.md):
Raspberry Pi Zero W Rev 1.1, kernel `6.12.47+rpt-rpi-v6`, dwc2 in buffer-DMA
host mode, test device CH340 USB-serial adapter `1a86:7523` (full speed, ep0
max packet 8). Only standard GET_DESCRIPTOR(DEVICE) requests and bounded
USBDEVFS_RESETs were sent; the adapter's serial port was never opened.
Evidence: [evidence/m3-20261101/](evidence/m3-20261101/) (`summary.json`
condenses every number below). Revisions per data set are listed in
[evidence/m3-20261101/README.md](evidence/m3-20261101/README.md).

## Hypotheses

| ID | Status | Evidence |
|---|---|---|
| H1 buffer DMA | PASS (M1) | `m1-20261101-ch340/probe-capabilities.json` |
| H2 8 host channels | PASS (M1) | same |
| H3 cancelled control URB reports `actual_length` 0 although DATA completed | **PASS (kernel-level, small n)**: 5 URBs cancelled after the controller reported the DATA stage complete (2 with the DATA transfer-complete recorded before the dequeue, 3 with the STATUS run already started) all returned 0 bytes. Wire-level data movement is not observed; the controller's own completion report is the evidence | `e3/e3-state/cancel.json`, `h3/h3-state/cancel.json`, `e3/e3-fast/cancel.json` |
| H4 DMA into the buffer after giveback | BLOCKED: needs a memory-safe canary design (MMAP buffers); not attempted | — |
| H5 re-enumeration after reset depends on usbcore parameters | **PASS**: default (`old_scheme_first=N`) does two port resets with GET_DESCRIPTOR(DEVICE,64) at address 0 between them, ~410 ms per reset; `old_scheme_first=Y` does one port reset, SET_ADDRESS first, then GET_DESCRIPTOR(DEVICE,8), ~222 ms | `e6/e6-reset/reset.json`, `h5/h5-old-scheme/reset.json` |
| H6 URBs pending across USBDEVFS_RESET | **INCONCLUSIVE**: 20/20 URBs queued just before the ioctl completed normally, but 0.1–0.3 ms after it started, while the first port reset came 1.5–12.5 ms later; none was in flight at the reset. Needs a URB that stays pending (bulk/interrupt IN), i.e. API work, not new hardware | `e6/e6-pending/` |
| H7 URB / channel / phase correlation | PASS (M1); 1000/1000 joins in every profile with an enqueue probe after the join fix | `instrumentation/bench1.json` |
| H8 kprobe overhead small (full profile ≤ 1.20× median) | FAIL (unchanged): full profile +52.3 % | `instrumentation/bench1.json` |
| H9 traceable functions | PASS (M1) | — |
| H10 trace_clock=mono | PASS (M1, and every M3 session) | session `markers` |
| **H11** (new) an instrumentation profile reconstructs every control phase within +20 % median latency | **PASS**: profile `fast` +12.8 % median (+3.6 % P95), 600/600 and 800/800 phases | `instrumentation/bench2.json`, `bench3.json` |
| **H12** (new) every control transfer follows dwc2's programmed sequence (verified from driver state) | **PASS**: 600/600 URBs conform | `instrumentation/bench3-runs.tar.xz` (`*-state/model.json`) |

## 1. Instrumentation overhead (Priority 1)

usbmon submit → complete of successful GET_DESCRIPTOR(DEVICE), 200 transfers
per run, profiles in seeded shuffled order per round.

| Profile | Probe hits / transfer | Median | Overhead median / P95 | Phases | URB join | Data set |
|---|---|---|---|---|---|---|
| none (usbmon only) | 0 | 172 µs | — | — | — | bench1 (5×200) |
| idle (tracing on, no hits) | 0 | 172 µs | +0.0 % / −3.7 % | — | — | bench1 |
| **fast** (function tracer: channel start + release) | 6 | 194 µs | **+12.8 % / +3.6 %** | 600/600 | — | bench3 (3×200) |
| fphase (function tracer: start) | 3 | 195 µs | +13.4 % / +7.8 % | 1000/1000 | — | bench1 |
| minimal (enqueue + giveback kprobes) | 2 | 203 µs | +18.0 % / +12.4 % | — | 1000/1000 | bench1 |
| phase | 5 | 224 µs | +30.2 % / +24.9 % | 1000/1000 | 1000/1000 | bench1 |
| channel | 8 | 236 µs | +37.2 % / +28.1 % | 1000/1000 | 1000/1000 | bench1 |
| state (driver state, H12) | 11 | 255 µs | +48.3 % / +37.0 % | 600/600 | 600/600 | bench3 |
| full (M1 profile) | 17 | 262 µs | +52.3 % / +41.5 % | 1000/1000 | 1000/1000 | bench1 |

Run-to-run spread of the per-run medians was 1–5 µs for the baseline and
2–11 µs for the profiles; every profile with a hit is slower than every
baseline run ("above noise" in the bench tables).

What the cost is made of (bench2, single probes, 4×200):
- All 18 kprobes are `[OPTIMIZED]` (branch, not breakpoint); there is no
  trap cost to remove.
- The cost is mostly per transfer, not per hit: one `urb_enqueue` kprobe
  +12.8 %, one `giveback` kprobe +9.9 %, function tracer on channel start
  (3 hits) +13.4 % and on start + release (6 hits) +12.8 %. Consistent with
  cache pollution by the tracing path on the ARM1176's 16 KB caches; this is
  an interpretation, not measured directly.
- A kprobe that dereferences `chan` (`hc_start`) costs +23.8 %.

Default profile is now `fast`. `m1-run.sh` pins `full` because H7 needs the
URB address join; `state` is for correctness work, not timing.

Correctness fixes found by the benchmark: the URB join used a fixed 1 ms
tolerance between usbmon 'S' and the dwc2 enqueue and missed 2/1000 when
the submitting task was delayed by 1.01 ms; pairing now requires the kernel
instance to start inside the usbmon S..C window. `kprobe_profile` is recorded
per session: 0 missed hits in the checked E3 capture.

## 2. Cancellation (E3, Priority 2)

`hostlab-ctl cancel-sweep`, delays measured from SUBMITURB return to
DISCARDURB, 5–10 attempts per delay; `hostlab-report cancel`.

| Capture | Attempts | Cancelled (phase at dequeue) | Accepted but completed | Too late |
|---|---|---|---|---|
| no probes, 0–300 µs | 305 | 80 (phase not observed) | 16 | 209 |
| fast, 0–300 µs | 305 | 48 DATA, 3 STATUS, 3 not observed | 26 | 225 |
| state, 0–300 µs | 305 | 42 DATA, 1 STATUS | 17 | 245 |
| state, 20–50 µs step 1 | 310 | 190 DATA, 1 STATUS | 106 | 13 |

- No URB was cancelled before its SETUP run or during SETUP: by the time
  the dequeue runs, SETUP has always finished.
- **Accepted ≠ cancelled.** An accepted DISCARDURB still completes normally
  when the completion interrupt lands between `usb_kill_urb` and dwc2 taking
  its lock in the dequeue. Kernel events locate the loss: before
  `usb_hcd_unlink_urb` (33), inside it with `use_count` already 0 so the HCD
  dequeue is never called (47), or inside dwc2's dequeue (26) (targeted
  sweep). That window is the cancel path itself: kill → dequeue median 12 µs
  (n=191).
- After the dequeue: channel halt request +5 µs (median), channel release
  +46 µs; in 234/234 cancellations the URB was given back **before** its
  channel was released (the M0 source reading, now measured).
- Cancelling *after* DATA is nearly unreachable from userspace: 2 of 615
  state-profile attempts; a late cancel usually loses the race instead.

Limitation: what reached the wire in a cancelled DATA run is not observable
here (no bus analyser); H3 rests on the controller's completion report.

## 3. Reset (E6, Priority 3)

15 resets in total, 5 per capture, profile `channel`; the device answered
GET_DESCRIPTOR after every stage (`*-device-check.jsonl`).

- USBDEVFS_RESET takes 402–463 ms with default usbcore parameters, 213–256 ms
  with `old_scheme_first=Y` (runtime parameter, restored to N).
- Sequence (default): port reset → GET_DESCRIPTOR(DEVICE,64) at address 0 →
  port reset → SET_ADDRESS(2) → GET_DESCRIPTOR(DEVICE,18) →
  GET_DESCRIPTOR(CONFIG,39) → SET_CONFIGURATION(1) → the `ch341` driver's
  vendor requests while it rebinds.
- Channel cleanup: every channel start in the reset captures was released,
  in order, none left open (135 + 195 + 135 starts).
- H6 not exercised (see table).

## 4. Control-transfer state model (Priority 4)

From the `state` profile, which reads dwc2's own fields with offsets taken
from DWARF of the running kernel's headers
([dwc2-offsets-6.12.47.json](evidence/m3-20261101/dwc2-offsets-6.12.47.json)),
600 of 600 GET_DESCRIPTOR(DEVICE) URBs (3 runs × 200) followed exactly, with
no unattributed or ambiguous events:

```
urb_enqueue (process context, usbfs)
  channel A: start  PID=SETUP  OUT  len=8      (dwc2_hc_start_transfer)
             IRQ    HCINT=XFERCOMPL|CHHLTD|ACK, qtd->control_phase=SETUP
             release reason=COMPLETE           -> channel A back to the free list
  channel B: start  PID=DATA1  IN   len=18     (next free channel, never A)
             IRQ    XFERCOMPL|CHHLTD|ACK, control_phase=DATA
             release reason=COMPLETE
  channel C: start  PID=DATA1  OUT  len=0      (STATUS)
             IRQ    XFERCOMPL|CHHLTD|ACK, control_phase=STATUS
             dwc2_host_complete -> usb_hcd_giveback_urb (IRQ side)
             release reason=URB_COMPLETE
  BH giveback, usbmon 'C', usbfs completion
```

Software-owned (dwc2, in the channel IRQ under `hsotg->lock`): the phase
sequence and `control_phase`, choice of channel per phase (free list
order: 1200/1200 phase changes moved to a different channel), starting PID per
phase, transfer length, release and re-queue, giveback, cancellation (halt
request + immediate giveback). Hardware-owned: token generation, packets
within a run (the 18-byte DATA run is 3 packets of 8 bytes), toggles within
a run, CRC, NAK retry in buffer-DMA mode, the transfer-complete/halt
interrupt. With cancellation the extra path is: dequeue → halt request
(reason URB_DEQUEUE) → giveback → CHHLTD IRQ → release (reason URB_DEQUEUE).

## Remaining limitations

- One device, one request type (GET_DESCRIPTOR(DEVICE), 3 phases, full
  speed, no hub, no split transactions). No-data-stage and OUT-data requests
  are modelled in the checker but were not exercised (only standard read
  requests are allowed by default).
- H3's n is 5; reaching the post-DATA window from userspace is rare.
- H6 needs a long-pending URB; H4 needs a canary design. Neither needs new
  hardware.
- Cache-pollution explanation of the per-transfer probe cost is not measured.
- Wi-Fi power saving dropped the Pi off the network twice; it was switched
  off at runtime for the session (`iw dev wlan0 set power_save off`, not
  persistent).
