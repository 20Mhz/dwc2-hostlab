# dwc2-hostlab

An open-source toolkit for instrumenting and studying USB **host** behaviour on
the Synopsys DesignWare HS OTG (DWC2) controller under Linux. The primary target
is the Raspberry Pi Zero W v1.1 (BCM2835, ARM1176JZF-S).

**Status: early (v0.1.0-dev).** Source analysis is complete and the first
non-invasive tools are built and unit-tested. **Nothing has run on a Pi yet**:
see [docs/plan.md](docs/plan.md) for what is verified and what is still a
hypothesis.

## What it does

- **Explains** how Linux and the DWC2 core split the work of a host transfer,
  with file and line references:
  [architecture](docs/dwc2-architecture.md),
  [control transfers](docs/usb-control-transfers.md),
  [capability matrix](docs/capability-matrix.md),
  [integration options](docs/kernel-integration-options.md).
- **Records** one experiment from three sides and joins them by kernel URB
  address. Userspace (usbfs), usbmon, and kprobe events on the dwc2 / usbcore
  host path, which show channel starts, halts, releases, phase transitions
  and givebacks. No kernel patch, no register access.
- **Reports** per URB: channel runs, inferred control phases and the channels
  they used, scheduling and giveback latencies, and, for cancelled transfers,
  the phase reached at dequeue and the channel-release latency.

```
sudo bin/hostlab-session run --out run1 -- bin/hostlab-ctl get-descriptor 1:4 --repeat 100
bin/hostlab-analyze run1
```

## Key findings so far

- Stock Raspberry Pi OS drives the Zero W's controller with the downstream
  **dwc_otg** driver; mainline **dwc2** needs `dtoverlay=dwc2`.
- dwc2 sequences SETUP → DATA → STATUS **in software**. Each phase is a
  separate host-channel run, and the channel is released between phases.
  Tokens, CRC, in-run toggles and (in buffer-DMA mode) NAK retries are done
  by the hardware.
- The stock RPi kernel has kprobes, kprobe events and the function tracer,
  but **no dynamic debug**, so all dwc2 `dev_dbg` output is compiled out. Neither
  dwc2 nor usbcore 6.18 has tracepoints.
- usbmon's binary `id` is the kernel `struct urb *`, which gives exact joins
  with kprobe arguments.
- A successful `USBDEVFS_DISCARDURB` does not mean the transfer was
  cancelled. On a real kernel (xHCI root hub) 111 of 400 accepted discards
  still completed normally ([evidence](docs/evidence/linux-vm-roothub-cancel-sweep.jsonl)).
- Controlling individual phases would need a dwc2 patch. That option is
  analysed but **gated** and not implemented. Packet- or token-level control is
  impossible on this hardware.

## Components

| Path | What |
|---|---|
| `include/hostlab/`, `src/lib/` | C library: versioned transport API (`usb_backend.h`), usbfs and mock backends, usbmon decoder, safety policy |
| `src/tools/hostlab-ctl.c` | control transfers, cancellation sweeps, reset, device listing |
| `src/tools/hostlab-mon.c` | usbmon binary capture → JSONL |
| `python/hostlab/` | `probe` (capabilities), `session` (orchestrates a capture, manages kprobes), `analyze` (joins and reports) |
| `scripts/` | ARMv6 build, kernel evidence build, deploy, Pi setup/revert |
| `docs/` | analysis, plan, environment and recovery, trace format, evidence |

## Build and test

```
make && make test          # native; mock backend; C + Python tests
make armv6                 # static ARMv6 binaries in Docker, tested under qemu -cpu arm1176
```

Deployment, first hardware run and recovery: [docs/environment.md](docs/environment.md).

## Safety and scope

- Default policy: standard device-to-host requests only, never to hubs.
  Writes, class/vendor requests, hub access and resets each need an explicit
  flag.
- No tool writes controller registers, uses `/dev/mem`, or uses dwc_otg's
  register sysfs. kprobes are observers, and teardown removes only hostlab's
  own events.
- This project is a generic USB host research toolkit. It contains no
  device-specific payloads or sequences and makes no assumptions about any
  particular target device.

## Supported hardware

| Board | Driver | Status |
|---|---|---|
| Raspberry Pi Zero W v1.1 | dwc2 (`dtoverlay=dwc2,dr_mode=host`) | primary target, **untested on hardware** |
| Raspberry Pi Zero W v1.1 | dwc_otg (default) | usbfs/usbmon layers only |
| Any Linux host | any HCD | `hostlab-ctl`, `hostlab-mon` (usbfs/usbmon); smoke-tested on an xHCI root hub |

## License

MIT for this repository's code (provisional, see [LICENSE](LICENSE)). The
documentation refers to GPL-2.0 kernel sources by path and line and does not
copy them. Kernel patches proposed later would be GPL-2.0.
