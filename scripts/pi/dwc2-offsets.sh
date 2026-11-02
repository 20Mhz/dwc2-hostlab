#!/bin/sh
# SPDX-License-Identifier: MIT
# Run ON the Pi. Compute member offsets of dwc2 structures for the running
# kernel, for kprobe fetch arguments (scripts only BUILD an object; nothing is
# loaded into the kernel).
#
#   scripts/pi/dwc2-offsets.sh DWC2_SRC_DIR [OUT.json]
#
# DWC2_SRC_DIR holds core.h, hcd.h and hw.h from the raspberrypi/linux commit
# of the running kernel (docs/references.md); they are not part of this
# repository (GPL-2.0). Needs the matching linux-headers package, gcc and
# readelf. Offsets come from DWARF (DW_AT_data_member_location and, for
# bitfields, DW_AT_data_bit_offset), i.e. exactly what this compiler lays out.
set -eu

src="${1:?usage: dwc2-offsets.sh DWC2_SRC_DIR [OUT.json]}"
out="${2:-}"
kdir="/lib/modules/$(uname -r)/build"
[ -d "$kdir" ] || { echo "missing $kdir (install linux-headers-$(uname -r))" >&2; exit 1; }
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
cp "$src"/core.h "$src"/hcd.h "$src"/hw.h "$work"/
cat > "$work/hostlab_offsets.c" <<'EOF'
#include <linux/module.h>
#include "core.h"
#include "hcd.h"
/* Instances force full type information into the DWARF. */
struct dwc2_host_chan hostlab_chan;
struct dwc2_qtd hostlab_qtd;
MODULE_LICENSE("GPL");
EOF
# DWARF 5 states bitfield positions as DW_AT_data_bit_offset (bits from the
# start of the struct); older versions use the MSB-relative DW_AT_bit_offset,
# which is converted below if it appears.
printf 'obj-m := hostlab_offsets.o\nccflags-y := -g -gdwarf-5\n' > "$work/Kbuild"
make -s -C "$kdir" M="$work" hostlab_offsets.o >/dev/null
python3 - "$work/hostlab_offsets.o" "$(uname -r)" "$out" <<'PY'
import json, re, subprocess, sys
obj, release, out = sys.argv[1], sys.argv[2], sys.argv[3]
dump = subprocess.run(["readelf", "--debug-dump=info", obj], capture_output=True,
                      text=True, check=True).stdout
want = {"dwc2_host_chan": {"hc_num", "dev_addr", "ep_num", "ep_is_in", "ep_type",
                           "max_packet", "data_pid_start", "xfer_len", "xfer_count",
                           "halt_status", "hcint", "qh"},
        "dwc2_qtd": {"control_phase", "in_process", "data_toggle", "urb", "qh"}}
res, cur, member = {}, None, None
for line in dump.splitlines():
    m = re.search(r"<(\d+)><[0-9a-f]+>: Abbrev Number: \d+ \((DW_TAG_\w+)\)", line)
    if m:
        depth, tag = int(m.group(1)), m.group(2)
        if depth == 1:
            cur = None
        member = None
        if tag == "DW_TAG_structure_type" and depth == 1:
            cur = "pending"
        elif tag == "DW_TAG_member" and isinstance(cur, str) and cur in want:
            member = {}
        continue
    n = re.search(r"DW_AT_name\s*:.*?([\w]+)\s*$", line)
    if n and cur == "pending":
        cur = n.group(1)
        continue
    if member is None or cur not in want:
        continue
    for attr in ("name", "data_member_location", "data_bit_offset", "bit_offset", "bit_size",
                 "byte_size"):
        a = re.search(rf"DW_AT_{attr}\s*:\s*(?:\(indirect string, offset: 0x[0-9a-f]+\):\s*)?(\S+)\s*$", line)
        if a:
            member[attr] = a.group(1) if attr == "name" else int(a.group(1), 0)
            if attr == "name" and a.group(1) in want[cur]:
                res.setdefault(cur, {})[a.group(1)] = member
for m in res.values():
    for v in m.values():
        if "bit_offset" in v:  # DWARF 2-4, little-endian: count from the LSB
            v["data_bit_offset"] = (v.get("data_member_location", 0) * 8
                                    + v["byte_size"] * 8 - v.pop("bit_offset") - v["bit_size"])
        if "data_bit_offset" in v:
            v.pop("data_member_location", None)
            v.pop("byte_size", None)
res = {s: {k: {kk: vv for kk, vv in v.items() if kk != "name"} for k, v in m.items()}
       for s, m in res.items()}
missing = {s: sorted(want[s] - set(res.get(s, {}))) for s in want}
doc = {"kernel_release": release, "source": "DWARF of a compiled object", "structs": res,
       "missing": {s: m for s, m in missing.items() if m}}
text = json.dumps(doc, indent=2, sort_keys=True)
if out:
    open(out, "w").write(text + "\n")
print(text)
PY
