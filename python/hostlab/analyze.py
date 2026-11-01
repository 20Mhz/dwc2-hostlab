# SPDX-License-Identifier: MIT
"""hostlab-analyze: correlate usbmon, ftrace/kprobe and userspace records.

    python3 -m hostlab.analyze SESSION_DIR [--json OUT]

Inputs (any subset) in SESSION_DIR: usbmon.jsonl (hostlab-mon), ftrace.txt
(hostlab-session), user.jsonl (hostlab-ctl).

Time bases are unified on CLOCK_MONOTONIC nanoseconds:
  - ftrace runs with trace_clock=mono, so its timestamps are used as-is;
    trace_marker records written by hostlab-session check that assumption.
  - usbmon timestamps are CLOCK_REALTIME microseconds; they are mapped with
    the REALTIME-MONOTONIC offset from the clock snapshots in usbmon.jsonl.

URBs are joined by the kernel URB address, which is both the usbmon id and the
``urb=`` argument of the kprobes. Channel-level events carry no URB address;
they are attributed to a URB only when exactly one URB is between enqueue and
giveback at that moment, otherwise they are counted as ambiguous.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from statistics import median
from typing import Any, Dict, List, Optional, Tuple

from . import __version__
from .ftrace_parse import TraceEvent, parse
from .tracefs import EVENT_PREFIX, HALT_STATUS

CHANNEL_EVENTS = {"hc_start", "hc_halt", "hc_release", "hc_irq", "xfercomp",
                  "host_complete", "stall", "nak", "xacterr", "babble"}


def _load_jsonl(path: str) -> List[Dict[str, Any]]:
    out = []
    if not os.path.exists(path):
        return out
    with open(path) as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise ValueError(f"{path}:{n}: {e}") from None
    return out


def _urb_key(v: Any) -> Optional[int]:
    if v is None:
        return None
    if isinstance(v, int):
        return v
    return int(str(v), 16)


@dataclass
class ClockMap:
    """REALTIME -> MONOTONIC using snapshot pairs."""
    offset_ns: Optional[int] = None  # real - mono
    drift_ns: Optional[int] = None   # offset change between first and last snapshot
    max_uncert_ns: Optional[int] = None

    @classmethod
    def from_records(cls, recs: List[Dict[str, Any]]) -> "ClockMap":
        snaps = [r for r in recs if r.get("ev") == "clock"]
        if not snaps:
            return cls()
        offs = [r["real_ns"] - r["mono_ns"] for r in snaps]
        return cls(offset_ns=int(sum(offs) / len(offs)), drift_ns=offs[-1] - offs[0],
                   max_uncert_ns=max(r.get("uncert_ns", 0) for r in snaps))

    def real_us_to_mono_ns(self, real_us: int) -> Optional[int]:
        if self.offset_ns is None:
            return None
        return real_us * 1000 - self.offset_ns


@dataclass
class UrbTimeline:
    urb: int
    xfer: Optional[str] = None
    dev: Optional[int] = None
    ep: Optional[int] = None
    is_in: Optional[bool] = None
    setup: Optional[str] = None
    status: Optional[int] = None
    mon_submit_ns: Optional[int] = None
    mon_complete_ns: Optional[int] = None
    k: Dict[str, int] = field(default_factory=dict)         # first ts of each kprobe event
    dequeue_status: Optional[int] = None
    channel: List[TraceEvent] = field(default_factory=list)

    def window(self) -> Optional[Tuple[int, int]]:
        # dwc2 gives a URB back (dwc2_host_complete) before releasing its
        # channel (hcd_intr.c:1020-1027), and the release still happens in the
        # same hardirq, i.e. before the BH-side giveback. End the window there.
        start = self.k.get("urb_enqueue")
        end = self.k.get("giveback_bh", self.k.get("giveback"))
        if start is None or end is None:
            return None
        return start, end

    def wlength(self) -> Optional[int]:
        if not self.setup or len(self.setup) != 16:
            return None
        return int(self.setup[14:16] + self.setup[12:14], 16)


def _phase_labels(n_starts: int, wlength: Optional[int],
                  truncated: bool = False) -> Tuple[List[str], str]:
    """Label the channel runs of a control URB.

    Returns (labels, certainty). "exact": the run count equals the phase count
    (2 without a data stage, 3 with one; usb-control-transfers.md §2), so each
    run is one phase. "prefix": the URB was cancelled before finishing and had
    no more runs than phases; labels assume no retried run, which the trace
    cannot rule out. "unknown": anything else (retries, multi-run data stage).
    """
    if wlength is not None:
        phases = ["SETUP", "STATUS"] if wlength == 0 else ["SETUP", "DATA", "STATUS"]
        if n_starts == len(phases) and not truncated:
            return phases, "exact"
        if truncated and n_starts <= len(phases):
            return phases[:n_starts], "prefix"
    return [f"run{i}" for i in range(n_starts)], "unknown"


def analyze(session_dir: str) -> Dict[str, Any]:
    mon = _load_jsonl(os.path.join(session_dir, "usbmon.jsonl"))
    user = _load_jsonl(os.path.join(session_dir, "user.jsonl"))
    trace_path = os.path.join(session_dir, "ftrace.txt")
    events: List[TraceEvent] = []
    if os.path.exists(trace_path):
        with open(trace_path) as f:
            events = list(parse(f))

    warnings: List[str] = []
    cmap = ClockMap.from_records(mon)
    if mon and cmap.offset_ns is None:
        warnings.append("usbmon.jsonl has no clock snapshots; usbmon times not mapped")
    if cmap.drift_ns is not None and abs(cmap.drift_ns) > 1_000_000:
        warnings.append(f"REALTIME drifted {cmap.drift_ns} ns against MONOTONIC during capture")

    # ftrace clock check: markers carry the userspace CLOCK_MONOTONIC value.
    marker_offsets = []
    for e in events:
        if e.event == "tracing_mark_write" and "mono_ns" in e.fields:
            marker_offsets.append(e.ts_ns - int(e.fields["mono_ns"]))
    if marker_offsets and max(abs(o) for o in marker_offsets) > 1_000_000:
        warnings.append("ftrace timestamps differ from CLOCK_MONOTONIC by >1 ms; "
                        "was trace_clock set to mono?")

    urbs: Dict[int, UrbTimeline] = {}

    def get(urb: int) -> UrbTimeline:
        if urb not in urbs:
            urbs[urb] = UrbTimeline(urb=urb)
        return urbs[urb]

    for r in mon:
        if r.get("ev") != "urb":
            continue
        t = get(_urb_key(r["urb"]))
        t.xfer, t.dev, t.ep, t.is_in = r.get("xfer"), r.get("dev"), r.get("ep"), r.get("in")
        ts = cmap.real_us_to_mono_ns(r["real_us"])
        if r["type"] == "S":
            t.mon_submit_ns = ts
            t.setup = r.get("setup", t.setup)
        elif r["type"] in ("C", "E"):
            t.mon_complete_ns = ts
            t.status = r.get("status")

    hostlab_events = [e for e in events if e.event.startswith(EVENT_PREFIX)]
    for e in hostlab_events:
        e.event = e.event[len(EVENT_PREFIX):]

    channel_events: List[TraceEvent] = []
    channel_numbers = set()
    port_resets: List[int] = []
    for e in hostlab_events:
        urb = e.int_field("urb")
        if urb is not None:
            t = get(urb)
            t.k.setdefault(e.event, e.ts_ns)
            if e.event == "urb_dequeue":
                t.dequeue_status = e.int_field("status")
        elif e.event in CHANNEL_EVENTS:
            channel_events.append(e)
            hc = e.int_field("hc")
            if hc is not None:
                channel_numbers.add(hc)
        elif e.event == "port_reset":
            port_resets.append(e.ts_ns)

    # Attribute channel events to the single URB in flight, if unique.
    windows = [(t.window(), t) for t in urbs.values() if t.window()]
    ambiguous = unattributed = 0
    for e in channel_events:
        owners = [t for (w, t) in windows if w[0] <= e.ts_ns <= w[1]]
        if len(owners) == 1:
            owners[0].channel.append(e)
        elif owners:
            ambiguous += 1
        else:
            unattributed += 1

    report_urbs = []
    lat_mon, lat_sched, lat_bh = [], [], []
    for t in sorted(urbs.values(), key=lambda u: u.k.get("urb_enqueue")
                    or u.mon_submit_ns or 0):
        starts = [e for e in t.channel if e.event == "hc_start"]
        rec: Dict[str, Any] = {
            "urb": hex(t.urb), "xfer": t.xfer, "dev": t.dev, "ep": t.ep, "in": t.is_in,
            "setup": t.setup, "status": t.status,
            "channel_runs": len(starts),
            "channels_used": sorted({e.int_field("hc") for e in starts}),
        }
        if t.mon_submit_ns is not None and t.mon_complete_ns is not None:
            rec["usbmon_latency_ns"] = t.mon_complete_ns - t.mon_submit_ns
            lat_mon.append(rec["usbmon_latency_ns"])
        if "urb_enqueue" in t.k and starts:
            rec["enqueue_to_first_start_ns"] = starts[0].ts_ns - t.k["urb_enqueue"]
            lat_sched.append(rec["enqueue_to_first_start_ns"])
        if "giveback" in t.k and "giveback_bh" in t.k:
            rec["giveback_bh_delay_ns"] = t.k["giveback_bh"] - t.k["giveback"]
            lat_bh.append(rec["giveback_bh_delay_ns"])
        cancelled = "urb_dequeue" in t.k
        if t.xfer == "ctrl" and starts:
            labels, certainty = _phase_labels(len(starts), t.wlength(), truncated=cancelled)
            rec["phases"] = [{"label": lab, "hc": s.int_field("hc"), "t_ns": s.ts_ns}
                             for lab, s in zip(labels, starts)]
            rec["phase_label_certainty"] = certainty
        if cancelled:
            tdq = t.k["urb_dequeue"]
            before = [s for s in starts if s.ts_ns <= tdq]
            labels, certainty = _phase_labels(len(before), t.wlength(), truncated=True)
            cancel: Dict[str, Any] = {
                "dequeue_status": t.dequeue_status,
                "runs_started_before_dequeue": len(before),
                "phase_at_dequeue": labels[-1] if before else "not_started",
                "phase_label_certainty": certainty if before else "exact",
            }
            # The halted channel may be released after the URB was given back,
            # i.e. outside its window, so search all channel events.
            rel = [e for e in channel_events if e.event == "hc_release" and e.ts_ns >= tdq
                   and e.int_field("reason") == HALT_STATUS.index("URB_DEQUEUE")][:1]
            if rel:
                cancel["dequeue_to_release_ns"] = rel[0].ts_ns - tdq
                if "giveback_bh" in t.k:
                    cancel["giveback_before_channel_release"] = t.k["giveback_bh"] < rel[0].ts_ns
            rec["cancel"] = cancel
        report_urbs.append(rec)

    def stats(xs: List[int]) -> Optional[Dict[str, int]]:
        if not xs:
            return None
        return {"n": len(xs), "min": min(xs), "median": int(median(xs)), "max": max(xs)}

    user_xfers = [r for r in user if r.get("ev") == "xfer"]
    user_meta = next((r for r in user if r.get("ev") == "meta"), None)
    return {
        "tool": "hostlab-analyze",
        "version": __version__,
        "inputs": {"usbmon_records": len(mon), "trace_events": len(events),
                   "user_records": len(user)},
        "clock": {"usbmon_real_minus_mono_ns": cmap.offset_ns, "drift_ns": cmap.drift_ns,
                  "snapshot_uncert_ns": cmap.max_uncert_ns,
                  "ftrace_marker_offsets_ns": marker_offsets},
        "user_backend": user_meta.get("backend") if user_meta else None,
        "user_xfers": len(user_xfers),
        "urbs": report_urbs,
        "channel_events": {"total": len(channel_events), "ambiguous": ambiguous,
                           "unattributed": unattributed,
                           "channel_numbers_seen": sorted(channel_numbers)},
        "port_resets_ns": port_resets,
        "latency": {"usbmon_submit_to_complete": stats(lat_mon),
                    "enqueue_to_first_channel_start": stats(lat_sched),
                    "giveback_irq_to_bh": stats(lat_bh)},
        "warnings": warnings,
    }


def format_text(rep: Dict[str, Any]) -> str:
    lines = [f"hostlab-analyze {rep['version']}: {len(rep['urbs'])} URBs, "
             f"{rep['channel_events']['total']} channel events "
             f"({rep['channel_events']['ambiguous']} ambiguous)"]
    for r in rep["urbs"]:
        parts = [r["urb"], str(r["xfer"]), f"dev={r['dev']}", f"status={r['status']}"]
        if "phases" in r:
            parts.append("phases=" + ",".join(f"{p['label']}@hc{p['hc']}" for p in r["phases"]))
        if "cancel" in r:
            c = r["cancel"]
            parts.append(f"cancel(phase={c['phase_at_dequeue']}, "
                         f"release+{c.get('dequeue_to_release_ns')}ns)")
        lines.append("  " + " ".join(parts))
    for name, s in rep["latency"].items():
        if s:
            lines.append(f"  {name}: n={s['n']} min={s['min']} median={s['median']} "
                         f"max={s['max']} ns")
    for w in rep["warnings"]:
        lines.append(f"  WARNING: {w}")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="hostlab-analyze")
    ap.add_argument("session_dir")
    ap.add_argument("--json", help="write the full report to this file")
    args = ap.parse_args(argv)
    rep = analyze(args.session_dir)
    if args.json:
        with open(args.json, "w") as f:
            json.dump(rep, f, indent=2, sort_keys=True)
    print(format_text(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
