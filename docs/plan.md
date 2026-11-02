# Implementation plan

Status as of 2026-11-01 (evening). "Done" means implemented **and** its tests
run. The first hardware session ran on a Pi Zero W on 2026-11-01
([m1-bringup.md](m1-bringup.md)), ending with a CH340 USB-serial adapter
as the test device: M1 is complete.

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

### M1 — Reproducible environment and hardware bring-up — **done**

| Item | Status |
|---|---|
| ARMv6 build (static, `-march=armv6kz`) verified under `qemu-arm-static -cpu arm1176` with a negative control | done; the binaries also run on the Pi |
| Kernel build script (`bcmrpi_defconfig`, dwc2.ko, traceable-function extraction) | done, run (6.18.59); the running kernel (6.12.47) was checked directly instead |
| Deploy script (ssh + tar) | done, **used on hardware** |
| Overlay apply / revert (`enable-dwc2.sh`) | done, **validated on hardware**: apply → dwc2 bound; revert → byte-identical `config.txt`, dwc_otg bound; re-apply → dwc2 |
| Preflight (`hostlab-preflight`, read-only, text + JSON) | done, run on hardware under dwc_otg and dwc2 |
| Evidence bundle (`hostlab-evidence`) and one-command run (`scripts/pi/m1-run.sh`) | done; [evidence/m1-20261101-ch340](evidence/m1-20261101-ch340/test-summary.md) |
| Recovery instructions | [environment.md](environment.md#recovery), revert path validated |
| CI workflow | [.github/workflows/ci.yml](../.github/workflows/ci.yml); first GitHub run green on 2026-11-01 (Linux py3.9/3.12, macOS, ARMv6 under arm1176 emulation). The kernel-evidence job runs only on manual dispatch |
| Unit tests (C 127 checks + CLI 9, Python 42, script 11) | done, passing |
| A benign USB peripheral enumerating under dwc2, with usbmon + kprobe traces analysed | done: CH340 (`1a86:7523`, full speed), 100 control URBs, 1300 channel events |
| Software-only USB loopback (dummy_hcd + g_zero) for integration tests | **blocked**: Docker VM kernel has no dummy_hcd/usbmon modules; needs a QEMU VM with a custom kernel |

M1 definition of done ([m1-bringup.md](m1-bringup.md#definition-of-done)):
all criteria PASS.

### M2 — Instrumentation — **done, verified on hardware**

Done: `hostlab-mon`, `hostlab-ctl`, `hostlab-probe`, `hostlab-preflight`,
`hostlab-session`, `hostlab-analyze`, `hostlab-evidence`, trace format v1
([trace-format.md](trace-format.md)).

Fixed after the first hardware run: kprobe registration (EINVAL from
Python's append-mode seek on tracefs), URB address reuse in the analyser,
the overlay script's `[cm5]` false positive, usbmon events lost at stop.
Details in
[m1-bringup.md](m1-bringup.md#defects-found-on-hardware).

### M3 — Controller experiments — **E1, E2, E3, E6 done; results in [m3-results.md](m3-results.md)**

Phase-control proposal (C2): [phase-control-feasibility.md](phase-control-feasibility.md),
awaiting owner approval.

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

**M3 readiness (2026-11-01).** M1 is complete; M3 can start. Because H8
failed, E2 timing must use a reduced probe profile or be corrected against
`--no-ftrace` runs, and the C1 decision below is now due. With the tools as
they are:

| Experiment | Ready? | Needs |
|---|---|---|
| E1 baseline, E2 per-phase timing | yes; E2 needs a reduced profile option | the CH340 (attached); `m1-run.sh` already records E1 at n=100 with and without probes |
| E3 cancellation sweep | yes (`hostlab-ctl cancel-sweep`) | the CH340; read-only GET_DESCRIPTOR only |
| E6 port reset | tool ready (`hostlab-ctl reset --allow-reset`) | a disposable peripheral; reset re-enumerates the device |
| E7 unplug | manual | someone at the Pi |
| E4 timeout, E5 STALL | no | a gadget (second Pi Zero with `g_zero` or FunctionFS); not an M1 dependency |
| E8 wire timing | no | Saleae + FS device + GPIO marker |
| E9 dwc_otg comparison | yes | revert the overlay (validated) |

Order: E1 → E2 → E3 → E9 → E6, then E4/E5 if a gadget is available. All
use well-formed standard requests. Incomplete or deliberately malformed
transfers are out of scope.

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

Current status and evidence: [m3-results.md](m3-results.md#hypotheses).
H1, H2, H3 (kernel-level), H5, H7, H9, H10, H11, H12 PASS; H8 FAIL (full
kprobe profile; the default `fast` profile meets H11 instead); H6
INCONCLUSIVE; H4 BLOCKED.

| ID | Hypothesis | How |
|---|---|---|
| H1 | BCM2835 host runs in buffer-DMA mode | `probe.json` |
| H2 | 8 host channels | `probe.json` |
| H3 | A cancelled control URB reports `actual_length` 0 even if DATA completed | E3: cancels after the DATA transfer-complete |
| H4 | An IN channel can DMA into the URB buffer after giveback on dequeue | MMAP buffers + canary (not designed) |
| H5 | Enumeration sequence after reset depends on usbcore parameters | E6 with `old_scheme_first` |
| H6 | Behaviour of URBs pending across `USBDEVFS_RESET` | needs a long-pending URB |
| H7 | kprobe profile valid; URB join exact | analyser `consistency` |
| H8 | full kprobe profile ≤ 1.20 × median latency | `hostlab-bench` |
| H9 | Shipped traceable functions match | `probe.json` |
| H10 | `trace_clock=mono` equals CLOCK_MONOTONIC | marker brackets |
| H11 | A profile reconstructs every phase within +20 % median latency | `hostlab-bench` |
| H12 | Control transfers follow dwc2's programmed sequence (driver state) | `hostlab-report model` |

## Issue drafts

1. **M1: QEMU VM with dummy_hcd + g_zero for CI integration tests.** Build an
   arm64 or x86 kernel with `USB_DUMMY_HCD`, `USB_ZERO`, `USB_MON`,
   `KPROBE_EVENTS`; run hostlab-ctl/mon/session against the dummy device.
   (Not dwc2, but exercises usbfs/usbmon/tracefs end to end.)
2. **M4: bulk/interrupt transfers in the transport API**, so a URB can stay pending across a reset (H6) and for a memory-safe H4 canary design.
3. **M3: kprobe offsets for struct fields.** Generate `offsetof` values for
   `dwc2_qtd.control_phase`, `dwc2_host_chan.{xfer_len,data_pid_start,hcint}`
   by compiling against the exact RPi tree and config (asm-offsets style), and
   validate them on hardware. Only needed if C1 is not pursued.
4. **M3: FunctionFS delay gadget** for E4.
5. **M3: Saleae alignment** with a GPIO marker for E8.
