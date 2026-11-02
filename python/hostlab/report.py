# SPDX-License-Identifier: MIT
"""hostlab-report: experiment reports built on hostlab-analyze.

    hostlab-report cancel SESSION_DIR [--json OUT] [--bin-us 20]
    hostlab-report reset  SESSION_DIR [--json OUT]

cancel (E3): joins every hostlab-ctl cancel-sweep attempt to its kernel URB
instance (by submit time) and classifies the outcome:

  cancelled_before_start   URB given back cancelled, no channel run started
  cancelled_in_SETUP/DATA/STATUS
                           cancelled while that phase's channel run was the
                           latest one started (needs hc_start probes)
  accepted_but_completed   DISCARDURB accepted, URB still completed normally
  too_late                 DISCARDURB refused (not pending), URB completed
  other:<cancel>/<status>  anything else

dwc2 starts the STATUS run only after the DATA run's transfer-complete
interrupt (hcd_intr.c dwc2_hc_xfercomp_intr), so a URB cancelled in STATUS
had its DATA stage reported complete by the controller. Its reported
actual_length is the kernel-side evidence for H3.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional

from .analyze import analyze, _load_jsonl
from .bench import percentile

JOIN_WINDOW_NS = 5_000_000  # user submit -> kernel submit/enqueue


def _device_ctrl(rep: Dict[str, Any]) -> List[Dict[str, Any]]:
    return sorted((u for u in rep["urbs"] if u.get("xfer") == "ctrl" and u.get("dev") != 1
                   and u.get("t_start_ns") is not None), key=lambda u: u["t_start_ns"])


def join_user(user: List[Dict[str, Any]], urbs: List[Dict[str, Any]]) -> List[Optional[Dict]]:
    """Pair each user transfer with the first unused URB that started after it."""
    out: List[Optional[Dict]] = []
    i = 0
    for r in user:
        t0 = r["t_submit_ns"]
        while i < len(urbs) and urbs[i]["t_start_ns"] < t0:
            i += 1
        if i < len(urbs) and urbs[i]["t_start_ns"] - t0 <= JOIN_WINDOW_NS:
            out.append(urbs[i])
            i += 1
        else:
            out.append(None)
    return out


def classify(r: Dict[str, Any], u: Optional[Dict[str, Any]]) -> str:
    st, c = r["status"], r["cancel"]
    if st == "cancelled":
        if u is None or "cancel" not in u:
            return "cancelled_unobserved"
        ph = u["cancel"]["phase_at_dequeue"]
        return "cancelled_before_start" if ph == "not_started" else f"cancelled_in_{ph}"
    if st == "ok":
        return "accepted_but_completed" if c == "ok" else "too_late" if c == "not_pending" \
            else f"other:{c}/{st}"
    return f"other:{c}/{st}"


def cancel_report(session_dir: str, bin_us: int = 20) -> Dict[str, Any]:
    rep = analyze(session_dir)
    user = [r for r in _load_jsonl(f"{session_dir}/user.jsonl")
            if r.get("ev") == "xfer" and r.get("op") == "async"]
    urbs = _device_ctrl(rep)
    joined = join_user(user, urbs)
    rows = []
    for r, u in zip(user, joined):
        cls = classify(r, u)
        row = {"seq": r["seq"], "delay_us": r["delay_us"], "cancel": r["cancel"],
               "status": r["status"], "urb_status": r["urb_status"], "actual": r["actual"],
               "class": cls, "joined": u is not None,
               "cancel_after_submit_ns": (r["t_cancel_ns"] - r["t_submit_ns"])
               if r.get("t_cancel_ns") else None}
        if u is not None:
            row.update({"urb": u["urb"], "instance": u["instance"],
                        "channel_runs": u["channel_runs"], "usbmon_status": u["status"]})
            if "cancel" in u:
                row.update({k: u["cancel"].get(k) for k in (
                    "phase_at_dequeue", "phase_label_certainty", "runs_started_before_dequeue",
                    "dequeue_status", "dequeue_to_halt_ns", "dequeue_to_release_ns",
                    "giveback_before_channel_release", "halted_hc")})
        rows.append(row)

    bins: Dict[int, Counter] = defaultdict(Counter)
    for row in rows:
        bins[row["delay_us"] // bin_us * bin_us][row["class"]] += 1
    cancelled = [r for r in rows if r["status"] == "cancelled"]
    in_status = [r for r in cancelled if r.get("phase_at_dequeue") == "STATUS"]
    in_data = [r for r in cancelled if r.get("phase_at_dequeue") == "DATA"]
    lat = lambda key: [r[key] for r in cancelled if r.get(key) is not None]
    h3 = {"cancelled_in_STATUS": len(in_status),
          "actual_nonzero_in_STATUS": sum(1 for r in in_status if r["actual"]),
          "cancelled_in_DATA": len(in_data),
          "actual_nonzero_in_DATA": sum(1 for r in in_data if r["actual"])}
    if not in_status:
        h3["status"], h3["reason"] = "INCONCLUSIVE", "no URB was cancelled during STATUS"
    elif h3["actual_nonzero_in_STATUS"] == 0:
        h3["status"] = "PASS"
        h3["reason"] = (f"all {len(in_status)} URBs cancelled during STATUS (DATA stage "
                        "reported complete by the controller) returned actual_length 0")
    else:
        h3["status"] = "FAIL"
        h3["reason"] = (f"{h3['actual_nonzero_in_STATUS']} of {len(in_status)} URBs cancelled "
                        "during STATUS reported a non-zero actual_length")
    return {
        "tool": "hostlab-report cancel", "session": session_dir,
        "profile": rep["consistency"].get("profile"),
        "attempts": len(rows), "joined": sum(1 for r in rows if r["joined"]),
        "classes": dict(Counter(r["class"] for r in rows)),
        "cancel_results": dict(Counter(r["cancel"] for r in rows)),
        "by_delay_us": {str(k): dict(v) for k, v in sorted(bins.items())},
        "bin_us": bin_us,
        "accepted_not_cancelled": sum(1 for r in rows if r["cancel"] == "ok"
                                      and r["status"] != "cancelled"),
        "dequeue_to_halt_ns": _stats(lat("dequeue_to_halt_ns")),
        "dequeue_to_release_ns": _stats(lat("dequeue_to_release_ns")),
        "giveback_before_channel_release": dict(Counter(
            str(r.get("giveback_before_channel_release")) for r in cancelled
            if r.get("channel_runs"))),
        "h3": h3,
        "rows": rows,
    }


def _stats(xs: List[int]) -> Optional[Dict[str, int]]:
    if not xs:
        return None
    return {"n": len(xs), "min": min(xs), "median": percentile(xs, 50),
            "p95": percentile(xs, 95), "max": max(xs)}


def format_cancel(r: Dict[str, Any]) -> str:
    classes = sorted({c for v in r["by_delay_us"].values() for c in v})
    L = [f"E3 cancellation: {r['attempts']} attempts, {r['joined']} joined to kernel URBs, "
         f"profile {r['profile']}",
         f"  classes: {r['classes']}",
         f"  DISCARDURB accepted but URB not cancelled: {r['accepted_not_cancelled']}",
         f"  dequeue->halt: {r['dequeue_to_halt_ns']}",
         f"  dequeue->release: {r['dequeue_to_release_ns']}",
         f"  giveback before channel release: {r['giveback_before_channel_release']}",
         f"  H3 {r['h3']['status']}: {r['h3'].get('reason')}", "",
         "  delay_us  " + "  ".join(f"{c[:22]:>22}" for c in classes)]
    for k, v in r["by_delay_us"].items():
        L.append(f"  {k:>8}  " + "  ".join(f"{v.get(c, 0):>22}" for c in classes))
    return "\n".join(L)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="hostlab-report")
    sub = ap.add_subparsers(dest="kind", required=True)
    c = sub.add_parser("cancel")
    c.add_argument("session_dir")
    c.add_argument("--json")
    c.add_argument("--bin-us", type=int, default=20)
    args = ap.parse_args(argv)
    rep = cancel_report(args.session_dir, args.bin_us)
    if args.json:
        with open(args.json, "w") as f:
            json.dump(rep, f, indent=2, sort_keys=True)
    print(format_cancel(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
