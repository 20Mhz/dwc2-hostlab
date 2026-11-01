# Control transfers through usbcore and dwc2

Evidence base and labels as in [dwc2-architecture.md](dwc2-architecture.md)
(**[C]** confirmed from source, **[H]** hypothesis). Line numbers are
mainline `46fec1c`; `rpi:` marks the Raspberry Pi tree.

---

## 1. End-to-end path of one control URB

Userspace `USBDEVFS_CONTROL` (synchronous) as the example:

| # | Context | Function | Location | What happens |
|---|---|---|---|---|
| 1 | process | `usbdev_ioctl` → `do_proc_control` | `core/devio.c:~1169-1235` | Copies setup + data into a kernel buffer, builds a URB, logs via `usbfs_snoop` if enabled |
| 2 | process | `usbfs_start_wait_urb` | `core/devio.c:1139-1165` | `usb_submit_urb`, then `wait_for_completion_killable_timeout(timeout)` |
| 3 | process | `usb_hcd_submit_urb` | `core/hcd.c:1519` | **usbmon `S` event** (`hcd.c:1527`); maps setup + data for DMA |
| 4 | process | `_dwc2_hcd_urb_enqueue` | `dwc2/hcd.c:4613` | Allocates `dwc2_hcd_urb`, `usb_hcd_link_urb_to_ep`, creates/looks up QH |
| 5 | process | `dwc2_hcd_urb_enqueue` | `dwc2/hcd.c:1844` | `-ENODEV` if port disconnected; `dwc2_hcd_qtd_init` (`control_phase = SETUP`, `data_toggle = DATA1`); `dwc2_hcd_qtd_add`; select + queue transactions |
| 6 | process | `dwc2_assign_and_init_hc` → `dwc2_hc_init_xfer` → `dwc2_hc_init` | `dwc2/hcd.c:2544`, `2319`, `651` | Channel programmed for the **SETUP** phase |
| 7 | process | `dwc2_hc_start_transfer` | `dwc2/hcd.c:1192` | `HCTSIZ.PID = SETUP`, `HCDMA = urb->setup_dma`, `HCCHAR.CHENA = 1` |
| 8 | hardware | — | — | SETUP token + DATA0(8 bytes), waits for ACK; halts channel |
| 9 | hardirq | `dwc2_handle_hcd_intr` → `dwc2_hc_n_intr` → `dwc2_hc_xfercomp_intr` (via `dwc2_hc_chhltd_intr_dma` in DMA mode) | `dwc2/hcd_intr.c:2204`, `2016`, `960` | `control_phase` → `DATA` (or `STATUS` if `wLength == 0`); channel released |
| 10 | hardirq | `dwc2_release_channel` → `dwc2_hcd_select_transactions` → … → `dwc2_hc_start_transfer` | `dwc2/hcd_intr.c:676` | **Same or different channel** re-programmed for the DATA phase |
| 11 | hardware | — | — | DATA phase packets; within the run the core toggles DATA1/DATA0 and retries NAKed INs by itself |
| 12 | hardirq | `dwc2_hc_xfercomp_intr` | `dwc2/hcd_intr.c:1010-1019` | `dwc2_update_urb_state`; if done → `STATUS`, else save toggle and re-queue |
| 13 | hardirq | STATUS phase programmed and run (opposite direction, DATA1, zero length) | `dwc2/hcd.c:2347-2367` | |
| 14 | hardirq | `dwc2_hc_xfercomp_intr` (`STATUS`) → `dwc2_host_complete(qtd, 0)` | `dwc2/hcd_intr.c:1020-1025`, `dwc2/hcd.c:4115` | `urb->actual_length` copied from the dwc2 URB; `usb_hcd_giveback_urb` |
| 15 | BH workqueue | `__usb_hcd_giveback_urb` | `core/hcd.c:1630` | **usbmon `C` event** (`hcd.c:1643`), unmap DMA, `urb->complete()` |
| 16 | process | `usbfs_start_wait_urb` returns | `core/devio.c` | data copied to userspace, ioctl returns `actual_length` or error |

**[C]** Steps 6–14 are all inside dwc2. usbmon sees only steps 3 and 15. The
phase boundaries (9, 12) and the channel number(s) used are not exported by
any interface on a stock kernel. They are reachable only through ftrace or
kprobes; see [capability-matrix.md](capability-matrix.md).

---

## 2. Phase state machine

`struct dwc2_qtd::control_phase` (`enum dwc2_control_phase`, `hcd.h:177-181`).

```
                 ┌───────────── wLength == 0 ────────────┐
                 │                                       ▼
  qtd_init ──▶ SETUP ──XferCompl──▶ DATA ──xfer_done──▶ STATUS ──XferCompl──▶ giveback(status)
                 │                   │ ▲                   │
                 │                   └─┘ !xfer_done:       │
                 │                   save toggle, re-queue │
                 └──── STALL (any phase) → -EPIPE ─────────┴──▶ giveback(-EPIPE)
                       XACTERR ×3   → -EPROTO
                       babble       → -EOVERFLOW
                       AHB error    → -EIO
```

Transitions happen only in `dwc2_hc_xfercomp_intr` (`hcd_intr.c:1003-1023`)
for buffer/slave DMA, and in `dwc2_complete_non_isoc_xfer_ddma`
(`hcd_ddma.c:1167-1180`) for descriptor DMA (not used for host on BCM2835,
see H1).

### Per-phase channel programming (`dwc2_hc_init_xfer`, `hcd.c:2319-2370`)

| Phase | Direction (`HCCHAR.EPDir`) | Initial PID (`HCTSIZ.PID`) | Length | Buffer | Notes |
|---|---|---|---|---|---|
| SETUP | OUT | `SETUP` (`DWC2_HC_PID_SETUP`) | 8 | `urb->setup_dma` | `do_ping = 0`. rpi also forces `max_packet = 8` (`rpi:hcd.c:~2354`) |
| DATA | from `bmRequestType` | `qtd->data_toggle` (DATA1 at first run) | `urb->length - actual_length` | `urb->dma + actual_length` | May need several channel runs |
| STATUS | IN if `wLength == 0` or data was OUT; else OUT | `DATA1` | 0 | `hsotg->status_buf{,_dma}` | `do_ping = 0` for IN |

**[C]** The host-side PID for each phase is chosen by software; the core does
not know it is executing a "control transfer". It executes three independent
channel runs whose `HCCHAR.EPType = control`.

**[C]** Circle's bare-metal driver independently uses the same structure:
three `TransferStage()` calls with an explicit PID per stage
(`circle:lib/usb/dwhcidevice.cpp:308-345`, `dwhcixferstagedata.cpp:402-427`).

### When does the DATA phase take more than one channel run?

**[C]** `dwc2_update_urb_state` (`hcd_intr.c:460-508`) returns "done" when
`short_read` or `actual_length >= length`. Otherwise the phase is re-queued
with the toggle saved from `HCTSIZ.PID` (`dwc2_hcd_save_data_toggle`,
`hcd_intr.c:510-539`). This happens when:

- the request exceeds `max_transfer_size` (BCM2835 param: 65535,
  `params.c:23`) or `max_packet_count` (511, `params.c:24`);
- an OUT data phase is NAKed (channel halts so software can rewind the
  buffer; `hcd_intr.c:1236-1256`), unless the core has the ≥ 2.71a OUT-NAK
  enhancement for HS (`hcd_intr.c:1812-1820`);
- a transaction error occurs before the third strike.

---

## 3. Completion statuses produced by dwc2 for control URBs

| Condition | Handler | URB status | Evidence |
|---|---|---|---|
| STATUS phase complete | `dwc2_hc_xfercomp_intr` | `0` (or `-EREMOTEIO` if `URB_SHORT_NOT_OK` and short) | `hcd_intr.c:1020-1025`, `hcd.c:4166-4170` |
| STALL in any phase | `dwc2_hc_stall_intr` | `-EPIPE` | `hcd_intr.c:1093-1135` |
| Three consecutive transaction errors | `dwc2_release_channel` | `-EPROTO` | `hcd_intr.c:696-702` |
| Babble | `dwc2_hc_babble_intr` | `-EOVERFLOW` | `hcd_intr.c:~1504` |
| AHB (DMA bus) error | `dwc2_hc_ahberr_intr` | `-EIO` | `hcd_intr.c:~1609` |
| Dequeued (unlink/kill) | `_dwc2_hcd_urb_dequeue` | status passed by usbcore (`-ENOENT` kill, `-ECONNRESET` async unlink) | `hcd.c:4801-4843` |
| Submit while port disconnected | `dwc2_hcd_urb_enqueue` | submit fails `-ENODEV` | `hcd.c:1852-1856` |

**[C]** A device that NAKs a control IN forever is retried by the core
indefinitely in buffer-DMA mode (`hcd_intr.c:1225-1234`). dwc2 has **no
timeout of its own**; the only bound is the caller's timeout (usbfs `timeout`
field, `usb_control_msg` `timeout`).

**[C]** On the target kernel `CONFIG_HZ=100`
([evidence](evidence/rpi-6.18.59-config-excerpt.txt)), so usbfs and
`usb_control_msg` timeouts have 10 ms granularity (`msecs_to_jiffies`).

---

## 4. Timeouts

`usbfs_start_wait_urb` (`core/devio.c:1139-1165`):
`wait_for_completion_killable_timeout` → on expiry `usb_kill_urb` → returns
`-ETIMEDOUT` unless the URB completed concurrently. The URB is then
cancelled exactly as in §5. In-kernel callers use `usb_start_wait_urb`
(`core/message.c:52-85`), which behaves the same and logs
"timed out or killed".

---

## 5. Cancellation (unlink / kill)

### Path **[C]**

1. `usb_kill_urb` (`core/urb.c:711-728`) sets `urb->reject`, calls
   `usb_hcd_unlink_urb(urb, -ENOENT)`, then **sleeps until `use_count == 0`**.
   `usb_unlink_urb` is the asynchronous variant (`-ECONNRESET`).
   usbfs `USBDEVFS_DISCARDURB` uses `usb_kill_urb` (`core/devio.c:2007-2028`).
2. `_dwc2_hcd_urb_dequeue` (`dwc2/hcd.c:4801`) under `hsotg->lock`:
   `usb_hcd_check_unlink_urb` → `dwc2_hcd_urb_dequeue` (`hcd.c:1904`).
3. If the QTD is `in_process` **and** a channel is assigned and the port is
   connected: `dwc2_hc_halt(chan, DWC2_HC_XFER_URB_DEQUEUE)` (`hcd.c:766`),
   which masks every channel interrupt except `CHHLTD`, clears pending ones,
   and writes `HCCHAR.CHDIS|CHENA` unless the channel is already disabled.
4. The QTD is unlinked and freed **immediately**; the QH is deactivated.
5. `usb_hcd_unlink_urb_from_ep` + `usb_hcd_giveback_urb(urb, status)`
   **before** the hardware has necessarily halted.
6. Later, the `CHHLTD` interrupt arrives; `dwc2_hc_n_intr` sees
   `halt_status == URB_DEQUEUE` and only releases the channel
   (`hcd_intr.c:2058-2072`).

### Consequences for an experimenter

- **[C]** The cancellation unit visible above dwc2 is the whole URB. Nothing
  reports which phase (SETUP / DATA / STATUS) was in progress, whether the
  SETUP packet reached the device, or how many DATA bytes moved.
- **[C]** dwc2 does not update `urb->actual_length` on dequeue (it is only
  set in `dwc2_host_complete`, `hcd.c:4137`), so a cancelled URB reports the
  value usbcore initialised at submit — expected `0` — even if DATA
  packets were transferred. **[H3]** verify on hardware.
- **[H4]** Because giveback (step 5) can precede the hardware halt (step 6),
  an IN channel may still DMA into the URB buffer after giveback. Whether this
  can happen on BCM2835, and for how long, is an open question. Experiment:
  usbfs with `USBDEVFS_CAP_MMAP` buffers (the user mapping is the DMA
  buffer), canary-fill, cancel during an IN data phase, inspect the buffer
  after reap. (M3, benign device.)
- **[C]** When the cancellation lands relative to phase boundaries is
  timing-dependent and not controllable from userspace: phases follow each
  other within one hardirq (`dwc2_release_channel` re-queues immediately,
  `hcd_intr.c:770-772`).
- **[C, observed on a non-dwc2 kernel]** A successful `USBDEVFS_DISCARDURB`
  does **not** mean the transfer was cancelled. usbfs accepts the discard
  while the URB is on its pending list, which lasts until the completion
  callback runs (in BH context for `HCD_BH` controllers). A kill that lands
  after the HCD has completed the URB cannot unlink it, and the URB is reaped
  with its real status. Observed: 111 of 400 zero-delay discards against an
  xHCI virtual root hub (Linux 6.8.0-117-generic aarch64, Docker Desktop VM)
  were accepted yet completed with status 0; none ended cancelled
  ([evidence](evidence/linux-vm-roothub-cancel-sweep.jsonl)). The
  experimental outcome is therefore always the *reaped* status. hostlab
  records `cancel` (ioctl result) and `status` (reaped) separately. The
  dwc2-on-BCM2835 rates are still to be measured (M3).

---

## 6. Bus reset and enumeration

### Root-port reset **[C]**

`hub_port_reset` (usbcore, traceable on target) → root hub
`SetPortFeature(PORT_RESET)` → `dwc2_hcd_hub_control` (`hcd.c:3651-3706`):
exits hibernation / partial power-down / clock gating if needed, sets
`HPRT0.PWR|RST`, `msleep(50)`, clears `RST`. The comment says "10ms (FS/LS)
or 50ms (HS)" but the code always waits 50 ms. Port-enable and speed are then
reported by the port interrupt (`dwc2_port_intr` → `dwc2_hprt0_enable`,
`hcd_intr.c:233-444`), which may adjust the PHY clock for LS/FS and schedule
`reset_work`.

### Device reset from userspace **[C]**

`USBDEVFS_RESET` → `proc_resetdevice` (`core/devio.c:1502`) →
`usb_reset_device` → `usb_reset_and_verify_device` → `hub_port_init`
(port reset, then the standard enumeration requests on ep0 at address 0,
`SET_ADDRESS`, descriptor re-read and comparison). Drivers bound to other
interfaces get `pre_reset`/`post_reset` or are unbound/rebound. All
enumeration control transfers go through `usb_hcd_submit_urb` and therefore
appear in usbmon.

**[H5]** The exact enumeration request sequence (initial descriptor
length, number of retries) depends on usbcore module parameters
(`old_scheme_first`, `use_both_schemes`) and device speed; record it from
usbmon on hardware rather than inferring it.

---

## 7. What userspace can and cannot express (usbfs)

| Operation | usbfs | Granularity |
|---|---|---|
| Complete control transfer | `USBDEVFS_CONTROL` (sync, timeout in ms) or `USBDEVFS_SUBMITURB` with `USBDEVFS_URB_TYPE_CONTROL` (async) | whole transfer |
| Cancel | `USBDEVFS_DISCARDURB` (→ `usb_kill_urb`) | whole URB |
| Timeout | `usbdevfs_ctrltransfer.timeout` | whole URB, 10 ms jiffies |
| Port/device reset | `USBDEVFS_RESET` | device (port reset + re-enumeration) |
| Endpoint halt clear | `USBDEVFS_CLEAR_HALT` | endpoint |
| Toggle reset | `USBDEVFS_RESETEP` | endpoint |
| Speed / port path | `USBDEVFS_GET_SPEED`, `USBDEVFS_CONNINFO_EX` | — |
| Individual phase (SETUP only, no STATUS, …) | **not expressible** | — |
| Arbitrary PID / token / malformed packet | **not expressible**, and not expressible to dwc2 hardware either (core generates tokens and CRC) | — |
