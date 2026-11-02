# SPDX-License-Identifier: MIT
"""Synthetic filesystem trees for tests. Not captured from hardware: values
follow the formats in the kernel sources cited next to each file."""
from __future__ import annotations

import gzip
import os

from hostlab.tracefs import ARM32_PROFILE, TraceFS

# dwc2 debugfs, "%-30s: %d" (drivers/usb/dwc2/debugfs.c print_param)
DWC2_PARAMS = """\
otg_caps.hnp_support          : 1
dma_desc_enable               : 0
speed                         : 0
max_transfer_size             : 65535
max_packet_count              : 511
host_channels                 : 8
uframe_sched                  : 1
ahbcfg                        : 0x10
host_dma                      : 1
"""
DWC2_HW_PARAMS = """\
op_mode                       : 0
arch                          : 2
dma_desc_enable               : 0
max_transfer_size             : 524287
host_channels                 : 8
snpsid                        : 0x4f54280a
"""
TRACE_CLOCKS = "[local] global counter uptime perf mono mono_raw boot\n"


def write(root: str, rel: str, content, mode: str = "w") -> str:
    path = os.path.join(root, rel.lstrip("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, mode) as f:
        f.write(content)
    return path


def make_tracefs(root: str, functions=None) -> str:
    if functions is None:
        functions = [p.symbol for p in ARM32_PROFILE]
    write(root, "sys/kernel/tracing/trace", "# tracer: nop\n")
    write(root, "sys/kernel/tracing/trace_clock", TRACE_CLOCKS)
    write(root, "sys/kernel/tracing/kprobe_events", "")
    write(root, "sys/kernel/tracing/tracing_on", "1\n")
    write(root, "sys/kernel/tracing/buffer_size_kb", "1408\n")
    write(root, "sys/kernel/tracing/trace_marker", "")
    write(root, "sys/kernel/tracing/current_tracer", "nop\n")
    write(root, "sys/kernel/tracing/kprobe_profile",
          "  hostlab/hl_giveback    120    2\n  other/x    5    0\n")
    write(root, "sys/kernel/tracing/set_ftrace_filter", "#### all functions enabled ####\n")
    lines = []
    for fn in functions:
        lines.append(f"{fn} [dwc2]" if fn.startswith(("dwc2", "_dwc2")) else fn)
    write(root, "sys/kernel/tracing/available_filter_functions",
          "\n".join(lines + ["schedule", "do_sys_open"]) + "\n")
    return os.path.join(root, "sys/kernel/tracing")


def make_dwc2_system(root: str, functions=None) -> None:
    write(root, "proc/device-tree/model", "Raspberry Pi Zero W Rev 1.1\0")
    write(root, "proc/sys/kernel/osrelease", "6.18.59+\n")
    write(root, "proc/device-tree/soc/usb@7e980000/compatible", "brcm,bcm2835-usb\0")
    write(root, "sys/bus/platform/drivers/dwc2/bind", "")
    write(root, "sys/bus/platform/drivers/dwc2/20980000.usb", "")
    write(root, "sys/kernel/debug/usb/20980000.usb/params", DWC2_PARAMS)
    write(root, "sys/kernel/debug/usb/20980000.usb/hw_params", DWC2_HW_PARAMS)
    write(root, "sys/kernel/debug/usb/20980000.usb/dr_mode", "host\n")
    write(root, "dev/usbmon0", "")
    os.makedirs(os.path.join(root, "sys/module/usbmon"), exist_ok=True)
    with gzip.open(os.path.join(root, "proc/config.gz"), "wt") as f:
        f.write("CONFIG_USB_DWC2=m\nCONFIG_KPROBES=y\nCONFIG_HZ=100\n"
                "# CONFIG_DYNAMIC_DEBUG is not set\n")
    make_tracefs(root, functions)


def make_dwc_otg_system(root: str) -> None:
    write(root, "proc/device-tree/model", "Raspberry Pi Zero W Rev 1.1\0")
    write(root, "proc/device-tree/soc/usb@7e980000/compatible", "brcm,bcm2708-usb\0")
    write(root, "sys/bus/platform/drivers/dwc_otg/20980000.usb", "")


class FakeTraceFS(TraceFS):
    """Models kprobe_events and events/<group>/enable like the kernel does:
    reading kprobe_events lists only currently registered probes."""

    def _write(self, name: str, value: str, append: bool = False) -> None:
        if name == "kprobe_events":
            path = self._p(name)
            with open(path) as f:
                lines = [ln for ln in f.read().splitlines() if ln]
            if value.startswith("-:"):
                target = value[2:]
                lines = [ln for ln in lines if ln.split()[0].split(":", 1)[1] != target]
                if os.path.exists(self._p(f"events/{target}")):
                    os.remove(self._p(f"events/{target}"))
            else:
                lines.append(value)
                group, ev = value.split()[0].split(":", 1)[1].split("/")
                write(self.path, f"events/{group}/{ev}", "")
                write(self.path, f"events/{group}/enable", "0\n")
            with open(path, "w") as f:
                f.write("".join(ln + "\n" for ln in lines))
            return
        if name == "trace_clock":
            clocks = [c.strip("[]") for c in self.read("trace_clock").split()]
            write(self.path, name, " ".join(f"[{c}]" if c == value else c for c in clocks) + "\n")
            return
        if name == "trace_marker":
            write(self.path, "trace",
                  f"     python3-100     [000] .....   100.000100: tracing_mark_write: {value}\n",
                  mode="a")
            return
        write(self.path, name, value + "\n", mode="a" if append else "w")
