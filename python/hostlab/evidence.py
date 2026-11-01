# SPDX-License-Identifier: MIT
"""hostlab-evidence: assemble a reproducible evidence bundle from recorded runs.

    hostlab-evidence --out DIR --preflight PREFLIGHT.json \\
        --session device=RUN_DIR [--session NAME=RUN_DIR ...] \\
        [--state NAME=PREFLIGHT.json ...] [--kernel-log auto|FILE|none]

The first --session is the primary one: its usbmon, kprobe events and
analysis become the top-level files. Every session is also copied to
sessions/NAME/. --state adds further preflight snapshots (for example
``dwc_otg``, ``reverted``, ``reapplied``) used by the recovery check.

Hypotheses H1-H10 (docs/plan.md) and the M1 completion checks are evaluated
only from the recorded files. Anything that was not measured is reported as
BLOCKED or INCONCLUSIVE with a reason; no result is inferred.

Privacy: USB string descriptors (which can carry serial numbers) are removed
from usbmon and user records; serial numbers, hostname and network details are
never collected.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from . import TRACE_SCHEMA, __version__
from .analyze import analyze
from .ftrace_parse import parse
from .probe import read_kernel_config

BUNDLE_SCHEMA = 1
PASS, FAIL, BLOCKED, INCONCLUSIVE = "PASS", "FAIL", "BLOCKED", "INCONCLUSIVE"

# H8 threshold: kprobes may add at most this fraction to the median
# usbmon submit->complete latency of the same command.
H8_MAX_OVERHEAD = 0.20

KLOG_KEEP = re.compile(r"dwc2|dwc_otg|usb|hub |kprobe|hostlab|Oops|BUG:|WARNING:|"
                       r"Unable to handle|Call trace|Internal error", re.I)
# Kernel oops markers are upper/mixed case and start a word ("mmc_debug:0" must
# not match BUG:); driver complaints are matched case-insensitively.
KLOG_OOPS = re.compile(r"\b(BUG|WARNING):|\bOops\b|Unable to handle kernel|Call trace:|"
                       r"Internal error:")
KLOG_DRIVER = re.compile(r"dwc2.*\b(error|timeout|fail)|kprobe.*\bfail", re.I)
MAC = re.compile(r"\b[0-9a-f]{2}(:[0-9a-f]{2}){5}\b", re.I)


# ---------------------------------------------------------------- helpers

def _load_json(path: str) -> Optional[Any]:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _load_jsonl(path: str) -> List[Dict[str, Any]]:
    try:
        with open(path) as f:
            return [json.loads(l) for l in f if l.strip()]
    except OSError:
        return []


def _write_json(path: str, obj: Any) -> None:
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
        f.write("\n")


def _write_jsonl(path: str, recs: List[Dict[str, Any]]) -> None:
    with open(path, "w") as f:
        for r in recs:
            f.write(json.dumps(r, sort_keys=True) + "\n")


def _unavailable(reason: str) -> Dict[str, Any]:
    return {"available": False, "reason": reason}


def is_string_descriptor_request(setup: Optional[str]) -> bool:
    """GET_DESCRIPTOR(STRING): bmRequestType 0x80, bRequest 6, wValue high byte 3."""
    return bool(setup) and len(setup) == 16 and setup[0:4].lower() == "8006" \
        and setup[6:8] == "03"


def redact_usbmon(recs: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    """Drop the payload of string-descriptor completions (S/C paired per address)."""
    pending: Dict[str, bool] = {}
    out, n = [], 0
    for r in recs:
        r = dict(r)
        if r.get("ev") == "urb":
            if r.get("type") == "S":
                pending[r["urb"]] = is_string_descriptor_request(r.get("setup"))
            elif pending.pop(r["urb"], False) and "data" in r:
                del r["data"]
                r["data_redacted"] = "string_descriptor"
                n += 1
        out.append(r)
    return out, n


def redact_user(recs: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    out, n = [], 0
    for r in recs:
        r = dict(r)
        if "data" in r and is_string_descriptor_request(r.get("setup")):
            del r["data"]
            r["data_redacted"] = "string_descriptor"
            n += 1
        out.append(r)
    return out, n


def ftrace_to_jsonl(path: str) -> List[Dict[str, Any]]:
    try:
        with open(path) as f:
            events = list(parse(f))
    except OSError:
        return []
    return [{"src": "ftrace", "ev": e.event, "ts_ns": e.ts_ns, "cpu": e.cpu,
             "task": e.task, "fields": e.fields} for e in events]


def kernel_log(mode: str) -> Tuple[Optional[List[str]], Optional[str]]:
    if mode == "none":
        return None, "not requested"
    if mode == "auto":
        try:
            r = subprocess.run(["dmesg"], capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired) as e:
            return None, f"dmesg failed: {e}"
        if r.returncode != 0:
            return None, f"dmesg failed (exit {r.returncode}; needs root): {r.stderr.strip()}"
        text = r.stdout
    else:
        try:
            with open(mode) as f:
                text = f.read()
        except OSError as e:
            return None, str(e)
    lines = [MAC.sub("xx:xx:xx:xx:xx:xx", l) for l in text.splitlines() if KLOG_KEEP.search(l)]
    return lines, None


def revision(start: str) -> Optional[str]:
    """Deployed REVISION file next to bin/, else git describe of the checkout."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(start)))
    try:
        with open(os.path.join(root, "REVISION")) as f:
            return f.read().strip()
    except OSError:
        pass
    try:
        r = subprocess.run(["git", "-C", root, "describe", "--always", "--dirty", "--abbrev=40"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


# ------------------------------------------------------------- evaluation

def _h(hid: str, text: str, status: str, detail: str, evidence: List[str],
       criterion: str) -> Dict[str, Any]:
    return {"id": hid, "hypothesis": text, "status": status, "detail": detail,
            "evidence": evidence, "criterion": criterion}


# H8 needs this many device control URBs on each side to compare medians.
H8_MIN_SAMPLES = 30


def _device_ctrl_latencies(analysis: Optional[Dict[str, Any]]) -> List[int]:
    """usbmon submit->complete of successful control URBs to a device (not the root hub)."""
    return sorted(u["usbmon_latency_ns"] for u in (analysis or {}).get("urbs", [])
                  if u.get("xfer") == "ctrl" and u.get("dev") != 1 and u.get("status") == 0
                  and "usbmon_latency_ns" in u)


def _median(xs: List[int]) -> int:
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) // 2


def evaluate_hypotheses(pf: Dict[str, Any], sessions: Dict[str, Dict[str, Any]],
                        primary: Optional[str]) -> List[Dict[str, Any]]:
    caps = pf.get("probe") or {}
    ctl = caps.get("controller") or {}
    summ = ctl.get("summary") or {}
    out = []

    if ctl.get("driver") != "dwc2" or not summ:
        st, d = BLOCKED, "dwc2 not bound or debugfs params unreadable at preflight"
        out.append(_h("H1", "BCM2835 host runs in buffer-DMA mode", st, d,
                      ["preflight.json"], "probe summary.host_dma_mode == buffer_dma"))
        out.append(_h("H2", "8 host channels", st, d, ["preflight.json"],
                      "params.host_channels == hw_params.host_channels == 8"))
    else:
        mode = summ.get("host_dma_mode")
        out.append(_h("H1", "BCM2835 host runs in buffer-DMA mode",
                      PASS if mode == "buffer_dma" else FAIL,
                      f"host_dma={ctl['params'].get('host_dma')} "
                      f"dma_desc_enable={ctl['params'].get('dma_desc_enable')} -> {mode}",
                      ["probe-capabilities.json", "preflight.json"],
                      "probe summary.host_dma_mode == buffer_dma"))
        hc, hwhc = summ.get("host_channels"), summ.get("hw_host_channels")
        out.append(_h("H2", "8 host channels", PASS if hc == hwhc == 8 else FAIL,
                      f"params.host_channels={hc} hw_params.host_channels={hwhc}",
                      ["probe-capabilities.json"],
                      "params.host_channels == hw_params.host_channels == 8"))

    out.append(_h("H3", "A cancelled control URB reports actual_length 0 even if DATA moved",
                  BLOCKED, "needs E3 cancel sweep against a device plus wire capture (E8, "
                  "logic analyzer); not part of M1", [], "E3 actual vs E8 wire data"))
    out.append(_h("H4", "An IN channel can DMA into the URB buffer after giveback on dequeue",
                  BLOCKED, "needs the MMAP-buffer transfer variant with a canary (M4 API "
                  "item); not implemented", [], "E3 with MMAP buffers + canary"))
    out.append(_h("H5", "Enumeration sequence after reset depends on usbcore parameters",
                  BLOCKED, "port reset (E6) is an M3 experiment; no reset was issued in M1",
                  [], "E6 + usbmon"))
    out.append(_h("H6", "Behaviour of URBs pending across USBDEVFS_RESET", BLOCKED,
                  "E6 variant; M3", [], "E6 variant"))

    ps = sessions.get(primary) if primary else None
    an = (ps or {}).get("analysis")
    sj = (ps or {}).get("session") or {}
    if not an:
        out.append(_h("H7", "The register-fetch kprobe profile is valid on the shipped kernel",
                      BLOCKED, "no primary session", [], "see criterion"))
    else:
        c = an.get("consistency", {})
        kp = sj.get("kprobes") or {}
        ctrl = [u for u in an["urbs"] if u.get("xfer") == "ctrl" and u.get("dev") != 1
                and "cancel" not in u and u.get("status") == 0]
        exact = [u for u in ctrl if u.get("phase_label_certainty") == "exact"]
        problems = []
        if kp.get("failed") or not kp.get("registered"):
            problems.append("kprobes not all registered")
        if c.get("usbmon_without_enqueue") or c.get("enqueue_without_usbmon"):
            problems.append(f"{c.get('usbmon_without_enqueue')} usbmon URBs without enqueue, "
                            f"{c.get('enqueue_without_usbmon')} enqueues without usbmon")
        if c.get("channel_numbers_out_of_range"):
            problems.append(f"channel numbers out of range {c['channel_numbers_out_of_range']}")
        detail = (f"{c.get('with_dwc2_enqueue')}/{c.get('device_urbs_in_usbmon')} device URBs "
                  f"joined; channels seen {an['channel_events']['channel_numbers_seen']} "
                  f"(limit {c.get('host_channels')}); {len(exact)}/{len(ctrl)} successful "
                  f"device control URBs with exactly one channel run per phase")
        if not c.get("device_urbs_in_usbmon") or not an["channel_events"]["total"]:
            st = INCONCLUSIVE
            detail = "no device URBs or no channel events recorded; " + detail
        elif problems:
            st = FAIL
            detail = "; ".join(problems) + "; " + detail
        elif ctrl and len(exact) == len(ctrl):
            st = PASS
        else:
            st = INCONCLUSIVE
            detail += " (some control URBs not labelled exactly; inspect analysis.json)"
        out.append(_h("H7", "The register-fetch kprobe profile is valid on the shipped kernel",
                      st, detail, ["analysis.json", "dwc2-events.jsonl", "usbmon.jsonl",
                                   f"sessions/{primary}/session.json"],
                      "all kprobes register; every device URB in usbmon has a dwc2 enqueue "
                      "with the same address and vice versa; hc < host_channels; successful "
                      "control URBs show one channel run per phase"))

    base = None
    if ps:
        for name, s in sessions.items():
            sj2 = s.get("session") or {}
            if name != primary and "kprobes" not in sj2 and sj2.get("command") == sj.get("command"):
                base = name
    if base is None:
        out.append(_h("H8", "kprobe overhead is small relative to transfer latency", BLOCKED
                      if not ps else INCONCLUSIVE,
                      "no session of the same command without kprobes (--no-ftrace)", [],
                      f"median usbmon latency with probes <= {1 + H8_MAX_OVERHEAD:.2f} x without"))
    else:
        a = _device_ctrl_latencies(an)
        b = _device_ctrl_latencies(sessions[base].get("analysis"))
        exits = (sj.get("command_exit"), (sessions[base].get("session") or {}).get("command_exit"))
        if exits != (0, 0) or min(len(a), len(b)) < H8_MIN_SAMPLES:
            st, d = INCONCLUSIVE, (f"command exits {exits}; {len(a)} vs {len(b)} successful "
                                   f"device control URBs (need {H8_MIN_SAMPLES} each, exit 0)")
        else:
            with_p, without = _median(a), _median(b)
            ratio = with_p / without
            st = PASS if ratio <= 1 + H8_MAX_OVERHEAD else FAIL
            d = f"median usbmon submit->complete of device control URBs: {with_p} ns with " \
                f"probes (n={len(a)}), {without} ns without (n={len(b)}), ratio {ratio:.3f}"
        out.append(_h("H8", "kprobe overhead is small relative to transfer latency", st, d,
                      [f"sessions/{primary}/analysis.json", f"sessions/{base}/analysis.json"],
                      f"median usbmon latency with probes <= {1 + H8_MAX_OVERHEAD:.2f} x without"))

    ft = (caps.get("facilities") or {}).get("ftrace")
    if not ft or ctl.get("driver") != "dwc2":
        out.append(_h("H9", "The shipped kernel's traceable dwc2 functions match our build",
                      BLOCKED, "tracefs unreadable or dwc2 not loaded at preflight",
                      ["preflight.json"], "hostlab_probes_missing == []"))
    else:
        miss = ft.get("hostlab_probes_missing") or []
        out.append(_h("H9", "The shipped kernel's traceable dwc2 functions match our build",
                      PASS if not miss else FAIL,
                      f"{len(ft['hostlab_probes_usable'])} profile symbols traceable, "
                      f"{len(miss)} missing", ["probe-capabilities.json", "preflight.json"],
                      "hostlab_probes_missing == []"))

    br = ((an or {}).get("clock") or {}).get("ftrace_marker_brackets") or []
    if not br:
        out.append(_h("H10", "trace_clock=mono equals CLOCK_MONOTONIC",
                      BLOCKED if not an else INCONCLUSIVE, "no marker brackets recorded",
                      ["analysis.json"], "each marker's ftrace time lies inside the "
                      "CLOCK_MONOTONIC interval read around its write"))
    else:
        ok = all(b["in_bracket"] for b in br)
        out.append(_h("H10", "trace_clock=mono equals CLOCK_MONOTONIC", PASS if ok else FAIL,
                      "; ".join(f"{b['tag']}: +{b['ftrace_minus_before_ns']} ns after the "
                                f"pre-write read, {b['after_minus_ftrace_ns']} ns before the "
                                f"post-write read" for b in br),
                      ["analysis.json", f"sessions/{primary}/session.json"],
                      "each marker's ftrace time lies inside the CLOCK_MONOTONIC interval "
                      "read around its write"))
    return out


def evaluate_m1(pf: Dict[str, Any], states: Dict[str, Dict[str, Any]],
                sessions: Dict[str, Dict[str, Any]], primary: Optional[str],
                klog: Optional[List[str]], klog_reason: Optional[str]) -> List[Dict[str, Any]]:
    checks = {c["id"]: c for c in pf.get("checks", [])}

    def from_check(cid: str) -> Tuple[str, str]:
        c = checks.get(cid)
        if not c:
            return BLOCKED, "not in preflight"
        return (PASS if c["status"] == "ok" else FAIL if c["status"] == "fail"
                else INCONCLUSIVE), c["detail"]

    out = []
    for cid, text in (("board", "Physical Pi Zero W identified"),
                      ("driver_binding", "dwc2 bound on the running kernel"),
                      ("usb_device", "A USB peripheral enumerates")):
        st, d = from_check(cid)
        out.append({"id": cid, "check": text, "status": st, "detail": d,
                    "evidence": ["preflight.json"]})

    an = (sessions.get(primary) or {}).get("analysis") if primary else None
    if not an:
        out.append({"id": "traces", "check": "usbmon and kprobe traces captured",
                    "status": BLOCKED, "detail": "no primary session", "evidence": []})
        out.append({"id": "analysis", "check": "Analysis processes hardware traces",
                    "status": BLOCKED, "detail": "no primary session", "evidence": []})
    else:
        n_mon, n_ch = an["inputs"]["usbmon_records"], an["channel_events"]["total"]
        out.append({"id": "traces", "check": "usbmon and kprobe traces captured",
                    "status": PASS if n_mon and n_ch else INCONCLUSIVE,
                    "detail": f"{n_mon} usbmon records, {an['inputs']['trace_events']} trace "
                              f"events, {n_ch} channel events",
                    "evidence": ["usbmon.jsonl", "dwc2-events.jsonl"]})
        phased = [u for u in an["urbs"] if u.get("phases")]
        out.append({"id": "analysis", "check": "Analysis processes hardware traces",
                    "status": PASS if phased else INCONCLUSIVE,
                    "detail": f"{len(an['urbs'])} URB instances, {len(phased)} with channel "
                              f"phases", "evidence": ["analysis.json"]})

    if klog is None:
        out.append({"id": "stability", "check": "System remains stable", "status": BLOCKED,
                    "detail": f"kernel log not collected: {klog_reason}", "evidence": []})
    else:
        bad = [l for l in klog if KLOG_OOPS.search(l) or KLOG_DRIVER.search(l)]
        out.append({"id": "stability", "check": "System remains stable",
                    "status": PASS if not bad else FAIL,
                    "detail": f"{len(bad)} problem lines in the filtered kernel log"
                              + (f", first: {bad[0]}" if bad else ""),
                    "evidence": ["kernel-log.txt"]})

    rev, reapp = states.get("reverted"), states.get("reapplied")
    if not rev:
        out.append({"id": "recovery", "check": "Recovery procedure validated", "status": BLOCKED,
                    "detail": "no 'reverted' state snapshot", "evidence": []})
    else:
        def drv(p):
            return ((p.get("probe") or {}).get("controller") or {}).get("driver")
        ok = drv(rev) == "dwc_otg" and not rev["boot_config"]["hostlab_block"]
        detail = f"after --revert + reboot: driver {drv(rev)}, hostlab block " \
                 f"{'present' if rev['boot_config']['hostlab_block'] else 'absent'}"
        if reapp:
            ok = ok and drv(reapp) == "dwc2"
            detail += f"; after --apply + reboot: driver {drv(reapp)}"
        out.append({"id": "recovery", "check": "Recovery procedure validated",
                    "status": PASS if ok else FAIL, "detail": detail,
                    "evidence": ["states/reverted.json"] + (["states/reapplied.json"]
                                                            if reapp else [])})
    return out


# -------------------------------------------------------------- assembly

def topology_text(devs: List[Dict[str, Any]]) -> str:
    lines = ["# USB devices from sysfs at preflight time (serial numbers not collected)"]
    for d in devs:
        ifs = ", ".join(f"{i.get('class')}/{i.get('driver')}" for i in d.get("interfaces", []))
        lines.append(f"bus {d.get('busnum'):>3} dev {d.get('devnum'):>3}  [{d['name']}]  "
                     f"{d.get('idVendor')}:{d.get('idProduct')}  usb {d.get('bcdUSB')}  "
                     f"speed {d.get('speed')} Mb/s  class {d.get('bDeviceClass')}  "
                     f"\"{d.get('manufacturer') or ''}\" \"{d.get('product') or ''}\"  "
                     f"interfaces: {ifs or '-'}")
    return "\n".join(lines) + "\n"


def load_session(d: str) -> Dict[str, Any]:
    return {"dir": d, "session": _load_json(os.path.join(d, "session.json")),
            "probe": _load_json(os.path.join(d, "probe.json")),
            "analysis": analyze(d)}


def build(args: argparse.Namespace) -> Dict[str, Any]:
    out = args.out
    if os.path.exists(out) and os.listdir(out):
        raise SystemExit(f"{out} exists and is not empty")
    os.makedirs(out, exist_ok=True)
    pf = _load_json(args.preflight)
    if pf is None:
        raise SystemExit(f"cannot read {args.preflight}")
    unavailable: List[Dict[str, str]] = []
    redactions: Dict[str, int] = {}

    sessions: Dict[str, Dict[str, Any]] = {}
    for spec in args.session or []:
        name, _, d = spec.rpartition("=")
        name = name or os.path.basename(os.path.normpath(d))
        if not os.path.isdir(d):
            raise SystemExit(f"session directory {d} does not exist")
        sessions[name] = load_session(d)
    primary = next(iter(sessions), None)
    states = {}
    for spec in args.state or []:
        name, _, p = spec.partition("=")
        states[name] = _load_json(p)
        if states[name] is None:
            raise SystemExit(f"cannot read state {p}")

    # Per-session copies.
    for name, s in sessions.items():
        sd = os.path.join(out, "sessions", name)
        os.makedirs(sd)
        for fn in ("session.json", "probe.json"):
            if os.path.exists(os.path.join(s["dir"], fn)):
                shutil.copy(os.path.join(s["dir"], fn), sd)
        mon, n1 = redact_usbmon(_load_jsonl(os.path.join(s["dir"], "usbmon.jsonl")))
        user, n2 = redact_user(_load_jsonl(os.path.join(s["dir"], "user.jsonl")))
        redactions[name] = n1 + n2
        _write_jsonl(os.path.join(sd, "usbmon.jsonl"), mon)
        _write_jsonl(os.path.join(sd, "user.jsonl"), user)
        _write_jsonl(os.path.join(sd, "dwc2-events.jsonl"),
                     ftrace_to_jsonl(os.path.join(s["dir"], "ftrace.txt")))
        _write_json(os.path.join(sd, "analysis.json"), s["analysis"])

    _write_json(os.path.join(out, "preflight.json"), pf)
    if states:
        os.makedirs(os.path.join(out, "states"))
        for name, st in states.items():
            _write_json(os.path.join(out, "states", f"{name}.json"), st)

    # Top-level files required by the bundle layout.
    hw = {"board": pf.get("board"), "os": pf.get("os"),
          "controller": (pf.get("probe") or {}).get("controller"),
          "modules": pf.get("modules"), "boot_config": pf.get("boot_config"),
          "python": pf.get("python"), "kernel_config_source": pf.get("kernel_config_source")}
    _write_json(os.path.join(out, "hardware.json"), hw)

    text, src, why = read_kernel_config("/") if args.kernel_config == "auto" else (None, None, None)
    if args.kernel_config not in ("auto", "none"):
        try:
            with open(args.kernel_config) as f:
                text, src = f.read(), args.kernel_config
        except OSError as e:
            why = str(e)
    running = (pf.get("os") or {}).get("kernel_release")
    if text is not None and args.kernel_config == "auto" and running != os.uname().release:
        text, why = None, f"bundle built on {os.uname().release}, preflight ran on {running}"
    if text is None:
        why = why or "not requested"
        with open(os.path.join(out, "kernel-config.txt"), "w") as f:
            f.write(f"# UNAVAILABLE: {why}\n")
        unavailable.append({"file": "kernel-config.txt", "reason": why})
    else:
        with open(os.path.join(out, "kernel-config.txt"), "w") as f:
            f.write(f"# source: {src} (kernel {running})\n" + text)

    with open(os.path.join(out, "usb-topology.txt"), "w") as f:
        f.write(topology_text(pf.get("usb_devices") or []))

    ps = sessions.get(primary) if primary else None
    probe_caps = (ps or {}).get("probe") or pf.get("probe")
    _write_json(os.path.join(out, "probe-capabilities.json"), probe_caps)

    if ps:
        sd = os.path.join(out, "sessions", primary)
        for fn in ("usbmon.jsonl", "dwc2-events.jsonl", "analysis.json"):
            shutil.copy(os.path.join(sd, fn), os.path.join(out, fn))
        for fn in ("usbmon.jsonl", "dwc2-events.jsonl"):
            if os.path.getsize(os.path.join(out, fn)) == 0:
                unavailable.append({"file": fn, "reason": "recorder produced no records"})
    else:
        for fn in ("usbmon.jsonl", "dwc2-events.jsonl"):
            _write_jsonl(os.path.join(out, fn),
                         [{"src": "hostlab-evidence", "ev": "unavailable",
                           "reason": "no session recorded"}])
            unavailable.append({"file": fn, "reason": "no session recorded"})
        _write_json(os.path.join(out, "analysis.json"), _unavailable("no session recorded"))
        unavailable.append({"file": "analysis.json", "reason": "no session recorded"})

    klog, klog_reason = kernel_log(args.kernel_log)
    if klog is not None:
        with open(os.path.join(out, "kernel-log.txt"), "w") as f:
            f.write("# dmesg lines matching USB/dwc2/kprobe/oops patterns; MACs masked\n")
            f.write("\n".join(klog) + "\n")
    else:
        unavailable.append({"file": "kernel-log.txt", "reason": klog_reason})

    hyps = evaluate_hypotheses(pf, sessions, primary)
    m1 = evaluate_m1(pf, states, sessions, primary, klog, klog_reason)
    _write_json(os.path.join(out, "hypothesis-results.json"),
                {"schema": BUNDLE_SCHEMA, "hypotheses": hyps, "m1_checks": m1,
                 "status_meaning": {
                     PASS: "supported by recorded evidence",
                     FAIL: "contradicted by recorded evidence",
                     BLOCKED: "not executed: required access, hardware or milestone missing",
                     INCONCLUSIVE: "executed, evidence insufficient"}})

    manifest = {
        "bundle_schema": BUNDLE_SCHEMA, "trace_schema": TRACE_SCHEMA,
        "tool": "hostlab-evidence", "version": __version__,
        "hostlab_revision": revision(sys.argv[0]),
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "preflight_utc": pf.get("generated_utc"),
        "kernel_release": running,
        "kernel_version": (pf.get("os") or {}).get("kernel_version"),
        "board": (pf.get("board") or {}).get("model"),
        "os": (pf.get("os") or {}).get("pretty_name"),
        "primary_session": primary,
        "sessions": {n: {"command": (s.get("session") or {}).get("command"),
                         "command_exit": (s.get("session") or {}).get("command_exit"),
                         "warnings": (s.get("session") or {}).get("warnings")}
                     for n, s in sessions.items()},
        "states": sorted(states),
        "unavailable": unavailable,
        "redactions": {"string_descriptor_payloads": redactions,
                       "not_collected": ["serial numbers", "hostname", "IP/MAC addresses"]},
        "m1_complete": all(c["status"] == PASS for c in m1),
    }
    with open(os.path.join(out, "test-summary.md"), "w") as f:
        f.write(summary_md(manifest, hyps, m1))
    files = []
    for dp, _, fns in os.walk(out):
        for fn in sorted(fns):
            p = os.path.join(dp, fn)
            with open(p, "rb") as f:
                files.append({"path": os.path.relpath(p, out), "bytes": os.path.getsize(p),
                              "sha256": hashlib.sha256(f.read()).hexdigest()})
    manifest["files"] = sorted(files, key=lambda x: x["path"])  # all but manifest.json
    _write_json(os.path.join(out, "manifest.json"), manifest)
    return manifest


def summary_md(man: Dict[str, Any], hyps: List[Dict[str, Any]], m1: List[Dict[str, Any]]) -> str:
    L = [f"# Evidence bundle: {man['board']}, kernel {man['kernel_release']}", "",
         f"- hostlab revision: `{man['hostlab_revision']}`",
         f"- created {man['created_utc']} (preflight {man['preflight_utc']})",
         f"- OS: {man['os']}",
         f"- primary session: `{man['primary_session']}`", "",
         f"**M1 complete: {'yes' if man['m1_complete'] else 'no'}**", "",
         "## M1 checks", "", "| Check | Status | Detail |", "|---|---|---|"]
    L += [f"| {c['check']} | {c['status']} | {c['detail']} |" for c in m1]
    L += ["", "## Hypotheses (docs/plan.md)", "", "| ID | Status | Detail | Evidence |",
          "|---|---|---|---|"]
    L += [f"| {h['id']} | {h['status']} | {h['detail']} | {', '.join(h['evidence']) or '-'} |"
          for h in hyps]
    L += ["", "## Sessions", ""]
    for n, s in man["sessions"].items():
        L.append(f"- `{n}`: `{' '.join(s['command'] or [])}` exit {s['command_exit']}"
                 + (f"; warnings: {s['warnings']}" if s["warnings"] else ""))
    if man["unavailable"]:
        L += ["", "## Unavailable", ""]
        L += [f"- `{u['file']}`: {u['reason']}" for u in man["unavailable"]]
    L.append("")
    return "\n".join(L)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="hostlab-evidence", description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--preflight", required=True, help="hostlab-preflight --json output")
    ap.add_argument("--session", action="append", help="[NAME=]RUN_DIR; first is primary")
    ap.add_argument("--state", action="append", help="NAME=PREFLIGHT.json snapshot")
    ap.add_argument("--kernel-log", default="auto", help="auto (dmesg), FILE or none")
    ap.add_argument("--kernel-config", default="auto", help="auto, FILE or none")
    args = ap.parse_args(argv)
    man = build(args)
    print(f"bundle {args.out}: m1_complete={man['m1_complete']}, "
          f"{len(man['files'])} files, {len(man['unavailable'])} unavailable")
    return 0


if __name__ == "__main__":
    sys.exit(main())
