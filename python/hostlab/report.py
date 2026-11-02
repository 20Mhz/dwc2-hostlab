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
from .ftrace_parse import parse

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
    rep = analyze(session_dir, keep_events=True)
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
            tdq = u.get("k", {}).get("urb_dequeue")
            comps = [e for e in u.get("events", []) if e["ev"] == "xfercomp"
                     and "phase" in e["fields"]]
            if tdq is not None and comps:
                # Profiles reading qtd->control_phase ("state"): phases whose
                # transfer-complete interrupt was handled before the dequeue.
                row["completed_before_dequeue"] = [
                    PHASES[int(e["fields"]["phase"], 0)] for e in comps if e["t_ns"] < tdq]
            kt = u.get("k", {})
            gb = kt.get("giveback")
            if row["cancel"] == "ok" and row["status"] == "ok" and gb is not None:
                # Where the completion interrupt overtook the cancel path
                # (usb_kill_urb -> usb_hcd_unlink_urb -> dwc2 dequeue, which
                # only runs while urb->use_count > 0).
                row["lost_race"] = (
                    "before_unlink" if kt.get("unlink", gb + 1) > gb
                    else "in_unlink" if kt.get("urb_dequeue", gb + 1) > gb
                    else "in_dequeue") if "kill" in kt else None
            if "kill" in kt and "urb_dequeue" in kt and row["status"] == "cancelled":
                row["kill_to_dequeue_ns"] = kt["urb_dequeue"] - kt["kill"]
                if "unlink" in kt:
                    row["kill_to_unlink_ns"] = kt["unlink"] - kt["kill"]
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
    # Strongest evidence first: the controller's DATA transfer-complete was
    # handled before the dequeue; otherwise the STATUS run had started.
    data_done = [r for r in cancelled if "DATA" in (r.get("completed_before_dequeue") or [])]
    in_status = data_done or [r for r in cancelled if r.get("phase_at_dequeue") == "STATUS"]
    in_data = [r for r in cancelled if r.get("phase_at_dequeue") == "DATA"]
    lat = lambda key: [r[key] for r in cancelled if r.get(key) is not None]
    h3 = {"evidence": "DATA transfer-complete before dequeue" if data_done
          else "STATUS run started before dequeue",
          "cancelled_in_STATUS": len(in_status),
          "actual_nonzero_in_STATUS": sum(1 for r in in_status if r["actual"]),
          "cancelled_in_DATA": len(in_data),
          "actual_nonzero_in_DATA": sum(1 for r in in_data if r["actual"])}
    if not in_status:
        h3["status"], h3["reason"] = "INCONCLUSIVE", ("no cancelled URB had its DATA stage "
                                                      "completed before the dequeue")
    elif h3["actual_nonzero_in_STATUS"] == 0:
        h3["status"] = "PASS"
        h3["reason"] = (f"all {len(in_status)} cancelled URBs whose DATA stage the controller "
                        f"reported complete ({h3['evidence']}) returned actual_length 0")
    else:
        h3["status"] = "FAIL"
        h3["reason"] = (f"{h3['actual_nonzero_in_STATUS']} of {len(in_status)} cancelled URBs "
                        f"whose DATA stage completed ({h3['evidence']}) reported a non-zero "
                        "actual_length")
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
        "lost_race": dict(Counter(r["lost_race"] for r in rows if r.get("lost_race"))),
        "kill_to_unlink_ns": _stats(lat("kill_to_unlink_ns")),
        "kill_to_dequeue_ns": _stats(lat("kill_to_dequeue_ns")),
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
         f"  accepted-but-completed lost the race: {r['lost_race']}",
         f"  kill->unlink: {r['kill_to_unlink_ns']}",
         f"  kill->dequeue: {r['kill_to_dequeue_ns']}",
         f"  dequeue->halt: {r['dequeue_to_halt_ns']}",
         f"  dequeue->release: {r['dequeue_to_release_ns']}",
         f"  giveback before channel release: {r['giveback_before_channel_release']}",
         f"  H3 {r['h3']['status']}: {r['h3'].get('reason')}", "",
         "  delay_us  " + "  ".join(f"{c[:22]:>22}" for c in classes)]
    for k, v in r["by_delay_us"].items():
        L.append(f"  {k:>8}  " + "  ".join(f"{v.get(c, 0):>22}" for c in classes))
    return "\n".join(L)


REQUESTS = {0x00: "GET_STATUS", 0x01: "CLEAR_FEATURE", 0x03: "SET_FEATURE", 0x05: "SET_ADDRESS",
            0x06: "GET_DESCRIPTOR", 0x08: "GET_CONFIGURATION", 0x09: "SET_CONFIGURATION",
            0x0A: "GET_INTERFACE", 0x0B: "SET_INTERFACE"}
DESC_TYPES = {1: "DEVICE", 2: "CONFIG", 3: "STRING", 6: "QUALIFIER", 0x0F: "BOS"}


def decode_setup(setup: str) -> str:
    """Readable name of a standard request; class/vendor requests as raw bytes."""
    b = bytes.fromhex(setup)
    rt, req = b[0], b[1]
    wv, wi, wl = b[2] | b[3] << 8, b[4] | b[5] << 8, b[6] | b[7] << 8
    if rt & 0x60:
        kind = "class" if rt & 0x60 == 0x20 else "vendor"
        return f"{kind}({setup})"
    name = REQUESTS.get(req, f"req{req:#04x}")
    if req == 0x06:
        return f"GET_DESCRIPTOR({DESC_TYPES.get(wv >> 8, wv >> 8)},{wl})"
    if req == 0x05:
        return f"SET_ADDRESS({wv})"
    if req == 0x09:
        return f"SET_CONFIGURATION({wv})"
    return f"{name}(wValue={wv},wIndex={wi},wLength={wl})"


def channel_accounting(trace_path: str) -> Optional[Dict[str, Any]]:
    """Per channel: starts, releases, out-of-order pairs, still open at the end.
    None when the trace has no numbered channel events (profiles without
    hc_start/hc_release kprobes)."""
    try:
        with open(trace_path) as f:
            events = [e for e in parse(f) if e.event in ("hl_hc_start", "hl_hc_release")]
    except OSError:
        return None
    if not any(e.event == "hl_hc_release" for e in events):
        return None
    open_: Dict[int, bool] = {}
    starts, releases, disorder = Counter(), Counter(), 0
    for e in events:
        hc = e.int_field("hc")
        if e.event == "hl_hc_start":
            starts[hc] += 1
            disorder += open_.get(hc, False)
            open_[hc] = True
        else:
            releases[hc] += 1
            disorder += not open_.get(hc, False)
            open_[hc] = False
    return {"starts": sum(starts.values()), "releases": sum(releases.values()),
            "out_of_order": disorder, "open_at_end": sorted(h for h, o in open_.items() if o)}


def reset_report(session_dir: str) -> Dict[str, Any]:
    rep = analyze(session_dir)
    user = _load_jsonl(f"{session_dir}/user.jsonl")
    resets = [r for r in user if r.get("ev") == "reset"]
    pend = [r for r in user if r.get("op") == "pending_across_reset"]
    mon = _load_jsonl(f"{session_dir}/usbmon.jsonl")
    from .analyze import ClockMap
    cmap = ClockMap.from_records(mon)
    submits = []
    for m in mon:
        if m.get("ev") == "urb" and m.get("type") == "S" and m.get("xfer") == "ctrl" \
                and m.get("setup"):
            t = cmap.real_us_to_mono_ns(m["real_us"])
            if t is not None:
                submits.append((t, m["dev"], m["setup"]))
    port_resets = rep["port_resets_ns"]
    rows = []
    for r in resets:
        t0, t1 = r["t_start_ns"], r["t_done_ns"]
        seq = [f"dev{d}:{decode_setup(s)}" for t, d, s in submits if t0 <= t <= t1]
        pr = [t - t0 for t in port_resets if t0 <= t <= t1]
        p = [x for x in pend if x["seq"] == r["seq"]]
        rows.append({"seq": r["seq"], "status": r["status"], "errno": r["errno"],
                     "duration_ns": t1 - t0, "pending": r.get("pending", 0),
                     "port_resets_after_ns": pr, "requests": seq,
                     "pending_outcomes": dict(Counter(x["status"] for x in p)),
                     "pending_urb_status": dict(Counter(str(x["urb_status"]) for x in p)),
                     "pending_actual": dict(Counter(str(x["actual"]) for x in p))})
    # Channel accounting over the whole capture: on each channel, starts and
    # releases must alternate, and none may be left started at the end.
    accounting = channel_accounting(f"{session_dir}/ftrace.txt")
    sequences = Counter(" > ".join(r["requests"]) for r in rows)
    return {"tool": "hostlab-report reset", "session": session_dir,
            "profile": rep["consistency"].get("profile"),
            "resets": len(rows), "reset_status": dict(Counter(r["status"] for r in rows)),
            "duration_ns": _stats([r["duration_ns"] for r in rows]),
            "request_sequences": dict(sequences),
            "pending_outcomes": dict(sum((Counter(r["pending_outcomes"]) for r in rows),
                                         Counter())),
            "channel_events": rep["channel_events"], "channel_accounting": accounting,
            "warnings": rep["warnings"], "rows": rows}


def format_reset(r: Dict[str, Any]) -> str:
    L = [f"E6 reset: {r['resets']} resets {r['reset_status']}, profile {r['profile']}",
         f"  USBDEVFS_RESET duration: {r['duration_ns']}",
         f"  channel accounting: {r['channel_accounting']}",
         f"  pending URB outcomes: {r['pending_outcomes']}"]
    for seq, n in r["request_sequences"].items():
        L.append(f"  {n} x {seq}")
    return "\n".join(L)


# dwc2 encodings (hw.h TSIZ_SC_MC_PID_*, hcd.h enum dwc2_control_phase).
PID_NAMES = {0: "DATA0", 1: "DATA2", 2: "DATA1", 3: "SETUP"}
PHASES = ["SETUP", "DATA", "STATUS"]
HCINT_BITS = {0: "XFERCOMPL", 1: "CHHLTD", 2: "AHBERR", 3: "STALL", 4: "NAK", 5: "ACK",
              6: "NYET", 7: "XACTERR", 8: "BBLERR", 9: "FRMOVRUN", 10: "DATATGLERR"}


def _hcint(v: Optional[str]) -> str:
    if v is None:
        return "?"
    x = int(v, 16)
    return "|".join(n for b, n in HCINT_BITS.items() if x >> b & 1) or "0"


def expected_runs(setup: str) -> List[Dict[str, Any]]:
    """Channel runs dwc2 programs for a control URB (hcd.c dwc2_hc_init_xfer):
    SETUP out 8 bytes with PID SETUP; DATA in the request's direction with
    wLength bytes starting at DATA1; STATUS in the opposite direction (IN if
    no data stage), 0 bytes, DATA1."""
    b = bytes.fromhex(setup)
    data_in, wlen = bool(b[0] & 0x80), b[6] | b[7] << 8
    runs = [{"phase": "SETUP", "in": 0, "pid": 3, "len": 8}]
    if wlen:
        runs.append({"phase": "DATA", "in": int(data_in), "pid": 2, "len": wlen})
    runs.append({"phase": "STATUS", "in": int(not data_in or not wlen), "pid": 2, "len": 0})
    return runs


def model_report(session_dir: str) -> Dict[str, Any]:
    """Check every successful device control URB of a 'state' capture against
    the driver's programmed sequence and count observed transitions."""
    rep = analyze(session_dir, keep_events=True)
    urbs = [u for u in rep["urbs"] if u.get("xfer") == "ctrl" and u.get("dev") not in (None, 1)
            and u.get("status") == 0 and u.get("setup")]
    checks = Counter()
    violations: List[Dict[str, Any]] = []
    transitions = Counter()
    hcint_at_comp = Counter()
    same_channel_next = Counter()
    for u in urbs:
        evs = [e for e in u["events"] if e["ev"] in ("hc_start", "xfercomp", "hc_release",
                                                       "hc_halt")]
        exp = expected_runs(u["setup"])
        starts = [e for e in evs if e["ev"] == "hc_start"]
        problems = []

        def f(e, k):
            v = e["fields"].get(k)
            return None if v is None else int(v, 0)
        if len(starts) != len(exp):
            problems.append(f"{len(starts)} channel runs, expected {len(exp)}")
        for e, x in zip(starts, exp):
            got = {"in": f(e, "in"), "pid": f(e, "pid"), "len": f(e, "len"),
                   "dev": f(e, "dev"), "ep": f(e, "ep")}
            want = dict(x, dev=u["dev"], ep=0)
            want.pop("phase")
            bad = {k: (got[k], want[k]) for k in want if got[k] != want[k]}
            if bad:
                problems.append(f"{x['phase']} start: got/expected {bad}")
        # Per channel run: start -> xfercomp(phase) -> release(reason).
        labels = []
        prev_hc = None
        for i, e in enumerate(evs):
            hc = f(e, "hc")
            if e["ev"] == "hc_start":
                ph = PHASES[len(labels)] if len(labels) < 3 else f"run{len(labels)}"
                if len(exp) == 2 and len(labels) == 1:
                    ph = "STATUS"
                labels.append(ph)
                if prev_hc is not None:
                    same_channel_next["same" if hc == prev_hc else "different"] += 1
                label = f"start[{ph},pid={PID_NAMES.get(f(e, 'pid'))},"
                label += f"{'in' if f(e, 'in') else 'out'},len={f(e, 'len')}]"
            elif e["ev"] == "xfercomp":
                ph_val = f(e, "phase")
                label = f"xfercomp[control_phase={PHASES[ph_val] if ph_val in (0, 1, 2) else ph_val}]"
                hcint_at_comp[_hcint(e["fields"].get("hcint"))] += 1
                if labels and ph_val is not None and PHASES[ph_val] != labels[-1]:
                    problems.append(f"xfercomp control_phase {ph_val} during {labels[-1]} run")
            elif e["ev"] == "hc_release":
                label = f"release[reason={f(e, 'reason')}]"
                prev_hc = hc
            else:
                label = f"halt[reason={f(e, 'reason')}]"
            if i:
                transitions[f"{prev_label} -> {label}"] += 1
            prev_label = label
        if not problems:
            checks["ok"] += 1
        else:
            violations.append({"urb": u["urb"], "instance": u["instance"], "problems": problems})
    return {"tool": "hostlab-report model", "session": session_dir,
            "profile": rep["consistency"].get("profile"),
            "urbs": len(urbs), "conforming": checks["ok"], "violations": violations[:20],
            "violation_count": len(violations),
            "transitions": dict(transitions.most_common()),
            "hcint_at_xfercomp": dict(hcint_at_comp),
            "next_phase_channel": dict(same_channel_next),
            "unattributed_channel_events": rep["channel_events"]["unattributed"],
            "ambiguous_channel_events": rep["channel_events"]["ambiguous"]}


def format_model(r: Dict[str, Any]) -> str:
    L = [f"Control-transfer model check: {r['conforming']}/{r['urbs']} URBs conform "
         f"(profile {r['profile']}); {r['violation_count']} violations; "
         f"unattributed {r['unattributed_channel_events']}, "
         f"ambiguous {r['ambiguous_channel_events']}",
         f"  HCINT at transfer-complete: {r['hcint_at_xfercomp']}",
         f"  next phase on: {r['next_phase_channel']}", "  transitions:"]
    L += [f"    {n:6d}  {t}" for t, n in r["transitions"].items()]
    for v in r["violations"][:5]:
        L.append(f"  violation {v['urb']}#{v['instance']}: {v['problems']}")
    return "\n".join(L)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="hostlab-report")
    sub = ap.add_subparsers(dest="kind", required=True)
    c = sub.add_parser("cancel")
    c.add_argument("session_dir")
    c.add_argument("--json")
    c.add_argument("--bin-us", type=int, default=20)
    mo = sub.add_parser("model")
    mo.add_argument("session_dir")
    mo.add_argument("--json")
    rs = sub.add_parser("reset")
    rs.add_argument("session_dir")
    rs.add_argument("--json")
    args = ap.parse_args(argv)
    if args.kind == "cancel":
        rep, text = cancel_report(args.session_dir, args.bin_us), None
        text = format_cancel(rep)
    elif args.kind == "model":
        rep = model_report(args.session_dir)
        text = format_model(rep)
    else:
        rep = reset_report(args.session_dir)
        text = format_reset(rep)
    if args.json:
        with open(args.json, "w") as f:
            json.dump(rep, f, indent=2, sort_keys=True)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
