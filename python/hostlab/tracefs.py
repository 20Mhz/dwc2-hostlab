# SPDX-License-Identifier: MIT
"""tracefs access and the kprobe profile for the dwc2 / usbcore host path.

Every probe below sits at a function *entry* and fetches arguments from
registers, so it needs no struct offsets and no debug info (the Raspberry Pi
kernel has neither BTF nor DWARF). The register assignment follows the ARM
32-bit AAPCS: arguments 1-4 in r0-r3. Signatures were read from the source
trees listed in docs/references.md; the function list matches
docs/evidence/rpi-6.18.59-*-traceable-functions.txt.

The one memory fetch, ``+0(%r1):u8``, reads ``struct dwc2_host_chan::hc_num``,
which is the first member of the struct (drivers/usb/dwc2/hcd.h:89-90), so its
offset is 0 by definition.

Values that must hold if the profile is right are checked by the analyser
(channel numbers below host_channels, enqueue URB pointers equal to usbmon ids).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

GROUP = "hostlab"
# The trace text shows only the event name, not the group
# (Documentation/trace/kprobetrace.rst:286), so names carry their own prefix.
EVENT_PREFIX = "hl_"

# enum dwc2_halt_status, drivers/usb/dwc2/core.h:1281-1296
HALT_STATUS = [
    "NO_HALT_STATUS", "COMPLETE", "URB_COMPLETE", "ACK", "NAK", "NYET",
    "STALL", "XACT_ERR", "FRAME_OVERRUN", "BABBLE_ERR", "DATA_TOGGLE_ERR",
    "AHB_ERR", "PERIODIC_INCOMPLETE", "URB_DEQUEUE",
]


@dataclass(frozen=True)
class Probe:
    name: str      # event name inside GROUP
    symbol: str    # kernel function
    fetch: str     # kprobe fetch arguments
    layer: str     # "dwc2" or "usbcore"
    meaning: str

    @property
    def event(self) -> str:
        return EVENT_PREFIX + self.name

    def definition(self, group: str = GROUP) -> str:
        return f"p:{group}/{self.event} {self.symbol} {self.fetch}".rstrip()


ARM32_PROFILE: List[Probe] = [
    # usbcore: URB lifecycle around the HCD
    Probe("submit", "usb_hcd_submit_urb", "urb=%r0:x32", "usbcore",
          "usb_hcd_submit_urb(urb, mem_flags); usbmon 'S' is recorded inside"),
    Probe("kill", "usb_kill_urb", "urb=%r0:x32", "usbcore",
          "usb_kill_urb(urb): usbfs DISCARDURB and timeouts"),
    Probe("unlink", "usb_hcd_unlink_urb", "urb=%r0:x32 status=%r1:s32", "usbcore",
          "usb_hcd_unlink_urb(urb, status)"),
    Probe("giveback", "usb_hcd_giveback_urb", "urb=%r1:x32 status=%r2:s32", "usbcore",
          "usb_hcd_giveback_urb(hcd, urb, status): HCD hands the URB back (IRQ side)"),
    Probe("giveback_bh", "__usb_hcd_giveback_urb", "urb=%r0:x32", "usbcore",
          "__usb_hcd_giveback_urb(urb): BH side; usbmon 'C' is recorded inside"),
    Probe("port_reset", "hub_port_reset", "port=%r1:s32", "usbcore",
          "hub_port_reset(hub, port1, udev, delay, warm)"),
    # dwc2: HCD entry points
    Probe("urb_enqueue", "_dwc2_hcd_urb_enqueue", "urb=%r1:x32", "dwc2",
          "_dwc2_hcd_urb_enqueue(hcd, urb, mem_flags)"),
    Probe("urb_dequeue", "_dwc2_hcd_urb_dequeue", "urb=%r1:x32 status=%r2:s32", "dwc2",
          "_dwc2_hcd_urb_dequeue(hcd, urb, status)"),
    # dwc2: host-channel lifecycle
    Probe("hc_start", "dwc2_hc_start_transfer", "hc=+0(%r1):u8", "dwc2",
          "dwc2_hc_start_transfer(hsotg, chan): channel enabled for one phase/run"),
    Probe("hc_halt", "dwc2_hc_halt", "hc=+0(%r1):u8 reason=%r2:u32", "dwc2",
          "dwc2_hc_halt(hsotg, chan, halt_status): software-requested halt"),
    Probe("hc_release", "dwc2_release_channel", "hc=+0(%r1):u8 reason=%r3:u32", "dwc2",
          "dwc2_release_channel(hsotg, chan, qtd, halt_status): channel back to pool"),
    Probe("hc_irq", "dwc2_hc_n_intr", "hc=%r1:s32", "dwc2",
          "dwc2_hc_n_intr(hsotg, chnum): per-channel interrupt"),
    Probe("xfercomp", "dwc2_hc_xfercomp_intr", "hc=%r2:s32", "dwc2",
          "dwc2_hc_xfercomp_intr(hsotg, chan, chnum, qtd): phase transitions happen here"),
    Probe("host_complete", "dwc2_host_complete", "status=%r2:s32", "dwc2",
          "dwc2_host_complete(hsotg, qtd, status)"),
    Probe("stall", "dwc2_hc_stall_intr", "hc=%r2:s32", "dwc2", "STALL handler"),
    Probe("nak", "dwc2_hc_nak_intr", "hc=%r2:s32", "dwc2", "NAK handler"),
    Probe("xacterr", "dwc2_hc_xacterr_intr", "hc=%r2:s32", "dwc2", "transaction error handler"),
    Probe("babble", "dwc2_hc_babble_intr", "hc=%r2:s32", "dwc2", "babble handler"),
]


# Probes that fire only on cancellation, reset or errors. They cost nothing on
# the normal path, so every kprobe profile keeps them.
RARE = ["kill", "unlink", "urb_dequeue", "hc_halt", "port_reset",
        "stall", "nak", "xacterr", "babble"]

# Instrumentation profiles. "hits" is the number of probe hits for one
# successful 3-phase control transfer; on the Pi Zero W each kprobe hit costs
# about 5 us (H8, docs/m1-bringup.md), so the profiles trade detail for
# overhead. "functions" are traced with the function tracer instead of a
# kprobe: cheaper, but without arguments (no URB address, no channel number),
# so URB windows come from usbmon.
PROFILES: Dict[str, Dict[str, Any]] = {
    "full": {"kprobes": [p.name for p in ARM32_PROFILE], "functions": [], "hits": 17,
             "about": "every probe: URB lifecycle, channel lifecycle, IRQs, BH giveback"},
    "channel": {"kprobes": ["urb_enqueue", "giveback", "hc_start", "hc_release"] + RARE,
                "functions": [], "hits": 8,
                "about": "URB join, channel start/release with numbers and release reasons"},
    "phase": {"kprobes": ["urb_enqueue", "giveback", "hc_start"] + RARE,
              "functions": [], "hits": 5,
              "about": "URB join and one event per channel run (phase reconstruction)"},
    "minimal": {"kprobes": ["urb_enqueue", "giveback"] + RARE, "functions": [], "hits": 2,
                "about": "URB join only; no phases"},
    "state": {"kprobes": ["urb_enqueue", "giveback", "hc_start", "xfercomp", "hc_release"]
              + RARE, "functions": [], "hits": 11,
              "about": "channel/endpoint/PID/length at each start and control_phase at each "
                       "completion; needs --offsets from scripts/pi/dwc2-offsets.sh"},
    "idle": {"kprobes": RARE, "functions": [], "hits": 0,
             "about": "diagnostic: tracing on and rare-path probes registered, no hits on "
                      "a normal transfer; isolates the fixed cost of tracing"},
    "fast": {"kprobes": RARE, "functions": ["dwc2_hc_start_transfer", "dwc2_release_channel"],
             "hits": 6,
             "about": "default: function-tracer channel start and release (phases, channel "
                      "lifecycle timing); URB windows from usbmon; no URB address join and no "
                      "channel numbers. Needs one transfer in flight per window "
                      "(ambiguous events are counted)"},
    "fphase": {"kprobes": RARE, "functions": ["dwc2_hc_start_transfer"], "hits": 3,
               "about": "function-tracer channel starts; URB windows from usbmon; "
                        "no channel numbers"},
}

# Function-tracer events are mapped onto the kprobe event names (without args).
FUNCTION_EVENTS = {"dwc2_hc_start_transfer": "hc_start", "dwc2_release_channel": "hc_release",
                   "dwc2_hc_halt": "hc_halt", "dwc2_hc_xfercomp_intr": "xfercomp"}


def _member_fetch(reg: str, m: Dict[str, int], kind: str = "u32") -> str:
    """Kprobe fetch for a struct member described by dwc2-offsets.sh output.
    Bitfields use the "b<width>@<bit>/32" form on the 32-bit word holding
    them (little-endian ARM, GCC allocates from bit 0 up)."""
    if "data_bit_offset" in m:
        bit = m["data_bit_offset"]
        return f"+{bit // 32 * 4}({reg}):b{m['bit_size']}@{bit % 32}/32"
    return f"+{m['data_member_location']}({reg}):{kind}"


def state_probes(offsets: Dict[str, Any]) -> List[Probe]:
    """Probes that read dwc2's own state (profile "state"): channel setup at
    each start (device, endpoint, direction, starting PID, length) and the
    QTD's control_phase at each transfer-complete interrupt. Offsets must come
    from scripts/pi/dwc2-offsets.sh for the running kernel; the prototypes
    are the 6.12.47 ones (docs/references.md)."""
    ch, qtd = offsets["structs"]["dwc2_host_chan"], offsets["structs"]["dwc2_qtd"]
    r1 = lambda name, kind="u32": _member_fetch("%r1", ch[name], kind)
    start = (f"hc=+0(%r1):u8 dev={r1('dev_addr')} ep={r1('ep_num')} in={r1('ep_is_in')} "
             f"pid={r1('data_pid_start')} len={r1('xfer_len')}")
    comp = (f"hc=%r2:s32 phase={_member_fetch('%r3', qtd['control_phase'])} "
            f"hcint={_member_fetch('%r1', ch['hcint'], 'x32')}")
    return [
        Probe("urb_enqueue", "_dwc2_hcd_urb_enqueue", "urb=%r1:x32", "dwc2", "enqueue"),
        Probe("giveback", "usb_hcd_giveback_urb", "urb=%r1:x32 status=%r2:s32", "usbcore",
              "IRQ-side giveback"),
        Probe("hc_start", "dwc2_hc_start_transfer", start, "dwc2",
              "channel start with its programmed endpoint, direction, PID and length"),
        Probe("xfercomp", "dwc2_hc_xfercomp_intr", comp, "dwc2",
              "transfer complete: qtd->control_phase being completed, latched HCINT"),
        Probe("hc_release", "dwc2_release_channel", "hc=+0(%r1):u8 reason=%r3:u32", "dwc2",
              "release"),
    ] + [p for p in ARM32_PROFILE if p.name in RARE]


def profile_probes(name: str) -> List[Probe]:
    wanted = set(PROFILES[name]["kprobes"])
    return [p for p in ARM32_PROFILE if p.name in wanted]


def find_tracefs(root: str = "/") -> Optional[str]:
    for rel in ("sys/kernel/tracing", "sys/kernel/debug/tracing"):
        path = os.path.join(root, rel)
        if os.path.isfile(os.path.join(path, "trace")):
            return path
    return None


def read_filter_functions(tracefs: str) -> Optional[set]:
    """Names in available_filter_functions, without the " [module]" suffix."""
    path = os.path.join(tracefs, "available_filter_functions")
    try:
        with open(path) as f:
            return {line.split()[0] for line in f if line.strip()}
    except OSError:
        return None


def usable_probes(available: Optional[set], profile: Iterable[Probe] = ARM32_PROFILE):
    """Split the profile into (usable, missing) by function availability."""
    usable, missing = [], []
    for p in profile:
        (usable if available is not None and p.symbol in available else missing).append(p)
    return usable, missing


class TraceFS:
    """Small, explicit wrapper; every write goes through _write for testability."""

    def __init__(self, path: str):
        self.path = path

    def _p(self, name: str) -> str:
        return os.path.join(self.path, name)

    def read(self, name: str) -> str:
        with open(self._p(name)) as f:
            return f.read()

    def _write(self, name: str, value: str, append: bool = False) -> None:
        # Not open(..., "a"): Python seeks to the end in append mode and
        # tracefs's kprobe_events rejects lseek(SEEK_END) with EINVAL (seen on
        # 6.12.47). Never O_TRUNC kprobe_events either: that deletes every
        # kprobe on the system, not only hostlab's.
        fd = os.open(self._p(name), os.O_WRONLY | (os.O_APPEND if append else os.O_TRUNC))
        try:
            os.write(fd, (value + "\n").encode())
        finally:
            os.close(fd)

    def current_clock(self) -> Optional[str]:
        try:
            text = self.read("trace_clock")
        except OSError:
            return None
        for tok in text.split():
            if tok.startswith("[") and tok.endswith("]"):
                return tok[1:-1]
        return None

    def set_clock(self, clock: str) -> None:
        self._write("trace_clock", clock)

    def set_tracing_on(self, on: bool) -> None:
        self._write("tracing_on", "1" if on else "0")

    def set_buffer_kb(self, kb: int) -> None:
        self._write("buffer_size_kb", str(kb))

    def clear(self) -> None:
        self._write("trace", "")

    def current_tracer(self) -> Optional[str]:
        try:
            return self.read("current_tracer").strip() or None
        except OSError:
            return None

    def set_tracer(self, name: str) -> None:
        self._write("current_tracer", name)

    def ftrace_filter(self) -> List[str]:
        """Functions in set_ftrace_filter; [] means "all functions" (the default)."""
        lines = [l.strip() for l in self.read("set_ftrace_filter").splitlines()]
        return [l for l in lines if l and not l.startswith("#")]

    def set_ftrace_filter(self, functions: List[str]) -> None:
        # O_TRUNC replaces the filter; an empty write restores "all functions".
        self._write("set_ftrace_filter", "\n".join(functions))

    def add_kprobe(self, probe: Probe) -> None:
        self._write("kprobe_events", probe.definition(), append=True)

    def remove_kprobe(self, event: str) -> None:
        self._write("kprobe_events", f"-:{GROUP}/{event}", append=True)

    def set_group_enabled(self, on: bool) -> None:
        self._write(os.path.join("events", GROUP, "enable"), "1" if on else "0")

    def marker(self, text: str) -> None:
        self._write("trace_marker", text)

    def registered_hostlab_probes(self) -> List[str]:
        try:
            lines = self.read("kprobe_events").splitlines()
        except OSError:
            return []
        names = []
        for line in lines:
            parts = line.split()
            if parts and not parts[0].startswith("-") and ":" in parts[0] and "/" in parts[0]:
                group, name = parts[0].split(":", 1)[1].split("/", 1)
                if group == GROUP:
                    names.append(name)
        return names

    def teardown(self) -> Dict[str, List[str]]:
        """Remove only hostlab's own probes; never touch other users' events."""
        removed, errors = [], []
        try:
            if os.path.exists(self._p(os.path.join("events", GROUP, "enable"))):
                self.set_group_enabled(False)
        except OSError as e:
            errors.append(f"disable: {e}")
        for name in self.registered_hostlab_probes():
            try:
                self.remove_kprobe(name)
                removed.append(name)
            except OSError as e:
                errors.append(f"{name}: {e}")
        return {"removed": removed, "errors": errors}
