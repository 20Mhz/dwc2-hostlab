#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Run one transfer under the phase hold and release it after a delay.

    hold_release.py --ctl BIN --device BUS:DEV --delay-ms D [--count N] --out FILE.jsonl

For each of N transfers: start `hostlab-ctl get-descriptor`, poll the
debugfs phase_hold file until a transfer is held, sleep D ms, write
"release", wait for the command. Records CLOCK_MONOTONIC times and the
command's own record. Run as root with the hold configured.
"""
import argparse
import json
import subprocess
import sys
import time

CTRL = "/sys/kernel/debug/usb/20980000.usb/phase_hold"


def held() -> int:
    with open(CTRL) as f:
        for tok in f.read().split():
            if tok.startswith("held="):
                return int(tok[5:])
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ctl", required=True)
    ap.add_argument("--device", required=True)
    ap.add_argument("--delay-ms", type=float, required=True)
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    if not 1 <= a.count <= 100 or not 0 <= a.delay_ms <= 900:
        ap.error("count 1..100, delay 0..900 ms")
    with open(a.out, "w") as out:
        for i in range(a.count):
            t0 = time.monotonic_ns()
            p = subprocess.Popen([a.ctl, "get-descriptor", a.device, "--timeout", "2000"],
                                 stdout=subprocess.PIPE, text=True)
            t_held = None
            while time.monotonic_ns() - t0 < 1_000_000_000:
                if held():
                    t_held = time.monotonic_ns()
                    break
            if t_held is not None:
                time.sleep(a.delay_ms / 1000)
            t_rel = time.monotonic_ns()
            with open(CTRL, "w") as f:
                f.write("release")
            stdout, _ = p.communicate(timeout=5)
            xfer = next((json.loads(l) for l in stdout.splitlines()
                         if '"ev":"xfer"' in l), None)
            out.write(json.dumps({"i": i, "delay_ms": a.delay_ms, "t_start_ns": t0,
                                  "t_held_seen_ns": t_held, "t_release_ns": t_rel,
                                  "xfer": xfer}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
