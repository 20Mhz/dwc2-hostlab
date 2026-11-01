# SPDX-License-Identifier: MIT
"""hostlab-preflight: check that this system is ready for a hostlab session.

Read-only: it reads procfs, sysfs, debugfs/tracefs, the boot config and the
module tree, and runs ``hostlab-ctl --help``. It never loads modules, mounts
filesystems, registers kprobes or edits files. Run it as root to see debugfs
and tracefs; as a normal user those checks are reported as ``unknown``.

    hostlab-preflight [--json OUT] [--bindir DIR] [--root DIR]

Prints a human-readable report and, with --json, writes the same data as JSON.
Serial numbers, MAC/IP addresses and the hostname are deliberately not
collected.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import platform
import re
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

from . import TRACE_SCHEMA, __version__
from .probe import _p, _read, probe, read_kernel_config

OK, WARN, FAIL, UNKNOWN = "ok", "warn", "fail", "unknown"

# New-style revision code fields (Raspberry Pi documentation, "Raspberry Pi
# revision codes"). Only values relevant to this project are named.
REV_TYPES = {0x09: "Zero", 0x0C: "Zero W", 0x12: "Zero 2 W"}
REV_PROCESSORS = {0: "BCM2835", 1: "BCM2836", 2: "BCM2837", 3: "BCM2711", 4: "BCM2712"}
REV_MANUFACTURERS = {0: "Sony UK", 1: "Egoman", 2: "Embest", 3: "Sony Japan",
                     4: "Embest", 5: "Stadium"}
REV_MEMORY_MB = {0: 256, 1: 512, 2: 1024, 3: 2048, 4: 4096, 5: 8192, 6: 16384}

USB_ATTRS = ["busnum", "devnum", "devpath", "idVendor", "idProduct", "bDeviceClass", "bcdDevice",
             "version", "speed", "maxchild", "manufacturer", "product", "bNumConfigurations"]


def decode_revision(code: Optional[str]) -> Optional[Dict[str, Any]]:
    if not code:
        return None
    try:
        v = int(code, 16)
    except ValueError:
        return {"code": code, "decoded": False}
    if not v & (1 << 23):
        return {"code": code, "decoded": False, "reason": "old-style revision code"}
    t = (v >> 4) & 0xFF
    return {
        "code": code, "decoded": True,
        "type": REV_TYPES.get(t, f"0x{t:02x}"),
        "board_revision": f"1.{v & 0xF}",
        "processor": REV_PROCESSORS.get((v >> 12) & 0xF),
        "manufacturer": REV_MANUFACTURERS.get((v >> 16) & 0xF),
        "memory_mb": REV_MEMORY_MB.get((v >> 20) & 0x7),
    }


def board(root: str) -> Dict[str, Any]:
    cpuinfo = _read(root, "/proc/cpuinfo") or ""
    rev = re.search(r"^Revision\s*:\s*(\S+)", cpuinfo, re.M)
    cpu = re.search(r"^model name\s*:\s*(.+)$", cpuinfo, re.M)
    model = _read(root, "/proc/device-tree/model")
    return {
        "model": model.rstrip("\0\n") if model else None,
        "revision": decode_revision(rev.group(1) if rev else None),
        "cpu": cpu.group(1).strip() if cpu else None,
    }


def os_info(root: str) -> Dict[str, Any]:
    rel: Dict[str, str] = {}
    for line in (_read(root, "/etc/os-release") or "").splitlines():
        k, _, v = line.partition("=")
        if k in ("PRETTY_NAME", "VERSION_CODENAME", "ID"):
            rel[k.lower()] = v.strip('"')
    version = _read(root, "/proc/version")
    return {
        **rel,
        "kernel_release": (_read(root, "/proc/sys/kernel/osrelease") or "").strip() or None,
        "kernel_version": version.strip() if version else None,
        "machine": platform.machine() if root == "/" else None,
    }


def boot_config(root: str) -> Dict[str, Any]:
    """dtoverlay=dwc2 lines in config.txt and the section each is in.

    Mirrors scripts/pi/enable-dwc2.sh: only lines with no filter or under
    [all], [pi0] or [pi0w] apply to a Zero W.
    """
    for cand in ("/boot/firmware/config.txt", "/boot/config.txt"):
        text = _read(root, cand)
        if text is not None:
            break
    else:
        return {"path": None}
    lines, section = [], None
    for n, raw in enumerate(text.splitlines(), 1):
        s = raw.strip()
        m = re.fullmatch(r"\[([^\]]*)\]", s)
        if m:
            f = m.group(1).strip()
            section = None if f in ("all", "pi0", "pi0w") else f"[{f}]"
            continue
        if re.match(r"dtoverlay\s*=\s*dwc2([\s,]|$)", s):
            lines.append({"line": n, "text": s, "applies": section is None,
                          "section": section or "all"})
    backups = sorted(os.path.basename(b) for b in glob.glob(_p(root, cand) + ".hostlab-*"))
    return {
        "path": cand,
        "dwc2_overlay_lines": lines,
        "dwc2_overlay_effective": any(x["applies"] for x in lines),
        "hostlab_block": "# dwc2-hostlab:" in text,
        "hostlab_backups": backups,
    }


def modules(root: str, release: Optional[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if not release:
        return out
    base = _p(root, f"/lib/modules/{release}")
    builtin = _read_abs_lines(os.path.join(base, "modules.builtin"))
    for name, rel in (("dwc2", "kernel/drivers/usb/dwc2/dwc2"),
                      ("usbmon", "kernel/drivers/usb/mon/usbmon"),
                      ("dwc_otg", "kernel/drivers/usb/host/dwc_otg/dwc_otg")):
        files = glob.glob(os.path.join(base, rel + ".ko*"))
        out[name] = {
            "loaded": os.path.isdir(_p(root, f"/sys/module/{name}")),
            "module_file": "/" + os.path.relpath(files[0], root) if files else None,
            "builtin": any(b.startswith(rel + ".ko") for b in builtin),
        }
    return out


def _read_abs_lines(path: str) -> List[str]:
    try:
        with open(path) as f:
            return f.read().splitlines()
    except OSError:
        return []


def usb_topology(root: str) -> List[Dict[str, Any]]:
    """USB devices from sysfs. Serial numbers are not read."""
    devs = []
    for d in sorted(glob.glob(_p(root, "/sys/bus/usb/devices/*"))):
        name = os.path.basename(d)
        if ":" in name:  # interfaces
            continue
        rec: Dict[str, Any] = {"name": name}
        for a in USB_ATTRS:
            v = _read_abs_str(os.path.join(d, a))
            if v is not None:
                rec[a] = int(v) if a in ("busnum", "devnum", "maxchild", "bNumConfigurations") \
                    and v.isdigit() else v
        ifaces = []
        for i in sorted(glob.glob(d + "/*:*")):  # root hub "usb1" has "1-0:1.0"
            cls = _read_abs_str(os.path.join(i, "bInterfaceClass"))
            drv = os.path.realpath(os.path.join(i, "driver"))
            ifaces.append({"name": os.path.basename(i), "class": cls,
                           "driver": os.path.basename(drv) if os.path.exists(drv) else None})
        rec["interfaces"] = ifaces
        rec["root_hub"] = name.startswith("usb")
        devs.append(rec)
    return devs


def _read_abs_str(path: str) -> Optional[str]:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def tracepoints(tfs: Optional[str]) -> Optional[Dict[str, Any]]:
    """USB-related tracepoint subsystems (none exist for dwc2 or usbcore 6.x)."""
    if tfs is None:
        return None
    ev = os.path.join(tfs, "events")
    if not os.path.isdir(ev):
        return {"events_dir": False}
    systems = sorted(e for e in os.listdir(ev) if os.path.isdir(os.path.join(ev, e)))
    usb = [s for s in systems if re.search(r"usb|dwc|xhci|hcd|gadget", s)]
    return {"events_dir": True, "subsystems": len(systems), "usb_related": usb}


def tool_check(bindir: Optional[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for t, flag in (("hostlab-ctl", "--help"), ("hostlab-mon", "-h")):
        path = os.path.join(bindir, t) if bindir else None
        # Relative path only: the absolute one would carry the user's home.
        shown = os.path.relpath(path, os.path.dirname(os.path.abspath(bindir))) if path else None
        if not path or not os.access(path, os.X_OK):
            out[t] = {"path": shown, "runs": False, "reason": "not found or not executable"}
            continue
        try:
            r = subprocess.run([path, flag], capture_output=True, timeout=10)
            out[t] = {"path": shown, "runs": r.returncode == 0}
        except (OSError, subprocess.TimeoutExpired) as e:
            out[t] = {"path": shown, "runs": False, "reason": str(e)}
    return out


def collect(root: str = "/", bindir: Optional[str] = None) -> Dict[str, Any]:
    caps = probe(root)
    osi = os_info(root)
    release = osi.get("kernel_release")
    cfg_text, cfg_src, _ = read_kernel_config(root)
    fac = caps["facilities"]
    ftrace = fac.get("ftrace")
    tfs = _p(root, ftrace["path"]) if ftrace else None
    rep: Dict[str, Any] = {
        "tool": "hostlab-preflight", "version": __version__, "schema": TRACE_SCHEMA,
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "euid": caps["euid"],
        "board": board(root),
        "os": osi,
        "boot_config": boot_config(root),
        "modules": modules(root, release),
        "usb_devices": usb_topology(root),
        "tracepoints": tracepoints(tfs),
        "tools": tool_check(bindir),
        "python": platform.python_version(),
        "kernel_config_source": cfg_src,
        "probe": caps,
    }
    rep["checks"] = checks(rep)
    rep["ready"] = {
        "dwc2_session": all(c["status"] == OK for c in rep["checks"] if c["needed_for_dwc2"]),
        "blocking": [c["id"] for c in rep["checks"]
                     if c["needed_for_dwc2"] and c["status"] != OK],
    }
    return rep


def _check(cid: str, status: str, detail: str, needed: bool = True) -> Dict[str, Any]:
    return {"id": cid, "status": status, "detail": detail, "needed_for_dwc2": needed}


def checks(rep: Dict[str, Any]) -> List[Dict[str, Any]]:
    caps, fac = rep["probe"], rep["probe"]["facilities"]
    ctl = caps["controller"]
    cfg = fac.get("kernel_config") or {}
    out = []

    b = rep["board"]
    rev = b.get("revision") or {}
    is_zero_w = rev.get("type") == "Zero W" or "Zero W" in (b.get("model") or "")
    out.append(_check("board", OK if is_zero_w else WARN,
                      f"{b.get('model')} (revision {rev.get('code')})", needed=False))

    if fac.get("kernel_config") is None:
        out.append(_check("kernel_config", UNKNOWN, "kernel config not readable"))
    else:
        need = {"CONFIG_USB_DWC2": ("m", "y"), "CONFIG_USB_MON": ("m", "y"),
                "CONFIG_KPROBES": ("y",), "CONFIG_KPROBE_EVENTS": ("y",),
                "CONFIG_DEBUG_FS": ("y",)}
        bad = [k for k, ok in need.items() if cfg.get(k) not in ok]
        out.append(_check("kernel_config", FAIL if bad else OK,
                          ("missing: " + ", ".join(bad)) if bad else
                          f"dwc2={cfg.get('CONFIG_USB_DWC2')} usbmon={cfg.get('CONFIG_USB_MON')} "
                          f"kprobe_events=y, from {rep['kernel_config_source']}"))

    bc = rep["boot_config"]
    if bc.get("path") is None:
        out.append(_check("boot_overlay", UNKNOWN, "config.txt not found"))
    else:
        out.append(_check("boot_overlay", OK if bc["dwc2_overlay_effective"] else FAIL,
                          ("dtoverlay=dwc2 applies to this board" if bc["dwc2_overlay_effective"]
                           else "no dtoverlay=dwc2 for this board; "
                                "run scripts/pi/enable-dwc2.sh --apply and reboot")
                          + (f" (hostlab block present)" if bc["hostlab_block"] else "")))

    drv = ctl.get("driver")
    out.append(_check("driver_binding", OK if drv == "dwc2" else FAIL,
                      f"20980000.usb bound to {drv}" if drv else "no USB controller driver bound"))

    if drv == "dwc2":
        summ = ctl.get("summary")
        out.append(_check("dwc2_debugfs", OK if summ else UNKNOWN,
                          f"dma={summ['host_dma_mode']} channels={summ['host_channels']} "
                          f"core={summ['core_revision']}" if summ
                          else caps["unknown"].get("controller.params", "unreadable")))

    devs = [d for d in rep["usb_devices"] if not d["root_hub"]]
    out.append(_check("usb_device", OK if devs else FAIL,
                      ", ".join(f"{d.get('busnum')}:{d.get('devnum')} {d.get('idVendor')}:"
                                f"{d.get('idProduct')} {d.get('product') or ''}".strip()
                                for d in devs) or "only root hubs present; attach a test device"))

    um = fac["usbmon"]
    out.append(_check("usbmon", OK if um["devices"] else FAIL,
                      f"/dev/{um['devices'][0]}.." if um["devices"] else
                      "usbmon not loaded (scripts/pi/prepare-session.sh loads it)"))

    ft = fac.get("ftrace")
    root_user = caps["euid"] in (0, None)
    if ft is None or (not ft["available_filter_functions"] and not root_user):
        # tracefs files are root-only (0640); without root nothing can be concluded.
        out.append(_check("tracefs", UNKNOWN if not root_user else FAIL,
                          "tracefs not readable without root" if not root_user
                          else caps["unknown"].get("facilities.ftrace", "")))
    else:
        out.append(_check("tracefs", OK if ft["kprobe_events"] and ft["mono_clock"] else FAIL,
                          f"{ft['path']}, kprobe_events={ft['kprobe_events']}, "
                          f"mono clock={ft['mono_clock']}"))
        missing = ft["hostlab_probes_missing"]
        dwc2_loaded = rep["modules"].get("dwc2", {}).get("loaded")
        note = "" if dwc2_loaded or not missing else " (dwc2 module not loaded)"
        out.append(_check("kprobe_targets", OK if not missing else FAIL,
                          f"{len(ft['hostlab_probes_usable'])} usable, {len(missing)} missing"
                          + (": " + ", ".join(m["symbol"] for m in missing) if missing else "")
                          + note))

    tools = rep["tools"]
    bad = [t for t, v in tools.items() if not v.get("runs")]
    out.append(_check("hostlab_tools", FAIL if bad else OK,
                      ("not runnable: " + ", ".join(bad)) if bad else "hostlab-ctl, hostlab-mon run"))

    py = tuple(int(x) for x in rep["python"].split(".")[:2])
    out.append(_check("python", OK if py >= (3, 7) else FAIL, rep["python"]))

    if caps["euid"] not in (0, None):
        out.append(_check("privileges", WARN, "not root: debugfs, tracefs and usbmon need root",
                          needed=False))
    return out


def format_text(rep: Dict[str, Any]) -> str:
    o, b = rep["os"], rep["board"]
    lines = [f"hostlab-preflight {rep['version']} {rep['generated_utc']}",
             f"  board:  {b.get('model')}  cpu: {b.get('cpu')}",
             f"  os:     {o.get('pretty_name')}  kernel {o.get('kernel_release')}"]
    for c in rep["checks"]:
        mark = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL", "unknown": "??  "}[c["status"]]
        lines.append(f"  [{mark}] {c['id']:<15} {c['detail']}")
    r = rep["ready"]
    lines.append("  ready for a dwc2 session: " +
                 ("yes" if r["dwc2_session"] else "no (" + ", ".join(r["blocking"]) + ")"))
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="hostlab-preflight", description=__doc__.splitlines()[0])
    ap.add_argument("--root", default="/", help=argparse.SUPPRESS)
    ap.add_argument("--bindir", default=os.path.dirname(os.path.abspath(sys.argv[0])),
                    help="directory with hostlab-ctl/hostlab-mon (default: this script's)")
    ap.add_argument("--json", help="write the full report as JSON to this file ('-' = stdout)")
    args = ap.parse_args(argv)
    rep = collect(args.root, args.bindir)
    if args.json == "-":
        json.dump(rep, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0
    if args.json:
        with open(args.json, "w") as f:
            json.dump(rep, f, indent=2, sort_keys=True)
    print(format_text(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
