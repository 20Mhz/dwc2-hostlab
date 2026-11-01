# Source references

All findings in `docs/` cite the exact trees below. Line numbers refer to the
**mainline** tree unless a path is prefixed with `rpi:`.

Inspected on 2026-11-01.

| Tree | Repository | Branch | Commit | Version |
|---|---|---|---|---|
| mainline | https://github.com/torvalds/linux | master | `46fec1c3029b552ec63b2d01af9d43a386f043ea` (2026-11-01, "Merge tag 'sched-urgent-2026-11-01'") | Makefile: 7.3.0 |
| rpi | https://github.com/raspberrypi/linux | rpi-6.18.y (default branch) | `b3495ea401d709af0701a85babcba5e3d788a602` (2026-10-28) | Makefile: 6.18.59 |
| rpi-6.12.47 | https://github.com/raspberrypi/linux | (commit of the running kernel) | `359f37f0faefb712add32a39f98751aea67d5c1f` (2025-09-15) | Makefile: 6.12.47 |
| circle | https://github.com/rsta2/circle | master | `7a81e1b480fe7d19fc1fc695b89bff0a0848f44b` (2026-09-25, "Merge branch 'hotfix-51.1.1'") | Step 51.1.1 |

The trees were fetched as shallow, blob-filtered sparse checkouts containing:

- mainline: `drivers/usb/{dwc2,core,mon}`, `include/linux/usb*`,
  `include/uapi/linux/usbdevice_fs.h`, `Documentation/{usb,driver-api/usb,trace}`
- rpi: `drivers/usb/{dwc2,core,mon}`, `drivers/usb/host/dwc_otg`,
  `arch/arm/boot/dts/{broadcom,overlays}` (Zero W and dwc2 related),
  `arch/arm/configs/bcmrpi_defconfig`, `kernel/trace/Kconfig`, `lib/Kconfig.debug`
- circle: `lib/usb/dwhci*`, `include/circle/usb/dwhci*`

A full RPi tree at the same commit is built by
`scripts/kernel/build-dwc2-module.sh`, whose outputs (resolved `.config`,
`dwc2.ko`, symbol table, ftrace-able function list) are recorded in
`docs/evidence/`.

### The kernel actually running on the target

The Pi Zero W used for M1 runs `6.12.47+rpt-rpi-v6` (Raspberry Pi OS
trixie package `1:6.12.47-1+rpt1`). Its Debian changelog
(`/usr/share/doc/linux-image-6.12.47+rpt-rpi-v6/changelog.Debian.gz`) names
"Linux commit: 359f37f0faefb712add32a39f98751aea67d5c1f", fetched sparsely
(`drivers/usb/{dwc2,core,mon}`, the Zero W DTS and the dwc2 overlay) to
check the M0 findings against it. Differences that matter, 6.18.59 vs 6.12.47:

| File | Lines differing | Relevance |
|---|---|---|
| `dwc2/hcd_intr.c`, `dwc2/hcd.h` | 0 | channel state machine, phase sequencing, probe targets: identical |
| `dwc2/hcd.c` | 111 | power/PM changes; probed prototypes and `HCD_BH` flag identical |
| `dwc2/core.h` | 23 | backup/restore register helpers; `enum dwc2_halt_status` identical |
| `core/hcd.c` | 69 | root-hub enqueue signature, DMA sync, giveback comment; probed functions keep their prototypes |

The shipped `dwc2.ko` and the running kernel's `available_filter_functions`
were checked directly: all 18 hostlab probe symbols are present and
traceable (`docs/evidence/m1-20261101-partial/probe-capabilities.json`).
`usb_kill_urb` additionally has a `usb_kill_urb.part.0` clone (GCC partial
inlining); the probe on `usb_kill_urb` still sees every call.

## Which tree matters for the target

The Pi Zero W on Raspberry Pi OS runs the **rpi** kernel, not mainline. The
mainline tree is used because it is where upstream proposals would land and
because the dwc2 host code is nearly identical (see
[dwc2-architecture.md § RPi delta](dwc2-architecture.md#rpi-downstream-delta)).

## Documentation consulted

- Linux USB host-side API: https://docs.kernel.org/driver-api/usb/
- usbmon: `Documentation/usb/usbmon.rst` (mainline tree above)
- usbfs: `include/uapi/linux/usbdevice_fs.h`, `drivers/usb/core/devio.c`
- Raspberry Pi overlay README: `rpi:arch/arm/boot/dts/overlays/README`
