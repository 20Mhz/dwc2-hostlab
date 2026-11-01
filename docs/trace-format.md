# Trace format (schema 1)

All recorders write JSON Lines: one flat object per line, with `src` and
`ev` on every record. A `meta` record comes first. Clock snapshots are
written at the start and end.

## Session directory

| File | Writer | Content |
|---|---|---|
| `probe.json` | hostlab-probe | capabilities at capture time |
| `usbmon.jsonl` | hostlab-mon | usbmon events |
| `user.jsonl` | the command (normally hostlab-ctl) | userspace events |
| `user.stderr` | the command | diagnostics |
| `ftrace.txt` | hostlab-session | raw tracefs `trace` text |
| `session.json` | hostlab-session | kprobes registered / missing / failed, teardown result, warnings, exit code |
| `analysis.json` | hostlab-analyze `--json` | joined report |

## Time bases

| Source | Field | Clock | Unit |
|---|---|---|---|
| user | `t_*_ns` | CLOCK_MONOTONIC (mock backend: virtual, `timebase: virtual_ns`) | ns |
| usbmon | `real_us` | CLOCK_REALTIME (`mon_bin.c:505`) | µs |
| ftrace | line timestamp | `trace_clock=mono` | µs (text output) |
| clock snapshots | `mono_ns`, `real_ns`, `boot_ns`, `uncert_ns` | REALTIME read between two MONOTONIC reads; `uncert_ns` is the bracket width | ns |

## Records

### Common: `{"src": S, "ev": "meta"}`
`tool`, `version`, `schema`, plus per tool: `backend`, `api`, `command`,
`timebase` (hostlab-ctl); `device`, `snaplen`, `timebase` (hostlab-mon).

### Common: `{"ev": "clock"}`
`mono_ns`, `real_ns`, `boot_ns` (0 where unavailable), `uncert_ns`.

### `src: usbmon`
- `ev: urb` — `type` (`S` submit, `C` complete, `E` submit error), `urb`
  (kernel URB address, hex string, same spelling as a kprobe `x32` value),
  `real_us`, `xfer` (`ctrl|bulk|intr|iso`), `bus`, `dev`, `ep`, `in`,
  `status` (negative errno; `-115` = `-EINPROGRESS` on `S`), `len_urb`,
  `len_cap`, `setup` (hex, control `S` only), `data` (hex, up to snaplen),
  `interval` (intr/iso), `xfer_flags`.
- `ev: stats` — `recorded`, `queued`, `dropped` (usbmon `MON_IOCG_STATS`).
  `dropped > 0` means the capture is incomplete.

### `src: user` (hostlab-ctl)
- `ev: device` — `path`, `bus`, `dev`, `vid`, `pid`, `class`, `speed`,
  `ports`; with `info`: `caps` and decoded `cap_*` booleans.
- `ev: xfer, op: control` — `seq`, `setup`, `t_submit_ns`, `t_done_ns`,
  `timeout_ms`, `status` (hl_status name), `errno`, `actual`, `data` (IN).
- `ev: xfer, op: async` — `seq`, `delay_us`, `setup`, `t_submit_ns`,
  `t_cancel_ns` (null if the discard was not issued or not accepted),
  `t_reap_ns`, `cancel` (`ok` = DISCARDURB accepted, `not_pending` = already
  given back, `error`), `status` (**the outcome**, from reap), `urb_status`,
  `actual`.
- `ev: summary` — counts per (`cancel`, `status`).
- `ev: reset` — `t_start_ns`, `t_done_ns`, `status`, `errno`.
- `ev: error` — `op`, `status`, `errno`, `msg`.

`status` values: `ok invalid no_device access busy timeout stall protocol
overflow short cancelled not_pending io nomem unsupported again other`.

## ftrace events

Registered in tracefs group `hostlab`; event names carry an `hl_` prefix
because trace text omits the group. Definitions and argument sources are in
[python/hostlab/tracefs.py](../python/hostlab/tracefs.py).

| Event | Function | Fields |
|---|---|---|
| `hl_submit` | `usb_hcd_submit_urb` | `urb` |
| `hl_kill` | `usb_kill_urb` | `urb` |
| `hl_unlink` | `usb_hcd_unlink_urb` | `urb`, `status` |
| `hl_giveback` | `usb_hcd_giveback_urb` | `urb`, `status` |
| `hl_giveback_bh` | `__usb_hcd_giveback_urb` | `urb` |
| `hl_port_reset` | `hub_port_reset` | `port` |
| `hl_urb_enqueue` | `_dwc2_hcd_urb_enqueue` | `urb` |
| `hl_urb_dequeue` | `_dwc2_hcd_urb_dequeue` | `urb`, `status` |
| `hl_hc_start` | `dwc2_hc_start_transfer` | `hc` |
| `hl_hc_halt` | `dwc2_hc_halt` | `hc`, `reason` (enum dwc2_halt_status) |
| `hl_hc_release` | `dwc2_release_channel` | `hc`, `reason` |
| `hl_hc_irq` | `dwc2_hc_n_intr` | `hc` |
| `hl_xfercomp` | `dwc2_hc_xfercomp_intr` | `hc` |
| `hl_host_complete` | `dwc2_host_complete` | `status` |
| `hl_stall` / `hl_nak` / `hl_xacterr` / `hl_babble` | `dwc2_hc_*_intr` | `hc` |

`reason`: 0 NO_HALT_STATUS, 1 COMPLETE, 2 URB_COMPLETE, 3 ACK, 4 NAK,
5 NYET, 6 STALL, 7 XACT_ERR, 8 FRAME_OVERRUN, 9 BABBLE_ERR,
10 DATA_TOGGLE_ERR, 11 AHB_ERR, 12 PERIODIC_INCOMPLETE, 13 URB_DEQUEUE
(`core.h:1281-1296`).

Markers: `tracing_mark_write: hostlab_start|hostlab_stop mono_ns=… real_ns=…`.

## Versioning

`schema` is bumped on any incompatible change to a field name or meaning.
Adding fields is compatible. The C transport API has its own version
(`HL_USB_API_MAJOR/MINOR`, reported as `api` in hostlab-ctl's meta record).
