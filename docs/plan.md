# Implementation plan

Status as of 2026-11-01. "Done" means implemented **and** its tests run; nothing
below has run on a Pi Zero W yet. Hardware was not reachable from the
development environment.

## Smallest useful prototype (built)

A non-invasive, userspace-only recorder for one experiment, on the stock
Raspberry Pi OS kernel with `dtoverlay=dwc2,dr_mode=host`:

```
hostlab-session run --out run1 -- hostlab-ctl get-descriptor 1:4 --repeat 100
hostlab-analyze run1
```

It records three views of the same transfers and joins them:

| Recorder | Source | Correlation key |
|---|---|---|
| `hostlab-ctl` | usbfs ioctls, CLOCK_MONOTONIC | setup bytes + time |
| `hostlab-mon` | usbmon binary API, CLOCK_REALTIME | `urb` = kernel URB address |
| kprobe events (`hostlab-session`) | tracefs, `trace_clock=mono` | `urb=` argument; channel events by time window |

For each control URB, the analyser reports channel runs, inferred phases
(SETUP / DATA / STATUS) with the channel number used for each, scheduling
latency (enqueue → first channel start), IRQ → BH giveback delay and usbmon
latency. For cancelled URBs it also reports the phase reached at dequeue and
the dequeue → channel-release latency.

No kernel code, no register access, no persistent system change. Teardown
removes only hostlab's kprobes and restores the tracefs settings it changed.

## Milestones

### M0 — Technical reconnaissance — **done**

[dwc2-architecture.md](dwc2-architecture.md),
[usb-control-transfers.md](usb-control-transfers.md),
[capability-matrix.md](capability-matrix.md),
[kernel-integration-options.md](kernel-integration-options.md),
[references.md](references.md), [evidence/](evidence/).

### M1 — Reproducible environment — **done except items needing hardware**

| Item | Status |
|---|---|
| ARMv6 build (static, `-march=armv6kz`) verified under `qemu-arm-static -cpu arm1176` with a negative control | done |
| Kernel build script (`bcmrpi_defconfig`, dwc2.ko, traceable-function extraction) | done, run |
| Deploy script (ssh + tar) | done, dry-run tested only |
| Target setup / revert script (`dtoverlay=dwc2`) | done, tested on a fake `/boot/firmware` in a container |
| Recovery instructions | [environment.md](environment.md#recovery) |
| CI workflow | written ([.github/workflows/ci.yml](../.github/workflows/ci.yml)); **not executed** (no remote). Its Linux, macOS and ARMv6 steps were run locally with the same commands |
| Unit tests (C 127 checks + CLI 9, Python 27) | done, passing |
| Software-only USB loopback (dummy_hcd + g_zero) for integration tests | **blocked**: Docker VM kernel has no dummy_hcd/usbmon modules; needs a QEMU VM with a custom kernel |

### M2 — Instrumentation — **implemented, awaiting first hardware run**

Done: `hostlab-mon`, `hostlab-ctl`, `hostlab-probe`, `hostlab-session`,
`hostlab-analyze`, trace format v1 ([trace-format.md](trace-format.md)).

First hardware run (runbook in [environment.md](environment.md#first-run-on-hardware)):
1. Capture `probe.json` on stock dwc_otg, then with the dwc2 overlay.
2. Verify H1, H2, H7, H9, H10 (below) from that run.
3. Commit the captures to `docs/evidence/` as the first real data.

### M3 — Controller experiments

Benign test devices, in order of preference:
1. **Second Pi Zero (W) in gadget mode** running `g_zero` (`CONFIG_USB_ZERO=m`
   in the stock kernel). Deterministic, scriptable, and it pairs with the
   stock `usbtest` host module for control/unlink stress.
2. A FunctionFS gadget on the same device, to answer control requests from
   userspace with a programmable delay. This gives controlled NAK / timeout
   behaviour without a special-purpose device.
3. Commodity devices: a FS keyboard (FS path, logic-analyzer friendly) and
   a HS flash drive.

| ID | Experiment | Measures | Answers |
|---|---|---|---|
| E1 | 1000× GET_DESCRIPTOR, with and without kprobes | latency distributions, runs per URB, channels used | baseline; H8 probe overhead |
| E2 | per-phase timing | `hc_start → xfercomp` per phase | phase cost on BCM2835 |
| E3 | `cancel-sweep` 0–2000 µs, step 10, 50 reps | outcome classes, phase at dequeue, dequeue→release, giveback-before-release, `actual` | H3, H4; DISCARDURB race rate on dwc2 |
| E4 | FunctionFS gadget delays its ep0 reply past the timeout | timeout path, NAK handling, kill latency | dwc2 has no own timeout ([§3](usb-control-transfers.md#3-completion-statuses-produced-by-dwc2-for-control-urbs)) |
| E5 | unsupported request → STALL | stall handler, `-EPIPE` | |
| E6 | `hostlab-ctl reset` | port-reset hold time, re-enumeration sequence | H5, H6 |
| E7 | unplug during a transfer (manual) | error statuses, channel cleanup | recovery behaviour |
| E8 | FS device on a Saleae Logic Pro 8, GPIO marker for clock alignment | wire tokens vs `hc_start` | which phase reached the bus at cancel |
| E9 | E1 + E3 on dwc_otg (usbmon only) | comparison | driver differences visible above the HCD |

**C1 decision (M3):** propose dwc2 tracepoints (observation only) if E1–E3
show that the kprobe profile is fragile or too costly, or that struct fields
(`control_phase`, `hcint`, `xfer_len`) are needed.

**C2 gate (M3, owner approval required):** experimental phase control. See
[kernel-integration-options.md](kernel-integration-options.md#c2-experimental-phase-control-control-path).
It is not started without a written justification of its independent value.

### M4 — Reusable framework

- Freeze backend API v1 after the M3 data (v0.1 is provisional). Candidates:
  an MMAP-buffer transfer variant (`USBDEVFS_CAP_MMAP`, needed for H4); bulk
  and interrupt transfers; per-transfer kernel URB id via a kprobe join.
- Scenario files (declarative experiments) and a regression suite that
  replays recorded sessions through the analyser.
- libusb backend if a consumer needs portability (not needed for usbfs
  semantics).

### M5 — Public release

README, diagrams, supported hardware, results from M3, known limitations,
licensing review, contribution guide. Upstream candidates: dwc2 tracepoints
(C1); the `hcd.c:3702` 50 ms reset hold regardless of speed (comment
disagrees with code; verify intent first); possibly the RPi DMA-alignment
patch if it is still needed and not yet upstream.

## Hypotheses to verify on hardware

| ID | Hypothesis | How |
|---|---|---|
| H1 | BCM2835 host runs in buffer-DMA mode (`host_dma=1`, `dma_desc_enable=0`) | `probe.json` |
| H2 | 8 host channels | `probe.json` (`hw_params.host_channels`) |
| H3 | A cancelled control URB reports `actual_length` 0 even if DATA moved | E3, `actual` vs E8 wire data |
| H4 | An IN channel can DMA into the URB buffer after giveback on dequeue | E3 with MMAP buffers + canary |
| H5 | Enumeration sequence after reset depends on usbcore parameters | E6, usbmon |
| H6 | Behaviour of URBs pending across `USBDEVFS_RESET` | E6 variant |
| H7 | The register-fetch kprobe profile is valid on the shipped kernel (AAPCS args preserved for static functions; `hc` < host_channels; enqueue `urb` = usbmon id) | E1 consistency checks in the analyser |
| H8 | kprobe overhead is small relative to transfer latency | E1 with / without probes |
| H9 | The shipped kernel's traceable dwc2 functions match our build | `probe.json` `hostlab_probes_missing` |
| H10 | `trace_clock=mono` equals CLOCK_MONOTONIC | marker offsets in `analysis.json` |

## Issue drafts

1. **M1: QEMU VM with dummy_hcd + g_zero for CI integration tests.** Build an
   arm64 or x86 kernel with `USB_DUMMY_HCD`, `USB_ZERO`, `USB_MON`,
   `KPROBE_EVENTS`; run hostlab-ctl/mon/session against the dummy device.
   (Not dwc2, but exercises usbfs/usbmon/tracefs end to end.)
2. **M2: first hardware run** per the runbook; commit captures.
3. **M3: kprobe offsets for struct fields.** Generate `offsetof` values for
   `dwc2_qtd.control_phase`, `dwc2_host_chan.{xfer_len,data_pid_start,hcint}`
   by compiling against the exact RPi tree and config (asm-offsets style), and
   validate them on hardware. Only needed if C1 is not pursued.
4. **M3: FunctionFS delay gadget** for E4.
5. **M3: Saleae alignment** with a GPIO marker for E8.
