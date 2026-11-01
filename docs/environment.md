# Development environment, deployment and recovery

## Host (development machine)

Verified on macOS (Apple Silicon) with Docker. Everything that compiles for
the Pi runs inside the image built from [docker/Dockerfile](../docker/Dockerfile)
(Debian bookworm):

| Purpose | Toolchain | Why |
|---|---|---|
| Userspace tools for the Pi | `arm-linux-gnueabi-gcc -march=armv6kz -marm -static` | Debian's armhf libc targets ARMv7; armel's is ARMv5TE, so a static link runs on the ARM1176 without depending on the Pi's glibc |
| Emulated test runs | `qemu-arm-static -cpu arm1176` | traps ARMv7-only instructions; a negative control in `scripts/build-armv6.sh` proves it |
| Kernel / modules | `crossbuild-essential-armhf` (`arm-linux-gnueabihf-`) | as in the Raspberry Pi kernel documentation for 32-bit kernels |

The kernel tree lives in a named Docker volume (`dwc2-hostlab-ksrc`) because
the Linux tree has file names that differ only in case, which the default
case-insensitive APFS cannot hold.

```
make                 # native build + mock-backend tests (any POSIX host)
make test            # C + Python tests
make armv6           # static ARMv6 binaries, tests under arm1176 emulation
scripts/kernel/build-dwc2-module.sh   # RPi dwc2.ko + traceability evidence
```

## Target (Pi Zero W v1.1)

Verified on hardware on 2026-11-01:

| Item | Value |
|---|---|
| Board | Raspberry Pi Zero W Rev 1.1, revision code `9000c1` (BCM2835, 512 MB, Sony UK) |
| OS | Raspberry Pi OS Lite 32-bit, Raspbian 13 (trixie) |
| Kernel | `6.12.47+rpt-rpi-v6`, package `1:6.12.47-1+rpt1`, raspberrypi/linux `359f37f` ([references.md](references.md)) |
| Boot partition | `/boot/firmware` (`config.txt`, `cmdline.txt`) |
| Kernel config | `/boot/config-6.12.47+rpt-rpi-v6`; `/proc/config.gz` after `modprobe configs` |
| Default USB driver | `dwc_otg` (built in), `dwc2.ko` and `usbmon.ko` shipped as modules |
| Python | 3.13.5 (preinstalled); the tools need only the standard library |
| Access | SSH over Wi-Fi (SDIO, independent of the USB controller) |

The analysis in M0 used `rpi-6.18.y`. The dwc2 host code (`hcd_intr.c`,
`hcd.h`) is identical in 6.12.47; every probed function keeps its prototype,
`hc_num` is still the first member of `struct dwc2_host_chan` and
`enum dwc2_halt_status` is unchanged. All 18 probe symbols exist under their
own names in the shipped `dwc2.ko` (no `.isra`/`.constprop` clones).

Stock trixie `config.txt` already contains `dtoverlay=dwc2,dr_mode=host`,
but under `[cm5]`, so it does not apply to a Zero W. `enable-dwc2.sh` only
counts lines with no filter or under `[all]`, `[pi0]` or `[pi0w]`.

```
make armv6
scripts/deploy.sh pizero                     # ssh alias or user@host; copies to ~/dwc2-hostlab
ssh pizero
cd ~/dwc2-hostlab
bin/hostlab-preflight                        # read-only; run with sudo for debugfs/tracefs
sudo scripts/pi/enable-dwc2.sh               # shows overlay state, changes nothing
sudo scripts/pi/enable-dwc2.sh --apply       # backs up config.txt, appends 3 lines
sudo reboot
```

`dr_mode=host` keeps the core in host mode regardless of the ID pin. A
micro-USB OTG adapter on the port marked **USB** (not PWR IN) connects the
device under test; the port supplies VBUS from the Pi's 5 V rail, so use a
powered hub for anything drawing much current.

## First run on hardware

Before the overlay, keep a baseline (used as the `dwc_otg` state):

```
mkdir -p runs/00-dwc_otg && sudo bin/hostlab-preflight --json runs/00-dwc_otg/preflight.json
```

With dwc2 bound and a test device attached (`bin/hostlab-ctl list`):

```
sudo scripts/pi/m1-run.sh 1:2                 # BUS:DEV of the test device
```

`m1-run.sh` loads usbmon, runs the preflight, records 100
GET_DESCRIPTOR(DEVICE) requests with kprobes and the same 100 without (for
H8), and writes an evidence bundle to `evidence/m1-DATE/`
([trace-format.md](trace-format.md#evidence-bundle)). Its
`test-summary.md` lists every M1 check and hypothesis as PASS, FAIL, BLOCKED
or INCONCLUSIVE with the file that supports it. Recovery snapshots in
`runs/05-reverted/` and `runs/06-reapplied/` (preflight JSON taken after
`--revert` + reboot and after `--apply` + reboot) are included when present.

Manual equivalents:

```
sudo scripts/pi/prepare-session.sh           # modprobe usbmon/configs, mount debugfs/tracefs
sudo bin/hostlab-session run --out run1 -- bin/hostlab-ctl get-descriptor 1:2 --repeat 100
bin/hostlab-analyze run1 --json run1/analysis.json
```

## Recovery

hostlab changes nothing persistent on the Pi except the optional
`config.txt` line added by `enable-dwc2.sh --apply`.

| Situation | Action |
|---|---|
| Want dwc_otg back | `sudo scripts/pi/enable-dwc2.sh --revert && sudo reboot`. Removes only the hostlab block (validated on hardware 2026-11-01: `config.txt` byte-identical to the pre-apply backup, dwc_otg bound after reboot) |
| Pi boots but USB is dead | Same. SSH runs over Wi-Fi, so it is unaffected |
| Pi does not come back on the network | Power off, put the SD card in another computer, open the FAT **boot** partition (`bootfs`), edit `config.txt` and delete the three lines starting at `# dwc2-hostlab` (or copy a `config.txt.hostlab-*` backup over it) |
| A session crashed and left kprobes | `sudo bin/hostlab-session teardown` (removes only the `hostlab` group). A reboot also clears all kprobes |
| tracefs left at `trace_clock=mono` | Harmless. `echo local | sudo tee /sys/kernel/tracing/trace_clock` restores the default |
| Kernel oops / hang during a kprobe session | Reboot (kprobes are not persistent). Report it with `session.json` and `probe.json` |

Serial console (UART on GPIO14/15, `enable_uart=1`) is the fallback when
neither network nor display is available. A Saleae or USB-UART adapter can
watch it.

## Known blockers

| Blocker | Impact | Workaround / next step |
|---|---|---|
| No USB peripheral attached to the Pi during the 2026-11-01 session | No dwc2 channel events yet: H7, H8 and the M1 trace/analysis checks are open | Attach a device, run `scripts/pi/m1-run.sh` |
| Docker VM kernel has no dummy_hcd / usbmon | No software-only end-to-end test | Issue draft 1 in [plan.md](plan.md#issue-drafts) |
| CI workflow not executed on GitHub | Workflow syntax unverified by GitHub | Steps run locally with the same commands |
