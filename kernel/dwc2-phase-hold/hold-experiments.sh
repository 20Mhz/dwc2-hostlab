#!/bin/sh
# SPDX-License-Identifier: GPL-2.0
# Run ON the Pi as root, with the patched dwc2 loaded with phase_hold=1
# (phase-hold.sh load hold). Bounded experiments on one device; the hold is
# switched off on exit, even on failure.
#
#   hold-experiments.sh BUS:DEV DEVADDR OUT_DIR OFFSETS_JSON
#
#   A  hold before DATA / before STATUS, released by the 50 ms timeout: 20 x 2
#   B  hold before DATA, explicit release after 1, 5, 20 ms: 10 each
#   C  DISCARDURB while held (5 ms after submit): before DATA / before STATUS, 50 each
#   D  usbcore request timeout (20 ms) while held (hold timeout 500 ms): 5
# STAGES (default "A B C D") selects stages.
set -eu
stages="${STAGES:-A B C D}"
has() { case " $stages " in *" $1 "*) return 0;; esac; return 1; }
dev="${1:?BUS:DEV}"; addr="${2:?DEVADDR}"; out="${3:?OUT_DIR}"; off="${4:?OFFSETS_JSON}"
cd "$(dirname "$0")/../.."
ctrl=/sys/kernel/debug/usb/20980000.usb/phase_hold
[ -e "$ctrl" ] || { echo "no $ctrl: load the patched dwc2 with phase_hold=1" >&2; exit 1; }
trap 'echo off > "$ctrl"' EXIT
mkdir -p "$out"
S="bin/hostlab-session run --profile state --offsets $off"
C=bin/hostlab-ctl
snap() { echo "$1: $(cat "$ctrl")" | tee -a "$out/phase_hold.log"; }

snap start
has A && for mask in data status; do
    echo "dev=$addr mask=$mask timeout_ms=50" > "$ctrl"
    $S --out "$out/A-$mask" -- $C get-descriptor "$dev" --repeat 20 --timeout 2000 || true
    snap "A-$mask"
done
has B && echo "dev=$addr mask=data timeout_ms=1000" > "$ctrl"
has B && for d in 1 5 20; do
    $S --out "$out/B-$d" -- python3 kernel/dwc2-phase-hold/hold_release.py \
        --ctl $C --device "$dev" --delay-ms $d --count 10 --out "$out/B-$d.release.jsonl" || true
    snap "B-$d"
done
has C && for mask in data status; do
    echo "dev=$addr mask=$mask timeout_ms=100" > "$ctrl"
    $S --out "$out/C-$mask" -- $C cancel-sweep "$dev" --delays-us 5000:5000:1 --repeat 50 || true
    snap "C-$mask"
done
if has D; then
    echo "dev=$addr mask=data timeout_ms=500" > "$ctrl"
    $S --out "$out/D" -- $C get-descriptor "$dev" --repeat 5 --timeout 20 || true
    snap D
fi
echo off > "$ctrl"
$C get-descriptor "$dev" > "$out/after.jsonl" && grep -q '"status":"ok"' "$out/after.jsonl" \
    && echo "device answers with the hold off" | tee -a "$out/phase_hold.log"
chown -R "$(stat -c %U .)" "$out"
