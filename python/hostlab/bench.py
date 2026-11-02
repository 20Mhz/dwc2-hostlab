# SPDX-License-Identifier: MIT
"""hostlab-bench: measure the latency cost of each instrumentation profile.

    sudo hostlab-bench --device BUS:DEV --out DIR [--profiles none,fphase,...]
                       [--rounds 5] [--repeat 200] [--seed 1]

Every round runs one hostlab-session per profile, in a shuffled order (seeded,
so it is reproducible) to spread slow drift over all profiles. Each session
sends --repeat GET_DESCRIPTOR(DEVICE) requests with hostlab-ctl. "none" is the
baseline: usbmon only, no ftrace.

Latency is usbmon submit -> complete of successful control URBs to the
device, as in H8. Per profile the report gives the pooled median and P95,
the per-run medians and their spread, overhead against the baseline, and
how completely phases and the URB join were reconstructed.

Run-to-run spread of the baseline is the noise floor: an overhead smaller
than it is not distinguishable from device or scheduler variation.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

from . import __version__
from .analyze import analyze
from .tracefs import PROFILES

DEFAULT_PROFILES = ["none", "idle", "fast", "fphase", "minimal", "phase", "channel", "full"]


def percentile(xs: List[int], p: float) -> Optional[int]:
    """Nearest-rank percentile of a list (sorted or not)."""
    if not xs:
        return None
    s = sorted(xs)
    k = max(1, math.ceil(p / 100 * len(s)))
    return s[min(k, len(s)) - 1]


def device_latencies(rep: Dict[str, Any]) -> List[int]:
    return [u["usbmon_latency_ns"] for u in rep["urbs"]
            if u.get("xfer") == "ctrl" and u.get("dev") != 1 and u.get("status") == 0
            and "usbmon_latency_ns" in u]


def user_latencies(run_dir: str) -> List[int]:
    out = []
    try:
        with open(os.path.join(run_dir, "user.jsonl")) as f:
            for line in f:
                r = json.loads(line)
                if r.get("ev") == "xfer" and r.get("status") == "ok":
                    out.append(r["t_done_ns"] - r["t_submit_ns"])
    except (OSError, ValueError, KeyError):
        pass
    return out


def summarize(runs: List[Dict[str, Any]], profiles: List[str]) -> Dict[str, Any]:
    by: Dict[str, Dict[str, Any]] = {}
    for p in profiles:
        rs = [r for r in runs if r["profile"] == p]
        lat = [x for r in rs for x in r["latencies"]]
        ulat = [x for r in rs for x in r["user_latencies"]]
        meds = [r["median"] for r in rs if r["median"] is not None]
        ok = sum(r["ctrl_urbs_ok"] or 0 for r in rs)
        exact = sum(r["ctrl_urbs_phases_exact"] or 0 for r in rs)
        joined = [r for r in rs if r["with_dwc2_enqueue"] is not None]
        by[p] = {
            "runs": len(rs), "urbs": len(lat),
            "median_ns": percentile(lat, 50), "p95_ns": percentile(lat, 95),
            "run_medians_ns": meds,
            "run_median_spread_ns": (max(meds) - min(meds)) if meds else None,
            "user_median_ns": percentile(ulat, 50),
            "hits_per_transfer": PROFILES[p]["hits"] if p in PROFILES else 0 if p == "none"
            else None,
            "phases_exact": exact if p != "none" else None,
            "phases_total": ok if p != "none" else None,
            "join_ok": sum(r["with_dwc2_enqueue"] for r in joined) if joined else None,
            "join_total": sum(r["device_urbs"] for r in joined) if joined else None,
            "ambiguous_channel_events": sum(r["ambiguous"] for r in rs),
            "command_failures": sum(1 for r in rs if r["exit"] != 0),
        }
    base = by.get("none")
    for p, s in by.items():
        if base and base["median_ns"] and s["median_ns"]:
            s["overhead_median"] = round(s["median_ns"] / base["median_ns"] - 1, 4)
            s["overhead_p95"] = round(s["p95_ns"] / base["p95_ns"] - 1, 4)
            # Distinguishable from noise only if every run of the profile is
            # slower than every baseline run.
            s["above_noise"] = bool(s["run_medians_ns"] and base["run_medians_ns"]
                                    and min(s["run_medians_ns"]) > max(base["run_medians_ns"]))
            if s["hits_per_transfer"]:
                s["cost_per_hit_ns"] = int((s["median_ns"] - base["median_ns"])
                                           / s["hits_per_transfer"])
    return by


def session_args(spec: str) -> List[str]:
    """Profile name, "none", or an ad-hoc set: "k:giveback+urb_enqueue",
    "f:dwc2_hc_start_transfer", or both joined by "/" ("k:a/f:b")."""
    if spec == "none":
        return ["--no-ftrace"]
    if spec in PROFILES:
        return ["--profile", spec]
    kp, fn = [], []
    for part in spec.split("/"):
        kind, _, names = part.partition(":")
        if kind not in ("k", "f") or not names:
            raise ValueError(f"bad profile spec {spec!r}")
        (kp if kind == "k" else fn).extend(names.split("+"))
    return ["--kprobes", ",".join(kp), "--functions", ",".join(fn)]


def run_summary(rd: str, rep: Dict[str, Any]) -> Dict[str, Any]:
    lat = device_latencies(rep)
    c = rep["consistency"]
    return {"latencies": lat, "median": percentile(lat, 50), "user_latencies": user_latencies(rd),
            "ctrl_urbs_ok": c["ctrl_urbs_ok"],
            "ctrl_urbs_phases_exact": c["ctrl_urbs_phases_exact"],
            "with_dwc2_enqueue": c["with_dwc2_enqueue"],
            "device_urbs": c["device_urbs_in_usbmon"],
            "ambiguous": rep["channel_events"]["ambiguous"]}


def reanalyze(out: str) -> Dict[str, Any]:
    """Recompute every run's analysis and the summary from the recorded
    sessions (the latencies are measurements and do not change; join and
    phase columns follow the current analyser)."""
    with open(os.path.join(out, "bench.json")) as f:
        res = json.load(f)
    runs = []
    for r in res["runs"]:
        rd = os.path.join(out, r["dir"])
        rep = analyze(rd)
        with open(os.path.join(rd, "analysis.json"), "w") as f:
            json.dump(rep, f, indent=2, sort_keys=True)
        r.update(run_summary(rd, rep))
        runs.append(r)
    res["profiles"] = summarize(runs, list(res["profiles"]))
    res["runs"] = [{k: v for k, v in r.items() if k not in ("latencies", "user_latencies")}
                   for r in runs]
    res["reanalyzed"] = {"version": __version__,
                         "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    return res


def format_md(res: Dict[str, Any]) -> str:
    L = [f"# Instrumentation benchmark", "",
         f"- device {res['device']}, {res['rounds']} rounds x {res['repeat']} transfers per "
         f"profile, seed {res['seed']}, hostlab {res['revision']}",
         f"- kernel {res['kernel']}, {res['started']} .. {res['finished']}", "",
         "Latency: usbmon submit -> complete of successful device control URBs.", "",
         "| Profile | Hits | URBs | Median us | P95 us | Overhead (median) | Overhead (P95) | "
         "Run-median spread us | Above noise | Phases exact | Join |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    us = lambda v: "-" if v is None else f"{v / 1000:.1f}"
    hits = lambda v: "?" if v is None else str(v)
    pct = lambda v: "-" if v is None else f"{v * 100:+.1f}%"
    frac = lambda a, b: "-" if a is None else f"{a}/{b}"
    for p, s in res["profiles"].items():
        L.append(f"| {p} | {hits(s['hits_per_transfer'])} | {s['urbs']} | {us(s['median_ns'])} | "
                 f"{us(s['p95_ns'])} | {pct(s.get('overhead_median'))} | "
                 f"{pct(s.get('overhead_p95'))} | {us(s['run_median_spread_ns'])} | "
                 f"{'yes' if s.get('above_noise') else 'no' if p != 'none' else '-'} | "
                 f"{frac(s['phases_exact'], s['phases_total'])} | "
                 f"{frac(s['join_ok'], s['join_total'])} |")
    return "\n".join(L) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="hostlab-bench", description=__doc__.splitlines()[0])
    ap.add_argument("--device")
    ap.add_argument("--out", required=True)
    ap.add_argument("--reanalyze", action="store_true",
                    help="recompute analyses and summary of an existing --out")
    ap.add_argument("--profiles", default=",".join(DEFAULT_PROFILES))
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--repeat", type=int, default=200)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--offsets", help="offsets JSON, passed to sessions of profile 'state'")
    args = ap.parse_args(argv)
    if args.reanalyze:
        res = reanalyze(args.out)
        write_results(args.out, res)
        return 0
    if not args.device:
        ap.error("--device is required")
    profiles = args.profiles.split(",")
    for p in profiles:
        try:
            session_args(p)
        except ValueError as e:
            ap.error(str(e))
    if not (1 <= args.rounds <= 20 and 1 <= args.repeat <= 2000):
        ap.error("rounds 1..20, repeat 1..2000")
    os.makedirs(args.out, exist_ok=False)

    here = os.path.dirname(os.path.realpath(sys.argv[0]))
    session_bin, ctl_bin = os.path.join(here, "hostlab-session"), os.path.join(here, "hostlab-ctl")
    root = os.path.dirname(here)
    try:
        with open(os.path.join(root, "REVISION")) as f:
            revision = f.read().strip()
    except OSError:
        revision = None
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    runs = []
    for rnd in range(args.rounds):
        order = list(profiles)
        random.Random(args.seed * 1000 + rnd).shuffle(order)
        for p in order:
            rd = os.path.join(args.out, "runs", f"r{rnd}-{p}")
            extra = ["--offsets", args.offsets] if p == "state" and args.offsets else []
            cmd = [session_bin, "run", "--out", rd] + session_args(p) + extra + [
                "--", ctl_bin, "get-descriptor", args.device, "--repeat", str(args.repeat)]
            rc = subprocess.call(cmd, stdout=subprocess.DEVNULL)
            rep = analyze(rd)
            with open(os.path.join(rd, "analysis.json"), "w") as f:
                json.dump(rep, f, indent=2, sort_keys=True)
            row = {"round": rnd, "profile": p, "dir": os.path.relpath(rd, args.out), "exit": rc}
            row.update(run_summary(rd, rep))
            runs.append(row)
            print(f"round {rnd} {p:8s} exit {rc} n={len(row['latencies'])} "
                  f"median={row['median']}", flush=True)
    res = {"tool": "hostlab-bench", "version": __version__, "revision": revision,
           "device": args.device, "rounds": args.rounds, "repeat": args.repeat,
           "seed": args.seed, "kernel": os.uname().release, "started": started,
           "finished": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "profiles": summarize(runs, profiles),
           "runs": [{k: v for k, v in r.items() if k not in ("latencies", "user_latencies")}
                    for r in runs]}
    write_results(args.out, res)
    return 0


def write_results(out: str, res: Dict[str, Any]) -> None:
    with open(os.path.join(out, "bench.json"), "w") as f:
        json.dump(res, f, indent=2, sort_keys=True)
    with open(os.path.join(out, "bench.md"), "w") as f:
        f.write(format_md(res))
    print(format_md(res))


if __name__ == "__main__":
    sys.exit(main())
