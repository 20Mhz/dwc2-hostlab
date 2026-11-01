# DWC2 host-mode architecture (Linux)

Evidence base: trees and commits in [references.md](references.md). Paths are
relative to the kernel tree root; line numbers refer to mainline `46fec1c`
unless prefixed `rpi:`.

Labels used throughout:

- **[C]** confirmed by reading source at the cited location
- **[H]** hypothesis, needs verification on hardware (each has an ID that
  the M3 experiment plan refers to)

---

## 1. Which driver owns the controller on a Pi Zero W

| Configuration | DT `compatible` on `usb@7e980000` | Driver | Source |
|---|---|---|---|
| Raspberry Pi OS default | `brcm,bcm2708-usb` | **dwc_otg** (downstream, `CONFIG_USB_DWCOTG=y`, built in) | `rpi:arch/arm/boot/dts/broadcom/bcm270x.dtsi:99-107`, `rpi:drivers/usb/host/dwc_otg/dwc_otg_driver.c:1009`, `rpi:arch/arm/configs/bcmrpi_defconfig:1175` |
| `dtoverlay=dwc2` in `config.txt` | `brcm,bcm2835-usb` | **dwc2** (mainline-derived, `CONFIG_USB_DWC2=m`) | `rpi:arch/arm/boot/dts/overlays/dwc2-overlay.dts`, `drivers/usb/dwc2/params.c:307`, `rpi:arch/arm/configs/bcmrpi_defconfig:1199` |
| Mainline kernel + upstream DT (`bcm2835-rpi-zero-w.dts`) | `brcm,bcm2835-usb` (`bcm283x.dtsi:491-501`) | dwc2 | `rpi:arch/arm/boot/dts/broadcom/bcm283x.dtsi:491` |

**[C]** Stock Raspberry Pi OS on a Zero W does **not** use the dwc2 driver.
This project targets dwc2 (`dtoverlay=dwc2,dr_mode=host`) because it is the
upstream driver, has debugfs instrumentation, and is the only place an
upstreamable change could land. dwc_otg is supported only at the
driver-independent layers (usbfs, usbmon); see §9.

The overlay's `dr_mode` defaults to `otg`; `dr_mode=host` is recommended for
this project so that role switching (`drivers/usb/dwc2/drd.c`) never moves
the core out of host mode during an experiment.

---

## 2. Layering

```
 userspace         libusb / our tools ──ioctl──▶ usbfs (drivers/usb/core/devio.c)
                                                       │ usb_submit_urb / usb_kill_urb
 usbcore           drivers/usb/core/{urb.c,hcd.c,message.c,hub.c}
                       │  usb_hcd_submit_urb()  ── usbmon 'S' hook (core/hcd.c:1527)
                       │  hc_driver ops (dwc2_hc_driver, dwc2/hcd.c:4993-5015)
 dwc2 HCD          _dwc2_hcd_urb_enqueue → dwc2_hcd_urb_enqueue → qtd/qh queues
                       │  dwc2_hcd_select_transactions / dwc2_hcd_queue_transactions
                       │  dwc2_hc_init / dwc2_hc_start_transfer  (register writes)
 hardware          DWC2 core: host channels 0..N-1, FIFOs, DMA engine, root port
                       │  IRQ  → dwc2_handle_hcd_intr (hcd_intr.c:2204)
 dwc2 HCD          dwc2_hc_n_intr → per-condition handlers → dwc2_release_channel
                       │  dwc2_host_complete → usb_hcd_giveback_urb
 usbcore           BH workqueue (HCD_BH) → __usb_hcd_giveback_urb
                       │  usbmon 'C' hook (core/hcd.c:1643) → urb->complete()
```

**[C]** `dwc2_hc_driver.flags = HCD_MEMORY | HCD_USB2 | HCD_BH`
(`hcd.c:4999`). With `HCD_BH`, `usb_hcd_giveback_urb()` defers the actual
giveback (and the usbmon completion event) to `system_bh_wq` /
`system_bh_highpri_wq` (`core/hcd.c:1731-1760`). The usbmon `C` timestamp is
therefore *later* than the point at which dwc2 decided the URB was complete,
by the BH scheduling latency. Measuring that gap requires ftrace (§8).

### hc_driver operations (`hcd.c:4993-5015`)

| Op | dwc2 function | Role |
|---|---|---|
| `urb_enqueue` | `_dwc2_hcd_urb_enqueue` (`hcd.c:4613`) | Allocate `dwc2_hcd_urb`, link URB to ep, call `dwc2_hcd_urb_enqueue` (`hcd.c:1844`) |
| `urb_dequeue` | `_dwc2_hcd_urb_dequeue` (`hcd.c:4801`) | Cancellation entry point (see [usb-control-transfers.md §5](usb-control-transfers.md#5-cancellation-unlink--kill)) |
| `endpoint_disable` | `_dwc2_hcd_endpoint_disable` → `dwc2_hcd_endpoint_disable` (`hcd.c:1962`) | Waits up to 250 × 20 ms for the QTD list to drain, then frees the QH |
| `endpoint_reset` | `dwc2_hcd_endpoint_reset` (`hcd.c:2021`) | Resets `qh->data_toggle` to DATA0 |
| `hub_control` | `dwc2_hcd_hub_control` (`hcd.c:3382`) | Virtual root hub; port reset at `hcd.c:3651-3706` |
| `irq` | `_dwc2_hcd_irq` (`hcd.c:4887`) → `dwc2_handle_hcd_intr` (`hcd_intr.c:2204`) | |
| `get_frame_number` | `_dwc2_hcd_get_frame_number` (`hcd.c:4526`) | RPi patches this, see §10 |

---

## 3. Data model

| Object | Defined | Lifetime | Key fields |
|---|---|---|---|
| `struct urb` (usbcore) | `include/linux/usb.h` | submit → giveback | `hcpriv` → `dwc2_hcd_urb` |
| `struct dwc2_hcd_urb` | `hcd.h` | enqueue → `dwc2_host_complete` / dequeue (`kfree`) | `priv` → `urb`, `qtd`, `pipe_info`, `actual_length`, `status` |
| `struct dwc2_qtd` | `hcd.h:402` | one per URB | `control_phase`, `data_toggle`, `error_count`, `num_naks`, `in_process`, `complete_split` |
| `struct dwc2_qh` | `hcd.h` | one per endpoint (`ep->hcpriv`) | `qtd_list`, `channel`, `data_toggle`, `ping_state`, `do_split`, `want_wait` |
| `struct dwc2_host_chan` | `hcd.h:89-138` | allocated once per hardware channel at init (`hcd.c:5222`) | `hc_num`, `qh`, `xfer_*`, `data_pid_start`, `halt_status`, `halt_pending`, `halt_on_queue`, `xfer_started`, `hcint` |

**[C]** One QTD per URB; the QTD carries the control-transfer phase state
(`dwc2_hcd_qtd_init`, `hcd_queue.c:1974-1995` sets `control_phase =
DWC2_CONTROL_SETUP` and `data_toggle = DATA1`).

---

## 4. Scheduling and host-channel allocation

**[C]** Channels are a pool: `hsotg->free_hc_list`. QHs move between
schedule lists:

- non-periodic (control, bulk): `non_periodic_sched_inactive` →
  `non_periodic_sched_active` (and `_waiting` for NAK back-off)
- periodic (interrupt, isochronous): `periodic_sched_inactive` → `_ready` →
  `_assigned` → `_queued`

Flow:

1. `dwc2_hcd_urb_enqueue` (`hcd.c:1844`) — rejects with `-ENODEV` if the
   port is not connected, inits the QTD, adds it to the QH
   (`dwc2_hcd_qtd_add`, `hcd_queue.c:2011`); if the QH became schedulable it
   immediately calls `dwc2_hcd_select_transactions` +
   `dwc2_hcd_queue_transactions` (`hcd.c:~1895`).
2. `dwc2_hcd_select_transactions` (`hcd.c:2673`) — walks periodic-ready
   first, then non-periodic-inactive. With `uframe_sched` (always `true` in
   `dwc2_set_default_params`, `params.c:529`) the limit is
   `available_host_channels`; one channel is always kept back for
   non-periodic traffic when assigning periodic QHs (`<= 1` test).
3. `dwc2_assign_and_init_hc` (`hcd.c:2544`) — pops the first free channel,
   sets `qh->channel`, `qtd->in_process = 1`, resets halt flags, seeds
   `data_pid_start` from `qh->data_toggle`, points `xfer_dma`/`xfer_buf` at
   `urb->dma + urb->actual_length` (so a re-queued transfer resumes where
   the previous channel run stopped), then `dwc2_hc_init_xfer` (`hcd.c:2319`)
   and `dwc2_hc_init` (`hcd.c:651`).
4. `dwc2_hcd_queue_transactions` (`hcd.c:3108`) →
   `dwc2_process_{non_,}periodic_channels` → `dwc2_queue_transaction`
   (`hcd.c:2788`) → `dwc2_hc_start_transfer` (`hcd.c:1192`) when
   `!chan->xfer_started`.

---

## 5. Host-channel state machine

The driver has no explicit `enum` for channel state; state is the
combination of list membership and flags. Reconstructed from source **[C]**:

```
            dwc2_assign_and_init_hc            dwc2_hc_start_transfer
  FREE ─────────────────────────────▶ ASSIGNED ───────────────────────▶ RUNNING
  (on free_hc_list)                  (qh->channel set,                (HCCHAR.CHENA=1,
   ▲                                  HCCHAR/HCSPLT/HCINTMSK           xfer_started=1)
   │                                  programmed, CHENA=0)               │      │
   │                                                                     │      │ dwc2_hc_halt()
   │                                     core halts on its own           │      │ (dequeue, error,
   │                                     (DMA: end of phase,             │      │  slave-mode IN)
   │                                      STALL, XACTERR, NAK on OUT…)   │      ▼
   │                                                                     │   HALTING
   │                                                                     │   (halt_pending=1, or
   │                                                                     │    halt_on_queue=1 when
   │                                                                     │    slave req-queue full)
   │                                                                     ▼      │
   │        dwc2_release_channel → dwc2_hc_cleanup              HALTED (HCINT.CHHLTD)
   └──────────────────────────────────────────────────────────── dwc2_hc_n_intr → handler
```

| Transition | Function | Registers written |
|---|---|---|
| FREE→ASSIGNED | `dwc2_assign_and_init_hc` → `dwc2_hc_init` (`hcd.c:651-736`) | `HCINT` (W1C all), `HCINTMSK`, `HAINTMSK`, `HCCHAR` (dev addr, ep, dir, LS, type, MPS), `HCSPLT` |
| ASSIGNED→RUNNING | `dwc2_hc_start_transfer` (`hcd.c:1192-1391`) | `HCTSIZ` (XferSize, PktCnt, **PID**, DoPing), `HCDMA` (DMA mode), `HCSPLT.SPLTENA`, `HCCHAR` (MultiCnt, odd frame, **CHENA**) |
| RUNNING→HALTING | `dwc2_hc_halt` (`hcd.c:766-919`) | `HCINTMSK`/`HCINT` narrowed to CHHLTD for dequeue/AHB error; `HCCHAR.CHDIS` (+ `CHENA` unless descriptor DMA) |
| HALTED→FREE | `dwc2_release_channel` (`hcd_intr.c:676-773`) → `dwc2_hc_cleanup` (`hcd.c:930-946`) | `HCINTMSK=0`, `HCINT` W1C, `HAINTMSK` bit cleared |

Halt reasons are recorded in `chan->halt_status` (`enum dwc2_halt_status`,
`core.h:1281-1296`): `COMPLETE`, `URB_COMPLETE`, `ACK`, `NAK`, `NYET`,
`STALL`, `XACT_ERR`, `FRAME_OVERRUN`, `BABBLE_ERR`, `DATA_TOGGLE_ERR`,
`AHB_ERR`, `PERIODIC_INCOMPLETE`, `URB_DEQUEUE`.

**[C]** Every phase of a control transfer, and every NAK/XACTERR restart on
OUT, ends with the channel being **released to the pool and re-assigned**
(`dwc2_complete_non_periodic_xfer`, `hcd_intr.c:835-883`: "Always halt and
release the host channel ... A channel will be reassigned to the transfer
when the non-periodic schedule is processed"). A three-phase control
transfer therefore consumes at least three FREE→…→FREE cycles, possibly on
different channel numbers.

**[C]** In buffer-DMA mode `dwc2_halt_channel` does not touch hardware; it
calls `dwc2_release_channel` directly, because "the core always halts the
channel automatically in DMA mode" (`hcd_intr.c:775-797`).

**[C]** Buffer-DMA cannot halt a non-split periodic channel
(`hcd.c:774-786`: "Channel can't be halted"); it relies on the core halting
at the end of the (micro)frame.

---

## 6. Interrupt path

`dwc2_handle_hcd_intr` (`hcd_intr.c:2204`) → for `GINTSTS.HCHINT`:
`dwc2_hc_intr` (`hcd_intr.c:2168`) reads `HAINT`, services channels in
split-order first, then by index → `dwc2_hc_n_intr` (`hcd_intr.c:2016`):

1. `hcint = HCINT & HCINTMSK`, write-1-to-clear immediately.
2. If `halt_status == URB_DEQUEUE`: release the channel and return
   (`WARN_ON(hcint != CHHLTD)`).
3. Dispatch in fixed priority: XFERCOMPL, CHHLTD, AHBERR, STALL, NAK, ACK,
   NYET, XACTERR, BBLERR, FRMOVRUN, DATATGLERR, re-checking after each that
   the QTD is still the head (`dwc2_check_qtd_still_ok`).

**[C]** In buffer-DMA mode, per-channel interrupts unmasked are only
`CHHLTD` and `AHBERR`, plus `ACK`/`NAK`/`DATATGLERR` while the channel is in
an error state (`dwc2_hc_enable_dma_ints`, `hcd.c:573-608`). The cause of a
halt is decoded from the *raw* `HCINT` latched in `chan->hcint`
by `dwc2_hc_chhltd_intr_dma` (`hcd_intr.c:1802-1978`).

---

## 7. Hardware vs software responsibilities

| Behaviour | Owner | Evidence |
|---|---|---|
| Token generation (SETUP/IN/OUT/PING), CRC, bit stuffing, SOF | **HW** | Software writes only `HCCHAR`/`HCTSIZ`/`HCSPLT`; no register carries token or CRC bytes |
| Packetisation of one channel run into `PktCnt` packets of ≤ MPS | **HW** | `HCTSIZ.PktCnt`/`XferSize`, `hcd.c:1265-1306` |
| DATA0/DATA1 toggling *within* a channel run | **HW** | Software only seeds `HCTSIZ.PID`; reads it back via `dwc2_hcd_save_data_toggle` (`hcd_intr.c:510-539`) |
| Toggle carried *across* channel runs / phases | **SW** | `qh->data_toggle`, `qtd->data_toggle` |
| Initial PID per control phase (SETUP / DATA1 / DATA1) | **SW** | `dwc2_hc_init_xfer`, `hcd.c:2330-2368` |
| Sequencing SETUP → DATA → STATUS | **SW** | `dwc2_hc_xfercomp_intr`, `hcd_intr.c:1003-1023` |
| Status-stage direction | **SW** | `hcd.c:2348-2357` |
| NAK retry, non-split IN, buffer DMA | **HW** | "The core will continue transferring data", `hcd_intr.c:1225-1234` |
| NAK on OUT / PING restart | **SW** halts and re-queues (unless core ≥ 2.71a OUT-NAK enhancement for HS) | `hcd_intr.c:1236-1256`, `1812-1820` |
| NAK back-off for splits | **SW** (`DWC2_NAKS_BEFORE_DELAY`, `qh->want_wait`) | `hcd_intr.c:1195-1222` |
| Transaction-error retry, 3-strike rule | **SW** (`qtd->error_count >= 3` → `-EPROTO`) | `hcd_intr.c:696-702` |
| HW-level immediate retry count | **HW**, seeded by `HCCHAR.MultiCnt/EC` (=1 for non-periodic split, 3 for periodic split) | `hcd.c:1243-1249` |
| Split transaction SSPLIT/CSPLIT sequencing | **SW** decides SSPLIT vs CSPLIT; **HW** emits split tokens | `dwc2_hc_init` `hcd.c:704-735` |
| URB timeout | **usbcore / caller** (no timer in dwc2 for control/bulk) | `core/message.c:52-85`, `core/devio.c:1139-1165` |
| Port reset timing | **SW**: `HPRT0.RST` set, `msleep(50)`, cleared | `hcd.c:3693-3705` |
| Port speed detection | **HW** (`HPRT0.SPD`), read by SW | `dwc2_hprt0_enable` `hcd_intr.c:233` |

**[H1]** On the BCM2835 the core is in buffer-DMA mode for host:
`p->host_dma = dma_capable` where `dma_capable = (GHWCFG2.arch !=
SLAVE_ONLY)` and `p->dma_desc_enable = false` (`params.c:510, 539-542`).
Expected `GHWCFG2.OtgArch = Internal DMA`. Verify via
`/sys/kernel/debug/usb/20980000.usb/params` (`host_dma : 1`,
`dma_desc_enable : 0`) and `hw_params`.

**[H2]** BCM2835 exposes 8 host channels. Not stated in any source file
inspected (it is read from `GHWCFG2.NumHstChnl`, `params.c:972`). Verify via
`hw_params`.

**[C]** There is **no runtime switch** for host DMA vs slave mode in dwc2:
`host_dma` is not read from DT or a module parameter (`params.c` has no
`device_property_read*` for it; `grep module_param drivers/usb/dwc2` is
empty). Comparing DMA vs slave mode requires a rebuilt module.

---

## 8. Built-in observability in dwc2

| Facility | Present? | Detail |
|---|---|---|
| Tracepoints | **No** | `grep TRACE_EVENT drivers/usb/dwc2` is empty; `core.h:20-38` defines `dwc2_sch_dbg` → `no_printk` as a compile-time "tracer" hook |
| `dev_dbg` | Yes, ~150 call sites | Compiled in only with `CONFIG_USB_DWC2_DEBUG` (`-DDEBUG`, `Makefile:2`) **or** controllable at runtime with `CONFIG_DYNAMIC_DEBUG` |
| `dev_vdbg` | Yes, most channel-level detail | Compiled out unless `CONFIG_USB_DWC2_VERBOSE` (`-DVERBOSE_DEBUG`); *not* reachable via dynamic debug otherwise |
| debugfs `usb/<dev>/` | Yes (`debugfs.c:776-808`) | `params`, `hw_params`, `dr_mode`, `regdump` (incl. `HCCHAR/HCSPLT/HCINT/HCINTMSK/HCTSIZ/HCDMA` for 16 channels, `debugfs.c:548+`), plus gadget-only `state`, `fifo`, `testmode`, `ep*` |
| usbcore tracepoints | Mainline only | `drivers/usb/core/trace.h` has only `usb_set_device_state`, `usb_alloc_dev`; **absent** in `rpi-6.18.y` |

**[C]** `regdump` reads live registers (non-destructive reads; `HCINT` is
write-1-to-clear). It is a racy snapshot, not a trace.

---

## 9. dwc_otg (the default driver) — what changes

**[C]** dwc_otg on the Pi uses a FIQ to service host-channel interrupts and
split transactions (`fiq_enable = 1`, `fiq_fsm_enable = true`,
`rpi:drivers/usb/host/dwc_otg/dwc_otg_driver.c:203-207`). FIQ-handled work
is invisible to ftrace/kprobes on the Linux IRQ path.

**[C]** dwc_otg exposes `regoffset`/`regvalue` sysfs attributes
(`rpi:dwc_otg_attr.c`) allowing root to read **and write** arbitrary
controller registers while the driver is active. This project must not use
the write path (it violates driver ownership).

usbfs and usbmon work identically above either driver.

---

## 10. RPi downstream delta (dwc2 only)

`diff -r mainline/drivers/usb/dwc2 rpi/drivers/usb/dwc2`: 9 files,
+89/−110. Host-relevant hunks **[C]**:

| Change | Location (rpi) | Effect on experiments |
|---|---|---|
| Interrupt-IN splits masqueraded as Control in `HCCHAR.EPType`, un-done in `dwc2_hc_n_intr` | `hcd.c:~679-691`, `hcd_intr.c:~2051-2053` | `HCCHAR` snapshots of INT split channels show type=control |
| SETUP phase forces `chan->max_packet = 8` | `hcd.c:~2354` | SETUP always a single 8-byte packet regardless of ep0 MPS |
| DMA bounce buffer also for IN transfers whose length is not a multiple of 4 ("core will only do DMA writes of full words") | `hcd.c:~2484-2531` | Bounce buffers appear for odd-length control IN; affects timing |
| Split IN `max_packet` clamped to remaining `xfer_len` | `hcd.c:~2656-2659` | |
| `get_frame_number` returns frames (÷8) on HS root port | `hcd.c:~4563-4573` | Frame numbers differ from mainline semantics |
| Toggle-save sanity checks become `WARN` | `hcd_intr.c:515-532` | |

The remainder is version skew (`kzalloc_obj` API, debugfs `devm` alloc,
comment fixes) and gadget-side changes.
