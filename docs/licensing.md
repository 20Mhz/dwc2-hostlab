# Licensing and provenance review

Review date 2026-11-01, at the end of M1 work. This is an engineering review
of where the repository's content comes from, not legal advice.

## Summary

- Every source file in the repository was written for this project. No
  third-party source file or code fragment was copied in. Each source file
  carries `SPDX-License-Identifier: MIT`.
- A few files **restate Linux interface definitions** (ABI layouts, ioctl
  numbers, enum values, function names) for interoperability, citing the
  kernel file and line. No kernel function bodies are copied.
- `docs/evidence/` holds **records produced by third-party software**
  (Raspberry Pi kernel configuration, function-name lists, kernel log
  lines). That is measurement data, not code, but its origin should be
  stated (proposal 3).
- The **copyright line in `LICENSE` is a placeholder.** Who holds copyright
  is the owner's decision (proposal 1).
- The static ARMv6 binaries link **glibc (LGPL-2.1)** statically. Building and
  using them locally is fine. **Distributing** them (for example as release
  assets) brings LGPL obligations (proposal 2).

## Provenance by file group

| Path | Origin | Third-party material | Notes |
|---|---|---|---|
| `src/lib/*.c`, `include/hostlab/*.h` | written for this project | `backend_linux.c` includes `<linux/usbdevice_fs.h>` from the build system | That header is UAPI, `GPL-2.0 WITH Linux-syscall-note`; the note allows any userspace program to use it. It is not vendored into the repo |
| `src/lib/usbmon.c` | written for this project | byte offsets of `struct mon_bin_hdr` (`drivers/usb/mon/mon_bin.c:91-116`) | ABI layout restated for decoding, with citation |
| `src/tools/hostlab-mon.c` | written for this project | `struct mon_bin_stats`, `struct mon_bin_get`, `MON_IOC_MAGIC`, ioctl numbers (`mon_bin.c:35-46,131-140`) | The usbmon binary interface is documented for userspace in `Documentation/usb/usbmon.rst` but has no UAPI header, so every usbmon client declares it |
| `src/tools/hostlab-ctl.c` | written for this project | none | |
| `python/hostlab/tracefs.py` | written for this project | kernel function names, `enum dwc2_halt_status` names and values (`core.h`) | Facts needed to address the interface |
| `python/hostlab/preflight.py` | written for this project | Raspberry Pi revision-code field layout | From Raspberry Pi's public documentation |
| `python/hostlab/*.py` (others), `python/tests`, `tests/` | written for this project | none | Test fixtures are synthetic, built from formats in the cited sources |
| `scripts/`, `Makefile`, `docker/Dockerfile`, CI | written for this project | none | The Dockerfile installs Debian packages at build time and does not redistribute them |
| `docs/*.md` | written for this project | file:line citations of Linux (GPL-2.0) and Circle (GPL-3.0); short identifiers, no quoted code blocks | Circle was read for comparison only. No Circle code is in the repo (checked with `grep -ri circle src include python`) |
| `docs/evidence/rpi-6.18.59-*` | generated from the raspberrypi/linux tree (GPL-2.0) by `scripts/kernel/build-dwc2-module.sh` | resolved Kconfig values, function-name lists | Measurement data |
| `docs/evidence/m1-*/` | recorded on the owner's Pi Zero W | full `kernel-config.txt` from Raspberry Pi's kernel package, kernel log lines, USB descriptors of the root hub | Measurement data. Serial numbers, hostname and network addresses are excluded by `hostlab-evidence` |
| `docs/evidence/linux-vm-roothub-cancel-sweep.jsonl` | recorded on the development machine's Docker VM | none | |

## Authorship

Commits are authored under the owner's name. The code was written by an AI
assistant (Claude) at the owner's direction, and the commits carry a
`Co-Authored-By` trailer that says so. Whether, and how far, AI-generated
material is protected by copyright differs between jurisdictions. This
review does not resolve that question.

## Proposals (owner decision)

1. **Copyright line.** Options:
   - (a) keep `Copyright (c) 2026 Ronald Valenzuela`;
   - (b) use `Copyright (c) 2026 The dwc2-hostlab contributors` and add an
     `AUTHORS` file, which suits outside contributions better;
   - (c) take advice first if the project will be published under an
     organisation.

   Until a choice is made, the license stays marked provisional in the
   README.
2. **Static binaries.** Before publishing prebuilt ARMv6 binaries, do one of:
   - link dynamically against the target's glibc. The binaries then depend
     on the Pi's libc version, and `-march=armv6kz` still applies;
   - link statically against musl (MIT). This needs a musl cross toolchain
     in the Docker image;
   - ship the object files and link instructions alongside, as LGPL-2.1 §6
     allows.

   Source-only releases need none of this.
3. **Evidence attribution.** Add one line to `docs/evidence/README` (or to
   this file) stating that the kernel configuration and log data come from
   Raspberry Pi OS kernel builds (GPL-2.0) and are included as measurement
   records.
4. **Future kernel patches** (C1 tracepoints, or C2 if it is ever approved)
   are GPL-2.0. Keep them in a separate `kernel/` directory with their own
   license statement and `SPDX-License-Identifier: GPL-2.0` headers, and
   never mix them into the MIT tree.
