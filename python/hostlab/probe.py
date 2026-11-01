# SPDX-License-Identifier: MIT
"""hostlab-probe: report what this system can observe and control.

Read-only. Every value it cannot determine is reported as null together with a
reason; nothing is guessed. ``--root`` points at an alternative filesystem root
so the probe can be tested against recorded or synthetic sysfs/debugfs trees.

    python3 -m hostlab.probe [--root DIR] [--pretty]
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import os
import sys
from typing import Any, Dict, List, Optional

from . import TRACE_SCHEMA, __version__
from .tracefs import ARM32_PROFILE, find_tracefs, read_filter_functions, usable_probes

CONFIG_KEYS = [
    "CONFIG_USB_DWC2", "CONFIG_USB_DWC2_DUAL_ROLE", "CONFIG_USB_DWC2_HOST",
    "CONFIG_USB_DWC2_DEBUG", "CONFIG_USB_DWC2_VERBOSE", "CONFIG_USB_DWCOTG",
    "CONFIG_USB_MON", "CONFIG_DYNAMIC_DEBUG", "CONFIG_FUNCTION_TRACER",
    "CONFIG_FUNCTION_GRAPH_TRACER", "CONFIG_KPROBES", "CONFIG_KPROBE_EVENTS",
    "CONFIG_DEBUG_FS", "CONFIG_HZ", "CONFIG_HIST_TRIGGERS", "CONFIG_USB_ZERO",
    "CONFIG_USB_TEST",
]

# GHWCFG2.OtgArch, drivers/usb/dwc2/hw.h:258-260
ARCH_NAMES = {0: "slave_only", 1: "external_dma", 2: "internal_dma"}

# Operations no backend can provide on this hardware/software stack
# (docs/kernel-integration-options.md).
ALWAYS_UNSUPPORTED = [
    {"capability": "control_phase_control",
     "reason": "phases are sequenced inside dwc2 (hcd_intr.c dwc2_hc_xfercomp_intr); "
               "no existing interface exposes them"},
    {"capability": "token_or_pid_control",
     "reason": "the DWC2 core generates tokens, PIDs within a run, and CRC"},
    {"capability": "per_packet_timing_observation",
     "reason": "hardware-internal in buffer-DMA mode; needs a bus analyzer (FS/LS)"},
]


def _p(root: str, path: str) -> str:
    return os.path.join(root, path.lstrip("/"))


def _read_abs(path: str) -> Optional[str]:
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8", "replace")
    except OSError:
        return None


def _read(root: str, path: str) -> Optional[str]:
    return _read_abs(_p(root, path))


def parse_dwc2_params(text: str) -> Dict[str, Any]:
    """Parse dwc2 debugfs params/hw_params ("%-30s: %d" or "0x%x" lines)."""
    out: Dict[str, Any] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        if not key:
            continue
        try:
            out[key] = int(val, 16) if val.lower().startswith("0x") else int(val)
        except ValueError:
            out[key] = val
    return out


def core_revision(snpsid: Optional[int]) -> Optional[str]:
    """0x4f54280a -> "2.80a" (DWC2_CORE_REV_* encoding, core.h)."""
    if snpsid is None:
        return None
    low = snpsid & 0xFFFF
    digits = f"{low:04x}"
    return f"{digits[0]}.{digits[1:3]}{digits[3]}"


def parse_kconfig(text: str, keys: List[str]) -> Dict[str, Optional[str]]:
    found: Dict[str, Optional[str]] = {k: None for k in keys}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("# CONFIG_") and line.endswith(" is not set"):
            k = line[2:-len(" is not set")]
            if k in found:
                found[k] = "n"
        elif line.startswith("CONFIG_") and "=" in line:
            k, _, v = line.partition("=")
            if k in found:
                found[k] = v.strip('"')
    return found


def _bound_devices(root: str, driver: str) -> List[str]:
    d = _p(root, f"/sys/bus/platform/drivers/{driver}")
    if not os.path.isdir(d):
        return []
    skip = {"bind", "unbind", "uevent", "module", "new_id", "remove_id"}
    return sorted(e for e in os.listdir(d) if e not in skip)


def probe_controller(root: str, unknown: Dict[str, str]) -> Dict[str, Any]:
    ctl: Dict[str, Any] = {"driver": None, "device": None, "compatible": None}

    nodes = sorted(glob.glob(_p(root, "/proc/device-tree/soc/usb@*")))
    if nodes:
        compat = _read_abs(os.path.join(nodes[0], "compatible"))
        if compat is not None:
            ctl["compatible"] = [c for c in compat.split("\0") if c]
    else:
        unknown["controller.compatible"] = "no /proc/device-tree/soc/usb@* node"

    for drv in ("dwc2", "dwc_otg"):
        devs = _bound_devices(root, drv)
        if devs:
            ctl["driver"], ctl["device"] = drv, devs[0]
            break
    if ctl["driver"] is None:
        unknown["controller.driver"] = "neither dwc2 nor dwc_otg is bound"
        return ctl

    if ctl["driver"] == "dwc_otg":
        ctl["note"] = ("downstream dwc_otg driver (Raspberry Pi OS default); dwc2 debugfs "
                       "and dwc2 kprobes are unavailable. Use dtoverlay=dwc2,dr_mode=host.")
        return ctl

    dbg = f"/sys/kernel/debug/usb/{ctl['device']}"
    params_text = _read(root, f"{dbg}/params")
    hw_text = _read(root, f"{dbg}/hw_params")
    ctl["dr_mode"] = (_read(root, f"{dbg}/dr_mode") or "").strip() or None
    if params_text is None or hw_text is None:
        unknown["controller.params"] = (f"{dbg}/params not readable "
                                        "(debugfs not mounted, or not root)")
        return ctl

    p = parse_dwc2_params(params_text)
    hw = parse_dwc2_params(hw_text)
    ctl["params"] = p
    ctl["hw_params"] = hw
    host_dma = p.get("host_dma")
    ddma = p.get("dma_desc_enable")
    if host_dma is None:
        mode = None
    elif not host_dma:
        mode = "slave"
    else:
        mode = "descriptor_dma" if ddma else "buffer_dma"
    ctl["summary"] = {
        "core_revision": core_revision(hw.get("snpsid")),
        "arch": ARCH_NAMES.get(hw.get("arch"), None),
        "host_channels": p.get("host_channels"),
        "hw_host_channels": hw.get("host_channels"),
        "host_dma_mode": mode,
        "max_transfer_size": p.get("max_transfer_size"),
        "max_packet_count": p.get("max_packet_count"),
        "uframe_sched": p.get("uframe_sched"),
    }
    return ctl


def read_kernel_config(root: str):
    """Return (text, source path, reason-if-missing). /proc/config.gz is the
    running kernel's own copy; /boot/config-RELEASE is what Raspberry Pi OS
    installs with the kernel package (the 'configs' module is not loaded by
    default)."""
    gz = _p(root, "/proc/config.gz")
    if os.path.exists(gz):
        try:
            with gzip.open(gz, "rt") as f:
                return f.read(), "/proc/config.gz", None
        except OSError as e:
            return None, None, f"/proc/config.gz unreadable: {e}"
    release = (_read(root, "/proc/sys/kernel/osrelease") or "").strip()
    if release:
        path = f"/boot/config-{release}"
        text = _read(root, path)
        if text is not None:
            return text, path, None
    return None, None, "neither /proc/config.gz nor /boot/config-RELEASE (modprobe configs)"


def probe_facilities(root: str, unknown: Dict[str, str]) -> Dict[str, Any]:
    fac: Dict[str, Any] = {}

    usbmon_devs = sorted(os.path.basename(p) for p in glob.glob(_p(root, "/dev/usbmon*")))
    fac["usbmon"] = {
        "module_loaded": os.path.isdir(_p(root, "/sys/module/usbmon")),
        "devices": usbmon_devs,
    }

    tfs = find_tracefs(root)
    if tfs is None:
        fac["ftrace"] = None
        unknown["facilities.ftrace"] = "tracefs not found (not mounted, or not root)"
    else:
        funcs = read_filter_functions(tfs)
        usable, missing = usable_probes(funcs)
        clocks = _read_abs(os.path.join(tfs, "trace_clock")) or ""
        fac["ftrace"] = {
            "path": "/" + os.path.relpath(tfs, root),
            "available_filter_functions": funcs is not None,
            "kprobe_events": os.path.exists(os.path.join(tfs, "kprobe_events")),
            "trace_clocks": [c.strip("[]") for c in clocks.split()],
            "mono_clock": any(c.strip("[]") == "mono" for c in clocks.split()),
            "hostlab_probes_usable": [p.name for p in usable],
            "hostlab_probes_missing": [{"name": p.name, "symbol": p.symbol} for p in missing],
        }

    ddebug = None
    for cand in ("/proc/dynamic_debug/control", "/sys/kernel/debug/dynamic_debug/control"):
        if os.path.exists(_p(root, cand)):
            ddebug = cand
    fac["dynamic_debug"] = ddebug

    text, source, why = read_kernel_config(root)
    fac["kernel_config_source"] = source
    fac["kernel_config"] = parse_kconfig(text, CONFIG_KEYS) if text is not None else None
    if text is None:
        unknown["facilities.kernel_config"] = why
    return fac


def probe(root: str = "/") -> Dict[str, Any]:
    unknown: Dict[str, str] = {}
    model = _read(root, "/proc/device-tree/model")
    release = _read(root, "/proc/sys/kernel/osrelease")
    result: Dict[str, Any] = {
        "tool": "hostlab-probe",
        "version": __version__,
        "schema": TRACE_SCHEMA,
        "root": root,
        "euid": os.geteuid() if root == "/" else None,
        "model": model.rstrip("\0\n") if model else None,
        "kernel_release": release.strip() if release else None,
        "controller": probe_controller(root, unknown),
        "facilities": probe_facilities(root, unknown),
    }
    unsupported = list(ALWAYS_UNSUPPORTED)
    fac = result["facilities"]
    if not fac.get("dynamic_debug"):
        unsupported.append({"capability": "dwc2_dev_dbg",
                            "reason": "no dynamic debug control; dev_dbg compiled out "
                                      "unless CONFIG_USB_DWC2_DEBUG"})
    if result["controller"].get("driver") != "dwc2":
        unsupported.append({"capability": "dwc2_channel_tracing",
                            "reason": "dwc2 is not the bound driver"})
    if fac.get("ftrace") and not fac["ftrace"]["kprobe_events"]:
        unsupported.append({"capability": "kprobe_events",
                            "reason": "kprobe_events missing (CONFIG_KPROBE_EVENTS)"})
    result["unsupported"] = unsupported
    result["unknown"] = unknown
    return result


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="hostlab-probe", description=__doc__.splitlines()[0])
    ap.add_argument("--root", default="/", help="filesystem root to inspect")
    ap.add_argument("--pretty", action="store_true", help="indent JSON output")
    args = ap.parse_args(argv)
    json.dump(probe(args.root), sys.stdout, indent=2 if args.pretty else None, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
