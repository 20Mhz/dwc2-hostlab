# Capability matrix

What can be **observed** and **controlled**, by which mechanism, on the
target kernel (Raspberry Pi OS, `rpi-6.18.y` @ `b3495ea`, kernel release
`6.18.59+`, `bcmrpi_defconfig`).

Kernel configuration facts below come from resolving `bcmrpi_defconfig` in
the real tree ([evidence/rpi-6.18.59-config-excerpt.txt](evidence/rpi-6.18.59-config-excerpt.txt));
function traceability comes from disassembling the built `dwc2.ko` and
usbcore objects for `__gnu_mcount_nc` call sites
([dwc2](evidence/rpi-6.18.59-dwc2-traceable-functions.txt),
[usbcore](evidence/rpi-6.18.59-usbcore-traceable-functions.txt)). Both are
reproducible with `scripts/kernel/build-dwc2-module.sh`.

**Caveat:** the official Raspberry Pi kernel package is built by the Raspberry
Pi Foundation, possibly with a different compiler. Inlining decisions, and so
the traceable-function list, can differ. `hostlab-probe` checks the real list
on the device (`available_filter_functions`). Everything in this file marked
"build" is **[C] for our build, [H] for the shipped kernel**.

---

## 1. Kernel facilities present on the target

| Facility | Config | Status | Notes |
|---|---|---|---|
| usbfs (`/dev/bus/usb/BBB/DDD`) | `CONFIG_USB=y` | ✅ | Needs root or a udev rule |
| usbmon text + binary | `CONFIG_USB_MON=m` | ✅ after `modprobe usbmon` | `/sys/kernel/debug/usb/usbmon/*`, `/dev/usbmonN` |
| debugfs | `CONFIG_DEBUG_FS=y` | ✅ | dwc2 files under `/sys/kernel/debug/usb/<dev>/` (dwc2 only) |
| ftrace function / function_graph | `CONFIG_FUNCTION_TRACER=y`, `CONFIG_FUNCTION_GRAPH_TRACER=y`, `CONFIG_DYNAMIC_FTRACE=y` | ✅ | 273 dwc2 + 503 usbcore functions traceable (build) |
| kprobe events (with register fetch args) | `CONFIG_KPROBES=y`, `CONFIG_KPROBE_EVENTS=y` | ✅ | No BTF/DWARF (`CONFIG_DEBUG_INFO` unset): struct fields need numeric offsets |
| eBPF on kprobes | `CONFIG_BPF_EVENTS=y`, `CONFIG_BPF_SYSCALL=y` | ✅ (kernel side) | Toolchain (bpftrace/BCC) on ARMv6 is heavy; not a first choice |
| Event histograms | `CONFIG_HIST_TRIGGERS` | ❌ unset | Latency histograms must be computed offline |
| Dynamic debug | `CONFIG_DYNAMIC_DEBUG` | ❌ unset | **All dwc2 `dev_dbg` are compiled out** on the stock kernel |
| dwc2 verbose debug | `CONFIG_USB_DWC2_DEBUG/VERBOSE` | ❌ unset | Requires a rebuilt module |
| USB tracepoints (usbcore) | — | ❌ absent in 6.18 | Mainline 7.3 has only `usb_set_device_state`, `usb_alloc_dev` |
| dwc2 tracepoints | — | ❌ absent in mainline too | Candidate upstream contribution (M5) |
| `/proc/config.gz` | `CONFIG_IKCONFIG=m` | ✅ after `modprobe configs` | Used by `hostlab-probe` |
| Benign test peripheral support | `CONFIG_USB_ZERO=m`, `CONFIG_USB_CONFIGFS=m`, `CONFIG_USB_TEST=m` | ✅ | A second Pi Zero in gadget mode running `g_zero` is a deterministic test device; `usbtest` on the host exercises control/unlink paths |
| `dummy_hcd` | `CONFIG_USB_DUMMY_HCD` | ❌ unset | Software-only host+gadget loopback needs a custom kernel (VM) |
| Timer granularity | `CONFIG_HZ=100` | — | usbfs / `usb_control_msg` timeouts round to 10 ms |
| `/dev/mem` | `CONFIG_STRICT_DEVMEM` unset | ⚠️ possible | **Prohibited by project policy** while a driver owns the core |

---

## 2. Observability by behaviour

Legend: ✅ available without kernel changes · 🔶 available with derived
struct offsets (kprobe fetch args) · 🧩 needs a dwc2 patch · ❌ not possible
with this hardware/software · — not applicable.

| Behaviour | usbfs (user) | usbmon | debugfs (dwc2) | ftrace function | kprobe events | dwc2 patch | Logic analyzer (FS/LS only) |
|---|---|---|---|---|---|---|---|
| URB submit / completion, status, length, setup bytes, data | own URBs only | ✅ all URBs | — | ✅ timing (`usb_hcd_submit_urb`, `usb_hcd_giveback_urb`) | ✅ URB ptr = usbmon `id` | — | — |
| URB → dwc2 enqueue / dequeue | — | — | — | ✅ `_dwc2_hcd_urb_enqueue`, `_dwc2_hcd_urb_dequeue` | ✅ `urb=%r1`, `status=%r2` | — | — |
| IRQ → BH giveback latency | — | ❌ (only BH side) | — | ✅ `dwc2_host_complete` vs `__usb_hcd_giveback_urb` | ✅ | — | — |
| Control phase boundaries | ❌ | ❌ | ❌ | ✅ by sequence of `dwc2_hc_start_transfer` / `dwc2_hc_xfercomp_intr` per URB | 🔶 `qtd->control_phase` | 🧩 tracepoint | ✅ tokens on the wire |
| Channel number used per phase | ❌ | ❌ | racy snapshot | — | ✅ `chan->hc_num` is offset 0 of `struct dwc2_host_chan` | 🧩 | — |
| Channel programming (`HCCHAR`, `HCTSIZ`, PID, length) | ❌ | ❌ | racy `regdump` snapshot | — | 🔶 `chan->data_pid_start`, `xfer_len`, `ep_is_in` | 🧩 | partly (PIDs on wire) |
| Channel halt + reason | ❌ | ❌ | ❌ | ✅ `dwc2_hc_halt`, `dwc2_release_channel` | ✅ `halt_status=%r2` / `%r3` | 🧩 | — |
| Raw `HCINT` causing a halt | ❌ | ❌ | ❌ | — | 🔶 `chan->hcint` at `dwc2_hc_xfercomp_intr` etc. | 🧩 | — |
| NAK / XACTERR / STALL handling | status only | status only | — | ✅ handler entry (`dwc2_hc_{nak,xacterr,stall}_intr`) | ✅ | 🧩 | ✅ handshakes on wire |
| HW NAK retries inside one channel run (buffer DMA, IN) | ❌ | ❌ | ❌ | ❌ (no IRQ) | ❌ | ❌ (HW-internal) | ✅ |
| Per-packet timing on the bus | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ FS/LS only |
| Cancellation: phase reached when cancelled | ❌ | ❌ | ❌ | ✅ by sequence | 🔶 `qtd->control_phase` at `_dwc2_hcd_urb_dequeue` | 🧩 | ✅ |
| Cancellation: bytes moved before cancel | ❌ (reports 0, H3) | ❌ | ❌ | — | 🔶 | 🧩 | ✅ |
| Port reset timing | — | — | — | ✅ `hub_port_reset` (usbcore) | ✅ | — | ✅ SE0 duration |
| Enumeration request sequence | — | ✅ | — | ✅ `hub_port_init`, `usb_new_device` | ✅ | — | ✅ |
| Controller config (`host_dma`, channels, FIFOs, core rev) | — | — | ✅ `params`, `hw_params` | — | — | — | — |
| FIQ-handled work (dwc_otg only) | — | — | — | ❌ | ❌ | — | ✅ |

---

## 3. Controllability

| Operation | Existing interface | Granularity / limits | Ownership-safe? |
|---|---|---|---|
| Standard control transfer | usbfs `USBDEVFS_CONTROL`, async `SUBMITURB` | whole transfer | ✅ |
| Cancel a transfer | usbfs `USBDEVFS_DISCARDURB` (= `usb_kill_urb`) | whole URB, timing not phase-aligned | ✅ |
| Timeout | usbfs timeout field | ≥ 10 ms steps | ✅ |
| Device reset (+ re-enumeration) | usbfs `USBDEVFS_RESET` | device | ✅ (may disturb other interface drivers) |
| Clear halt / reset toggle | `USBDEVFS_CLEAR_HALT`, `USBDEVFS_RESETEP` | endpoint | ✅ |
| Detach kernel driver | `USBDEVFS_DISCONNECT_CLAIM` / `USBDEVFS_IOCTL(DISCONNECT)` | interface | ✅ |
| USB electrical test modes | dwc2 debugfs `testmode` (gadget only); root hub `SetPortFeature(PORT_TEST)` via `dwc2_hcd_hub_control` | port | ✅ through usbcore only |
| DMA vs slave mode | none (no DT/module param) | — | needs rebuilt module |
| Execute only selected control phases | **none** | — | only as a dwc2 extension (see [kernel-integration-options.md](kernel-integration-options.md)) |
| Choose PID / emit malformed tokens / bad CRC | **none, and not supported by the hardware** | core generates tokens and CRC | ❌ |
| Per-packet scheduling within a (micro)frame | **none, hardware-owned** | — | ❌ |

---

## 4. Hardware capabilities to detect at runtime

`hostlab-probe` reports each of these; unknown values are reported as
`null` with a reason, never guessed.

| Capability | Source on target |
|---|---|
| Bound driver (`dwc2` / `dwc_otg` / none) | `/sys/bus/platform/drivers/{dwc2,dwc_otg}/*`, DT `compatible` |
| `dr_mode` | `/sys/kernel/debug/usb/<dev>/dr_mode`, DT |
| Core revision (`GSNPSID`) | `hw_params: snpsid` |
| Host channels | `hw_params: host_channels`, `params: host_channels` |
| DMA mode | `params: host_dma`, `dma_desc_enable`; `hw_params: arch` |
| Max transfer size / packet count | `params`, `hw_params` |
| usbmon available / loaded | `/sys/module/usbmon`, `/dev/usbmon*`, debugfs |
| ftrace / function filters for dwc2 | `/sys/kernel/tracing/available_filter_functions` |
| kprobe events | `/sys/kernel/tracing/kprobe_events` |
| Dynamic debug | `/proc/dynamic_debug/control` or debugfs |
| Kernel config | `/proc/config.gz` |
