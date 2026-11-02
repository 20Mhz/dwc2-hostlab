# SPDX-License-Identifier: MIT
"""hostlab-session: run one experiment with all non-invasive recorders.

    hostlab-session run --out DIR [--bus N] [--no-ftrace] [--no-usbmon] -- CMD [ARGS...]
    hostlab-session teardown        # remove leftover hostlab kprobes after a crash

``run`` does, in order:
  1. hostlab-probe -> DIR/probe.json
  2. tracefs (if usable): trace_clock=mono, register the hostlab kprobes that
     exist on this kernel, enable them
  3. start hostlab-mon -> DIR/usbmon.jsonl
  4. run CMD with stdout -> DIR/user.jsonl, stderr -> DIR/user.stderr
  5. stop tracing, save DIR/ftrace.txt, stop hostlab-mon
  6. always: remove hostlab's kprobes, restore trace_clock and tracing_on
  7. DIR/session.json with what was done and any warnings

Nothing here writes controller registers or changes driver state; kprobes are
observers. Teardown only removes events in the "hostlab" group.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

from . import __version__
from .probe import probe
from .tracefs import (ARM32_PROFILE, PROFILES, TraceFS, find_tracefs, read_filter_functions,
                      state_probes, usable_probes)

DEFAULT_BUFFER_KB = 4096
# Measured on the Pi Zero W (docs/m3-instrumentation.md): +12.8 % median
# latency for "fast" against +52 % for "full".
DEFAULT_PROFILE = "fast"


def _mono_ns() -> int:
    return time.clock_gettime_ns(time.CLOCK_MONOTONIC)


def _real_ns() -> int:
    return time.clock_gettime_ns(time.CLOCK_REALTIME)


def _find_tool(name: str, bindir: Optional[str]) -> Optional[str]:
    if bindir:
        cand = os.path.join(bindir, name)
        return cand if os.access(cand, os.X_OK) else None
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, "..", "..", "bin", name), shutil.which(name)):
        if cand and os.access(cand, os.X_OK):
            return os.path.abspath(cand)
    return None


class FtraceSession:
    def __init__(self, tfs: TraceFS, buffer_kb: int = DEFAULT_BUFFER_KB,
                 profile: str = DEFAULT_PROFILE, kprobes: Optional[List[str]] = None,
                 functions: Optional[List[str]] = None,
                 offsets: Optional[Dict[str, Any]] = None):
        self.tfs = tfs
        self.offsets = offsets
        self.buffer_kb = buffer_kb
        self.profile = profile
        # Explicit lists (profile "custom") override the named profile.
        self.kprobe_names = kprobes if kprobes is not None else PROFILES[profile]["kprobes"]
        self.function_names = functions if functions is not None \
            else PROFILES[profile]["functions"]
        self.functions: List[str] = []
        self.saved_tracer: Optional[str] = None
        self.saved_filter: Optional[List[str]] = None
        self.saved_clock: Optional[str] = None
        self.saved_tracing_on: Optional[str] = None
        self.saved_buffer_kb: Optional[str] = None
        self.registered: List[str] = []
        self.failed: List[Dict[str, str]] = []
        self.missing: List[str] = []
        # CLOCK_MONOTONIC read just before and just after each marker write;
        # with trace_clock=mono the marker's ftrace timestamp must fall inside.
        self.markers: List[Dict[str, Any]] = []

    def setup(self) -> None:
        funcs = read_filter_functions(self.tfs.path)
        catalogue = state_probes(self.offsets) if self.offsets else ARM32_PROFILE
        usable, missing = usable_probes(
            funcs, [p for p in catalogue if p.name in set(self.kprobe_names)])
        self.missing = [p.symbol for p in missing]
        wanted = self.function_names
        self.functions = [f for f in wanted if funcs is not None and f in funcs]
        self.missing += [f for f in wanted if f not in self.functions]
        self.saved_clock = self.tfs.current_clock()
        self.saved_tracing_on = self._read_value("tracing_on")
        self.saved_buffer_kb = self._read_value("buffer_size_kb")
        self.tfs.set_tracing_on(False)
        # Stale probes from a crashed run would make registration fail.
        self.tfs.teardown()
        self.tfs.set_clock("mono")
        self.tfs.set_buffer_kb(self.buffer_kb)
        self.tfs.clear()
        for p in usable:
            try:
                self.tfs.add_kprobe(p)
                self.registered.append(p.event)
            except OSError as e:
                self.failed.append({"probe": p.name, "error": str(e)})
        if self.registered:
            self.tfs.set_group_enabled(True)
        if self.functions:
            self.saved_tracer = self.tfs.current_tracer()
            self.saved_filter = self.tfs.ftrace_filter()
            self.tfs.set_ftrace_filter(self.functions)
            self.tfs.set_tracer("function")

    def _read_value(self, name: str) -> Optional[str]:
        try:
            return self.tfs.read(name).split()[0]
        except (OSError, IndexError):
            return None

    def start(self) -> None:
        self.tfs.set_tracing_on(True)
        self.marker("hostlab_start")

    def marker(self, tag: str) -> None:
        before = _mono_ns()
        self.tfs.marker(f"{tag} mono_ns={before} real_ns={_real_ns()}")
        self.markers.append({"tag": tag, "mono_before_ns": before, "mono_after_ns": _mono_ns()})

    def stop(self, out_path: str) -> None:
        self.marker("hostlab_stop")
        self.tfs.set_tracing_on(False)
        with open(out_path, "w") as f:
            f.write(self.tfs.read("trace"))

    def teardown(self) -> Dict[str, Any]:
        result = self.tfs.teardown()
        if self.functions:
            try:
                self.tfs.set_tracer(self.saved_tracer or "nop")
                self.tfs.set_ftrace_filter(self.saved_filter or [])
            except OSError as e:
                result["errors"].append(f"restore function tracer: {e}")
        restores = [("trace_clock", self.saved_clock),
                    ("buffer_size_kb", self.saved_buffer_kb),
                    ("tracing_on", self.saved_tracing_on)]
        for name, value in restores:
            # buffer_size_kb reads back as e.g. "1408 (expanded: 4096)"; only digits restore.
            if value is None or (name == "buffer_size_kb" and not value.isdigit()):
                continue
            try:
                self.tfs._write(name, value)
            except OSError as e:
                result["errors"].append(f"restore {name}: {e}")
        return result


def run(args: argparse.Namespace) -> int:
    os.makedirs(args.out, exist_ok=True)
    warnings: List[str] = []
    info: Dict[str, Any] = {"tool": "hostlab-session", "version": __version__,
                            "command": args.cmd, "warnings": warnings}

    caps = probe(args.root)
    with open(os.path.join(args.out, "probe.json"), "w") as f:
        json.dump(caps, f, indent=2, sort_keys=True)

    ftrace: Optional[FtraceSession] = None
    mon: Optional[subprocess.Popen] = None
    rc = 1
    try:
        if not args.no_ftrace:
            path = find_tracefs(args.root)
            if path is None:
                warnings.append("tracefs not available; kernel-side events not recorded")
            else:
                custom = args.kprobes is not None or args.functions is not None
                profile = "custom" if custom else args.profile
                offsets = None
                if args.profile == "state":
                    offsets = _load_offsets(args.offsets, caps.get("kernel_release"))
                    info["offsets"] = args.offsets
                ftrace = FtraceSession(
                    TraceFS(path), args.buffer_kb, args.profile,
                    kprobes=_names(args.kprobes) if custom else None,
                    functions=_names(args.functions) if custom else None,
                    offsets=offsets)
                ftrace.setup()
                info["profile"] = profile
                info["kprobes"] = {"registered": ftrace.registered, "failed": ftrace.failed,
                                   "missing_symbols": ftrace.missing}
                info["functions"] = ftrace.functions
                if ftrace.missing:
                    warnings.append(f"{len(ftrace.missing)} probe symbols absent "
                                    f"(e.g. {ftrace.missing[0]}); see session.json")
                if ftrace.failed:
                    warnings.append(f"{len(ftrace.failed)} kprobes failed to register "
                                    f"(e.g. {ftrace.failed[0]['probe']}: "
                                    f"{ftrace.failed[0]['error']}); see session.json")

        if not args.no_usbmon:
            mon_bin = _find_tool("hostlab-mon", args.bindir)
            if mon_bin is None:
                warnings.append("hostlab-mon not found; usbmon not recorded")
            else:
                mon = subprocess.Popen([mon_bin, "-b", str(args.bus), "-s", str(args.snaplen),
                                        "-o", os.path.join(args.out, "usbmon.jsonl")])
                time.sleep(0.3)  # let it open /dev/usbmonN before traffic starts
                if mon.poll() is not None:
                    warnings.append(f"hostlab-mon exited early with {mon.returncode}")
                    mon = None

        if ftrace:
            ftrace.start()
        t0 = _mono_ns()
        with open(os.path.join(args.out, "user.jsonl"), "w") as out, \
                open(os.path.join(args.out, "user.stderr"), "w") as err:
            rc = subprocess.call(args.cmd, stdout=out, stderr=err)
        info["command_exit"] = rc
        info["command_duration_ns"] = _mono_ns() - t0
        if ftrace:
            ftrace.stop(os.path.join(args.out, "ftrace.txt"))
    finally:
        if mon is not None:
            mon.send_signal(signal.SIGINT)
            try:
                mon.wait(timeout=5)
            except subprocess.TimeoutExpired:
                mon.kill()
                warnings.append("hostlab-mon did not stop on SIGINT; killed")
        if ftrace is not None:
            info["markers"] = ftrace.markers
            info["teardown"] = ftrace.teardown()
        with open(os.path.join(args.out, "session.json"), "w") as f:
            json.dump(info, f, indent=2, sort_keys=True)
    for w in warnings:
        print(f"hostlab-session: warning: {w}", file=sys.stderr)
    return rc


def _load_offsets(path: Optional[str], release: Optional[str]) -> Dict[str, Any]:
    """Offsets for profile "state"; refused unless generated for this kernel."""
    if not path:
        raise SystemExit("hostlab-session: --profile state needs --offsets FILE "
                         "(scripts/pi/dwc2-offsets.sh)")
    with open(path) as f:
        off = json.load(f)
    if off.get("kernel_release") != release:
        raise SystemExit(f"hostlab-session: {path} is for {off.get('kernel_release')}, "
                         f"running {release}")
    if off.get("missing"):
        raise SystemExit(f"hostlab-session: {path} lacks members {off['missing']}")
    return off


def _names(arg: Optional[str]) -> List[str]:
    return [n for n in (arg or "").split(",") if n]


def teardown(args: argparse.Namespace) -> int:
    path = find_tracefs(args.root)
    if path is None:
        print("hostlab-session: tracefs not found", file=sys.stderr)
        return 1
    tfs = TraceFS(path)
    tfs.set_tracing_on(False)
    result = tfs.teardown()
    print(json.dumps(result))
    return 1 if result["errors"] else 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="hostlab-session")
    ap.add_argument("--root", default="/", help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="action", required=True)
    r = sub.add_parser("run")
    r.add_argument("--out", required=True)
    r.add_argument("--bus", type=int, default=0, help="usbmon bus (0 = all)")
    r.add_argument("--snaplen", type=int, default=256)
    r.add_argument("--buffer-kb", type=int, default=DEFAULT_BUFFER_KB)
    r.add_argument("--bindir", help="directory containing hostlab-mon")
    r.add_argument("--profile", choices=sorted(PROFILES), default=DEFAULT_PROFILE,
                   help="instrumentation profile (default %(default)s); see tracefs.PROFILES")
    r.add_argument("--offsets", help="dwc2 struct offsets JSON for --profile state")
    r.add_argument("--kprobes", help="comma-separated probe names (tracefs.ARM32_PROFILE); "
                                     "with --functions, replaces --profile")
    r.add_argument("--functions", help="comma-separated functions for the function tracer")
    r.add_argument("--no-ftrace", action="store_true")
    r.add_argument("--no-usbmon", action="store_true")
    r.add_argument("cmd", nargs=argparse.REMAINDER)
    sub.add_parser("teardown")
    args = ap.parse_args(argv)
    if args.action == "run":
        if args.cmd and args.cmd[0] == "--":
            args.cmd = args.cmd[1:]
        if not args.cmd:
            ap.error("run needs a command after --")
        return run(args)
    return teardown(args)


if __name__ == "__main__":
    sys.exit(main())
