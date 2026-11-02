#!/bin/sh
# SPDX-License-Identifier: MIT
# Run ON the Pi as root: bounded M3 experiments against one test device.
#
#   sudo scripts/pi/m3-run.sh STAGE BUS:DEV OUT_DIR
#
# STAGE:
#   e3   cancel sweep 0..300 us step 5, 5 repeats, three times: profile
#        "state" (needs OFFSETS=file from scripts/pi/dwc2-offsets.sh), the
#        default "fast" profile, and without ftrace
#   e6   5 resets, then 5 resets with 4 GET_DESCRIPTOR URBs pending
#   h5   e6's 5 plain resets with usbcore old_scheme_first=1 (runtime module
#        parameter, restored on exit even on failure)
#
# Only standard GET_DESCRIPTOR(DEVICE) requests and USBDEVFS_RESET are sent.
# Every reset re-enumerates the device; after each stage the script checks it
# still answers GET_DESCRIPTOR.
set -eu

stage="${1:?usage: m3-run.sh e3|e6|h5 BUS:DEV OUT_DIR}"
dev="${2:?BUS:DEV}"
out="${3:?OUT_DIR}"
cd "$(dirname "$0")/../.."
scripts/pi/prepare-session.sh >/dev/null
mkdir -p "$out"
S="bin/hostlab-session run"
C="bin/hostlab-ctl"

check_device() {
    $C get-descriptor "$dev" > "$out/$1-device-check.jsonl" \
        && grep -q '"status":"ok"' "$out/$1-device-check.jsonl" \
        && echo "device $dev answers after $1" || echo "device $dev NOT answering after $1"
}

case "$stage" in
e3)
    : "${OFFSETS:?set OFFSETS to the dwc2-offsets.sh JSON for this kernel}"
    sweep="$C cancel-sweep $dev --delays-us 0:300:5 --repeat 5"
    $S --profile state --offsets "$OFFSETS" --out "$out/e3-state" -- $sweep || true
    $S --profile fast --out "$out/e3-fast" -- $sweep || true
    $S --no-ftrace --out "$out/e3-noprobe" -- $sweep || true
    for r in e3-state e3-fast e3-noprobe; do
        bin/hostlab-report cancel "$out/$r" --json "$out/$r/cancel.json"
    done
    check_device e3
    ;;
e6)
    $S --profile channel --out "$out/e6-reset" -- \
        $C reset "$dev" --allow-reset --repeat 5 || true
    $S --profile channel --out "$out/e6-pending" -- \
        $C reset "$dev" --allow-reset --repeat 5 --pending 4 || true
    bin/hostlab-report reset "$out/e6-reset" --json "$out/e6-reset/reset.json"
    bin/hostlab-report reset "$out/e6-pending" --json "$out/e6-pending/reset.json"
    check_device e6
    ;;
h5)
    p=/sys/module/usbcore/parameters/old_scheme_first
    old=$(cat "$p")
    trap 'echo "$old" > "$p"' EXIT
    echo "old_scheme_first was $old"
    echo Y > "$p"
    $S --profile channel --out "$out/h5-old-scheme" -- \
        $C reset "$dev" --allow-reset --repeat 5 || true
    echo "$old" > "$p"
    echo "old_scheme_first restored to $(cat "$p")"
    bin/hostlab-report reset "$out/h5-old-scheme" --json "$out/h5-old-scheme/reset.json"
    check_device h5
    ;;
*)
    echo "unknown stage $stage" >&2; exit 2 ;;
esac
chown -R "$(stat -c %U .)" "$out"
