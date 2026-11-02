# SPDX-License-Identifier: MIT
"""Parse the text output of /sys/kernel/tracing/trace.

Handles kprobe events created by hostlab (``hostlab/<name>``) and
``tracing_mark_write`` markers. Timestamps are converted to integer
nanoseconds without going through floats.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, Iterator, Optional

_LINE = re.compile(
    r"^\s*(?P<task>.+?)-(?P<pid>\d+)\s+"
    r"(?:\(\s*[-\d]+\)\s+)?"                 # optional TGID column
    r"\[(?P<cpu>\d+)\]\s+"
    r"(?:(?P<flags>[A-Za-z.\d]{4,6})\s+)?"   # irq-info flags, optional
    r"(?P<sec>\d+)\.(?P<frac>\d+):\s+"
    r"(?P<event>[\w./]+):\s?(?P<body>.*)$"
)
# Function tracer: "task-pid [cpu] flags ts: func <-parent" (Documentation/trace/ftrace.rst).
_FUNC = re.compile(
    r"^\s*(?P<task>.+?)-(?P<pid>\d+)\s+"
    r"(?:\(\s*[-\d]+\)\s+)?"
    r"\[(?P<cpu>\d+)\]\s+"
    r"(?:(?P<flags>[A-Za-z.\d]{4,6})\s+)?"
    r"(?P<sec>\d+)\.(?P<frac>\d+):\s+"
    r"(?P<func>[\w.]+)\s+<-(?P<parent>\S+)\s*$"
)
_FIELD = re.compile(r"(\w+)=(\S+)")
_PROBE_SITE = re.compile(r"^\((?P<site>[^)]*)\)\s*")


@dataclass
class TraceEvent:
    ts_ns: int
    event: str
    fields: Dict[str, str] = field(default_factory=dict)
    task: str = ""
    pid: int = 0
    cpu: int = 0
    text: str = ""

    def int_field(self, name: str) -> Optional[int]:
        v = self.fields.get(name)
        if v is None:
            return None
        return int(v, 16) if v.lower().startswith("0x") else int(v)


def parse_ts(sec: str, frac: str) -> int:
    frac = (frac + "000000000")[:9]
    return int(sec) * 1_000_000_000 + int(frac)


def parse_line(line: str) -> Optional[TraceEvent]:
    if not line.strip() or line.lstrip().startswith("#"):
        return None
    m = _LINE.match(line.rstrip("\n"))
    if not m:
        f = _FUNC.match(line.rstrip("\n"))
        if not f:
            return None
        return TraceEvent(ts_ns=parse_ts(f.group("sec"), f.group("frac")),
                          event="fn:" + f.group("func"), fields={"parent": f.group("parent")},
                          task=f.group("task").strip(), pid=int(f.group("pid")),
                          cpu=int(f.group("cpu")), text="")
    body = m.group("body")
    site = _PROBE_SITE.match(body)
    if site:
        body = body[site.end():]
    return TraceEvent(
        ts_ns=parse_ts(m.group("sec"), m.group("frac")),
        event=m.group("event"),
        fields=dict(_FIELD.findall(body)),
        task=m.group("task").strip(),
        pid=int(m.group("pid")),
        cpu=int(m.group("cpu")),
        text=body,
    )


def parse(lines: Iterable[str]) -> Iterator[TraceEvent]:
    for line in lines:
        ev = parse_line(line)
        if ev is not None:
            yield ev
