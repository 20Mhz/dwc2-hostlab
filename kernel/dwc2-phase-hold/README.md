# dwc2 experimental control-phase hold

**License: GPL-2.0** (a modification of the Linux dwc2 driver). This
directory is not covered by the repository's MIT license.

Patch against raspberrypi/linux `359f37f` (6.12.47, the Raspberry Pi OS
trixie kernel `6.12.47+rpt-rpi-v6`), `drivers/usb/dwc2`. Design and rationale:
[docs/phase-control-feasibility.md](../../docs/phase-control-feasibility.md);
results: [docs/m3-results.md](../../docs/m3-results.md#5-phase-hold-patch).

## What it does

Holds a control transfer of one device address between its stages. After
SETUP (or DATA) completes, dwc2 has already released the host channel and put
the QH back on the non-periodic inactive schedule; the hold makes
`dwc2_hcd_select_transactions()` skip that QH, so the next stage gets no
channel until it is released. Nothing in the controller is held during a
hold, and cancelling a held URB uses dwc2's existing not-in-process dequeue
path.

- Compiled in only with `CONFIG_USB_DWC2_PHASE_HOLD` (default n; the
  out-of-tree `Kbuild` here defines it).
- Inert unless the module parameter `phase_hold=1` is given: without it the
  debugfs file does not exist and `dev_addr` stays 0.
- Control: `/sys/kernel/debug/usb/<controller>/phase_hold` (root, 0600)

```
echo "dev=2 mask=data timeout_ms=100" > phase_hold   # hold before DATA
echo "dev=2 mask=status" > phase_hold                 # hold before STATUS
echo "dev=2 mask=data,status timeout_ms=50" > phase_hold
echo release > phase_hold                             # end every hold now
echo off > phase_hold                                 # clear and end every hold
cat phase_hold   # dev=2 mask=data, timeout_ms=100 held=0 holds=.. releases=.. timeouts=..
```

- Every hold ends after `timeout_ms` (1..1000, default 100) at the latest;
  usbcore's own request timeout or a cancel still ends a held URB earlier.
- Only control transfers, no split transactions, not in descriptor-DMA mode.
- Holds begin/end in `dwc2_phase_hold_begin()` / `dwc2_phase_hold_pass()`
  (noinline, traceable).

## Build, load, revert (on the Pi, as root)

```
kernel/dwc2-phase-hold/phase-hold.sh build DWC2_SRC   # pristine 359f37f drivers/usb/dwc2/*
kernel/dwc2-phase-hold/phase-hold.sh load             # patched driver, hold inert
kernel/dwc2-phase-hold/phase-hold.sh load hold        # patched driver, phase_hold=1
kernel/dwc2-phase-hold/phase-hold.sh stock            # back to the installed driver
kernel/dwc2-phase-hold/phase-hold.sh status
```

The module is built against the installed `linux-headers` and loaded with
`insmod` from `/tmp`; nothing is installed, so **a reboot always restores the
stock driver**. Loading it taints the kernel (out-of-tree module, flag 4096)
until the next reboot. USB is briefly unavailable while the driver is
swapped; SSH over Wi-Fi does not depend on it.

`hold-experiments.sh` runs the bounded experiments (A timeout, B explicit
release, C cancel while held, D request timeout while held);
`hold_release.py` is its release helper.

## Versions

| File | State |
|---|---|
| `0001-usb-dwc2-experimental-control-phase-hold.patch` | v2: per-transfer deadline, timer re-armed for the earliest one |
| `history/v1-shared-timer.patch` | v1, reconstructed: one timer armed by the first hold of a batch. A cancelled hold left it running, so it released a later transfer early (4 of 100 in stage C). Fixed in v2 |
