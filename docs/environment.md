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

Requirements: Raspberry Pi OS Lite 32-bit (the analysis used kernel
`rpi-6.18.y`), `python3` (expected to be preinstalled on Lite; check with
`python3 --version`), SSH access over Wi-Fi. The USB port is then free for
the device under test.

```
make armv6
scripts/deploy.sh pi@pizero.local            # copies to ~/dwc2-hostlab
ssh pi@pizero.local
sudo ~/dwc2-hostlab/scripts/pi/enable-dwc2.sh           # shows the change
sudo ~/dwc2-hostlab/scripts/pi/enable-dwc2.sh --apply   # backs up config.txt
sudo reboot
```

`dr_mode=host` keeps the core in host mode regardless of the ID pin. A
plain micro-USB OTG adapter or cable connects the device under test; the
port supplies VBUS from the Pi's 5 V rail, so use a powered hub for
anything drawing much current.

## First run on hardware

```
cd ~/dwc2-hostlab
sudo scripts/pi/prepare-session.sh           # modprobe usbmon/configs, mount debugfs/tracefs
sudo bin/hostlab-probe --pretty > probe-dwc2.json
bin/hostlab-ctl list                          # find the test device, e.g. 1:4
sudo bin/hostlab-session run --out run1 -- \
     bin/hostlab-ctl get-descriptor 1:4 --repeat 100
bin/hostlab-analyze run1 --json run1/analysis.json
sudo bin/hostlab-session run --out run2 -- \
     bin/hostlab-ctl cancel-sweep 1:4 --delays-us 0:500:10 --repeat 5
bin/hostlab-analyze run2 --json run2/analysis.json
```

Check before trusting the results:
- `probe.json`: `controller.driver == "dwc2"`, `summary.host_dma_mode`
  (H1), `summary.host_channels` (H2), `ftrace.hostlab_probes_missing` (H9).
- `analysis.json`: `channel_events.channel_numbers_seen` all below
  `host_channels`; every usbmon URB also has a `urb_enqueue` time (H7);
  `clock.ftrace_marker_offsets_ns` near 0 (H10).

Also take one `probe.json` *before* enabling the overlay, i.e. on dwc_otg.

## Recovery

hostlab changes nothing persistent on the Pi except the optional
`config.txt` line added by `enable-dwc2.sh --apply`.

| Situation | Action |
|---|---|
| Want dwc_otg back | `sudo scripts/pi/enable-dwc2.sh --revert && sudo reboot` (restores the newest backup) |
| Pi boots but USB is dead | Same. SSH runs over Wi-Fi, so it is unaffected |
| Pi does not come back on the network | Power off, put the SD card in another computer, open the FAT **boot** partition (`bootfs`), edit `config.txt`, delete the lines below `# dwc2-hostlab` (or copy a `config.txt.hostlab-*` backup over it) |
| A session crashed and left kprobes | `sudo bin/hostlab-session teardown` (removes only the `hostlab` group). A reboot also clears all kprobes |
| tracefs left at `trace_clock=mono` | Harmless. `echo local | sudo tee /sys/kernel/tracing/trace_clock` restores the default |
| Kernel oops / hang during a kprobe session | Reboot (kprobes are not persistent). Report it with `session.json` and `probe.json` |

Serial console (UART on GPIO14/15, `enable_uart=1`) is the fallback when
neither network nor display is available. A Saleae or USB-UART adapter can
watch it.

## Known blockers

| Blocker | Impact | Workaround / next step |
|---|---|---|
| No access to the Pi from the development environment | Nothing has run on real dwc2 hardware | Runbook above; results go to `docs/evidence/` |
| Docker VM kernel has no dummy_hcd / usbmon | No software-only end-to-end test | Issue draft 1 in [plan.md](plan.md#issue-drafts) |
| Official RPi kernel may be built with a different compiler than ours | Traceable-function list may differ (H9) | `hostlab-probe` reports the real list |
| CI workflow not executed on GitHub | Workflow syntax unverified by GitHub | Steps run locally with the same commands |
