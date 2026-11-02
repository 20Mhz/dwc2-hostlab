# Phase-control interface: feasibility and proposal

Status: **proposal, not implemented.** Implementing it changes a kernel
driver and needs the owner's explicit approval (C2 gate,
[kernel-integration-options.md](kernel-integration-options.md)). Everything
cited is from the 6.12.47 source at raspberrypi/linux `359f37f` and the M3
measurements in [m3-results.md](m3-results.md).

## Question

Can a minimal dwc2 extension let an experimenter stop an ordinary control
transfer at a phase boundary (after SETUP, after DATA) and continue or
cancel it, while dwc2 keeps sole ownership of the controller, kernel
stability is preserved and the USB protocol stays correct?

## What the measurements establish

1. **Phases are software-sequenced and leave no hardware state between
   them.** In 1200/1200 phase changes the channel was released (reason
   COMPLETE) before the next phase started on a different channel
   (H12). Between phases the transfer exists only as a QTD on the QH's list
   with `control_phase` advanced; no channel, no DMA in flight.
2. **Userspace cannot select a phase boundary.** Cancels issued from
   userspace reached the post-DATA window in 2 of 615 attempts (E3); late
   cancels mostly lose the race to the completion (kill → dequeue takes
   ~12 µs, the STATUS run ~23 µs). Timing tricks without a driver change do
   not give control.
3. **The cancel path of an idle QTD already exists and is safe.**
   `dwc2_hcd_urb_dequeue` (hcd.c ~1936-1965) halts a channel only when the
   QTD is in process; an idle QTD is unlinked and freed. usbcore's timeout
   (`usb_kill_urb`) uses this path, so a held transfer can always be
   cancelled by existing code.

## Driver structure relevant to a hold

- Phase advance: `dwc2_hc_xfercomp_intr` (hcd_intr.c 958-1034) sets
  `qtd->control_phase`, then `dwc2_complete_non_periodic_xfer` →
  `dwc2_release_channel` (674-760): `dwc2_deactivate_qh`, channel to
  `free_hc_list`, then `dwc2_hcd_select_transactions` +
  `dwc2_hcd_queue_transactions`, which put the next phase on a channel in
  the same hard IRQ.
- Locking: the whole path runs under `hsotg->lock` (hcd_intr.c 2217-2266);
  enqueue and dequeue take the same lock.
- Ownership: URB (usbcore) → `dwc2_hcd_urb` → QTD (per URB) on QH (per
  endpoint) on the non-periodic schedule; the channel is borrowed per run.

## Alternatives without a driver change

| Approach | Result |
|---|---|
| Userspace timing (usbfs) | measured: cannot hit phase boundaries (2/615) |
| kprobe handlers that delay or alter behaviour | kprobes stay observers in this project; function error injection is off in the running kernel (`# CONFIG_FUNCTION_ERROR_INJECTION is not set`), no dwc2/usbcore function is marked `ALLOW_ERROR_INJECTION`, and returning an error would abort a transfer, not hold it |
| Device-side delay (gadget NAKs) | controls the device, not the host's phases; needs a second board (excluded as a dependency) |
| Separate module touching registers | rejected (concurrent register ownership) |

None gives phase-boundary control, so a driver extension is the only way.

## Proposal: phase hold, debugfs-controlled, off by default

Scope: **control transfers on one selected device address, non-periodic
schedule, buffer-DMA mode** (the measured configuration). Everything else
behaves as today.

Mechanism (all under `hsotg->lock`):
- A per-controller hold mask {after_setup, after_data} and a target device
  address, set through `/sys/kernel/debug/usb/<dev>/phase_hold` (root only).
- In `dwc2_hcd_select_transactions`, a QH whose first QTD matches the target
  and whose `control_phase` is in the mask, and which has not been released
  for that phase, is skipped (stays on the inactive list). The channel was
  already returned by `dwc2_release_channel`, so nothing hardware-side is
  held.
- `echo release > phase_hold` marks held QTDs released and calls
  `dwc2_hcd_select_transactions`/`queue_transactions` — the same calls the
  release path makes today.
- Cancellation of a held transfer: unchanged dequeue path (idle QTD).
- Recovery: an hrtimer releases any hold after a bounded time (default
  100 ms, cap 1 s); disabling the knob releases everything; removing the
  device or unloading dwc2 goes through existing flush paths.
- Observability: a tracepoint (or the existing kprobes) on hold/release.

Build gating: `CONFIG_USB_DWC2_PHASE_HOLD` (default n) plus a module
parameter defaulting to disabled. Delivered as a rebuilt `dwc2.ko` for the
running kernel (dwc2 is a module there): no kernel replacement, and
reverting is unloading it and loading the stock module or rebooting.

Protocol correctness: tokens, PIDs and toggles stay hardware/driver
generated; only the start of the next phase is delayed. USB 2.0 sets no
host deadline between control stages; the device may wait or NAK. The
per-request timeout (hostlab-ctl uses 1 s) still applies through usbcore
and cancels a held transfer by the idle-QTD path.

Size estimate: ~150 lines in hcd_queue.c/hcd.c/debugfs.c, no change to the
IRQ handler's control flow.

## Risks and open questions

- A QH skipped by selection while others are queued must not starve the
  schedule: selection already iterates per QH; the held one is just not
  picked.
- `dwc2_hcd_qh_deactivate` and `continue_split` semantics for split
  transactions (FS device behind a HS hub) are out of scope; the knob must
  refuse split QHs.
- Descriptor DMA mode uses a different path (`hcd_ddma.c`); refuse when
  `dma_desc_enable`.
- Kernel API stability: debugfs only, explicitly unstable; no usbfs ioctl.

## Recommendation

**Justified as the next driver-level experiment, pending approval.** The
hold point needs no hardware ownership change, rides on existing
release/select/dequeue code, and replaces a measured impossibility
(userspace cannot reach phase boundaries). It is independent of any target
device. Smallest next step: write the patch against `359f37f`, build
`dwc2.ko` against the installed linux-headers on the Pi, and test only the
off state first (identical behaviour: rerun `hostlab-bench` and the H12
model check), then a single hold-and-release on the CH340.
