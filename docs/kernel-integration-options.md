# Kernel integration options

Question: how should a research framework observe and (experimentally)
control DWC2 host behaviour while keeping driver ownership, kernel stability
and USB protocol correctness?

Evidence: [dwc2-architecture.md](dwc2-architecture.md),
[usb-control-transfers.md](usb-control-transfers.md),
[capability-matrix.md](capability-matrix.md).

---

## Option A — Userspace on existing interfaces (usbfs + usbmon + tracefs)

**What it covers [C]:** everything at URB granularity (usbfs, usbmon), plus
phase/channel/halt timing through ftrace and kprobe events on functions that
survive inlining on the target build (`dwc2_hc_start_transfer`,
`dwc2_hc_halt`, `dwc2_release_channel`, `dwc2_hc_xfercomp_intr`,
`dwc2_host_complete`, `_dwc2_hcd_urb_{en,de}queue`, `dwc2_hc_n_intr`,
`dwc2_hc_{nak,ack,nyet,stall,xacterr,babble,ahberr,frmovrun}_intr`).

**Correlation hook [C]:** usbmon's binary `id` is the `struct urb *` value
(`drivers/usb/mon/mon_bin.c:577`). A kprobe on `_dwc2_hcd_urb_enqueue`
fetching `%r1` (second argument, ARM AAPCS) returns the same value, so
usbmon records and kernel trace events can be joined exactly, without struct
offsets.

| + | − |
|---|---|
| Zero kernel code; works on the stock Raspberry Pi OS kernel | Cannot control phases |
| Same tooling works on dwc_otg at the usbfs/usbmon layer | Kprobe struct-field fetches need offsets derived from the exact build (no BTF) |
| Upstream-neutral, publishable immediately | ftrace function names depend on inlining (must be checked on target) |
| Low risk: kprobes on a running driver are read-only observers | Probe overhead on a 1 GHz ARM1176 perturbs timing (to be measured) |

## Option B — Separate loadable module

Sub-options:

- **B1: module touching DWC2 registers** — **Rejected.** dwc2 owns the
  register file under `hsotg->lock` and keeps channel state
  (`free_hc_list`, `qh->channel`, `halt_pending`) that must match hardware
  (`dwc2/hcd_intr.c:676-773`, `dwc2/hcd.c:766-919`). A second writer would
  desynchronise it; a channel the module enables would raise `HAINT` for a
  channel whose `hc_ptr_array` entry has no QH (`hcd_intr.c:2029-2048`:
  "Interrupt on disabled channel"). Reads would be racy and add nothing over
  debugfs `regdump`. The task brief already prohibits this.
- **B2: module calling dwc2 internals** — **Not possible cleanly.** The
  only symbol dwc2 exports is `dwc2_pci_ids` (`params.c:376`); nothing on
  the host path is exported, and `struct dwc2_hsotg` is private to the
  driver.
- **B3: module as a USB *driver* (claims an interface, uses `usb_*` API)**
  — Possible but adds nothing over usbfs for control transfers, and needs
  per-device binding.
- **B4: module registering kprobes/tracepoints programmatically** —
  Equivalent to Option A's `kprobe_events`, with more risk and a build
  dependency on exact kernel headers.

**Conclusion:** B offers no capability A lacks, except to violate ownership.

## Option C — dwc2 driver extension (patch)

Two separable parts:

### C1: tracepoints in dwc2 (observation only)

Add `drivers/usb/dwc2/trace.h` with events at channel init / start / halt /
release, control-phase transition, URB enqueue / dequeue / giveback, port
reset. Precedent: dwc3 (`drivers/usb/dwc3/trace.h`) and cdns3 already do
this upstream.

| + | − |
|---|---|
| Stable event names and fields, independent of inlining and struct layout | Needs a rebuilt dwc2 module on the Pi (overlay already uses `dwc2.ko` as a module: `CONFIG_USB_DWC2=m`) |
| Cheap when disabled (static keys) | Must be maintained against `rpi-6.18.y` until accepted |
| **Realistic upstream candidate** (generic value for all dwc2 users) | |

### C2: experimental phase control (control path)

Phases are sequenced in software inside dwc2 (`dwc2_hc_xfercomp_intr`,
`hcd_intr.c:1003-1023`), so a driver extension *can* stop a control transfer
after a chosen phase, or delay between phases, by reusing the existing
dequeue / release machinery under `hsotg->lock`.

| Requirement from the brief | How it could be met | Open risk |
|---|---|---|
| Driver ownership | Implemented inside dwc2, per-URB, completed through `dwc2_host_complete` with a distinct status | none beyond normal driver code |
| Kernel stability | Reuse the `URB_DEQUEUE` halt path, which already handles freeing a QTD mid-transfer | H4 (DMA after giveback) applies equally |
| Protocol correctness | A host may abandon a control transfer; the next SETUP restarts the control pipe (USB 2.0 §8.5.3 — spec not re-checked in this session) | Device-side behaviour on abandoned transfers is device-specific |
| Isolation, disabled by default | Kconfig option default `n` + debugfs arming knob scoped to one device address, auto-disarm after N uses | — |

**C2 is not proposed for implementation now.** Its independent engineering
value (host-side robustness testing of devices against abandoned control
transfers) is real but narrow. It is also the capability downstream
consumers would most want, and the brief says not to grow scope for them.
Decision gate: M3, after C1 data exists, with explicit owner approval.

Out of reach for any option **[C]**: choosing token PIDs freely, sending
malformed packets or bad CRCs, per-packet timing within a frame. The core
generates these (see the hardware/software table in
[dwc2-architecture.md §7](dwc2-architecture.md#7-hardware-vs-software-responsibilities)).

---

## Answer to the critical engineering question

> Can DWC2 expose individual USB Host operations through an experimental
> Linux interface while preserving driver ownership, kernel stability and USB
> protocol correctness?

- **Transfer-level operations** (control transfer, cancel, timeout, reset):
  **yes, already**, through usbfs. No new interface is needed.
- **Observation of phase/channel-level operations**: **yes, without kernel
  changes** on the stock RPi kernel through ftrace + kprobe events (Option A).
  Durably, through dwc2 tracepoints (C1).
- **Control of individual phases**: **only through a dwc2 patch (C2)**. It
  looks feasible within the stated constraints, but this is not demonstrated
  yet and it is gated.
- **Packet/token-level operations**: **no.** Not with this controller under
  any driver, because the hardware owns them.

## Recommendation

1. **Now (M2):** Option A. Build the toolkit on usbfs + usbmon + tracefs,
   with exact URB correlation through the `urb` pointer. No kernel code.
2. **M3:** C1, dwc2 tracepoints, as a small, upstream-shaped patch carried
   out-of-tree against `rpi-6.18.y` and rebased on mainline for submission.
   The Option A tooling consumes the tracepoints when present and falls
   back to kprobes otherwise.
3. **M3 gate:** decide on C2 with owner approval and a written justification
   of independent value. If approved, ship it behind Kconfig `default n`
   plus a debugfs arm/disarm knob.
4. **Never:** B1, `/dev/mem`, or the dwc_otg `regoffset`/`regvalue` write
   path.
