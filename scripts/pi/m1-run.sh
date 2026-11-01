#!/bin/sh
# Run ON the Pi as root: the M1 hardware session and its evidence bundle.
#
#   sudo scripts/pi/m1-run.sh BUS:DEV [OUT]      (OUT default: evidence/m1-DATE)
#
# BUS:DEV is the test device from `bin/hostlab-ctl list` (not a hub). The
# runs only issue GET_DESCRIPTOR(DEVICE) requests, the default hostlab-ctl
# policy. Recovery snapshots are picked up when present:
#   runs/00-dwc_otg/preflight.json   before the overlay        (state dwc_otg)
#   runs/05-reverted/preflight.json  after --revert + reboot   (state reverted)
#   runs/06-reapplied/preflight.json after --apply + reboot    (state reapplied)
set -eu

dev="${1:?usage: m1-run.sh BUS:DEV [OUT]}"
cd "$(dirname "$0")/../.."
stamp=$(date +%Y%m%d-%H%M%S)
out="${2:-evidence/m1-$stamp}"
runs="runs/m1-$stamp"
reps=100

scripts/pi/prepare-session.sh >/dev/null
mkdir -p "$runs"
bin/hostlab-preflight --json "$runs/preflight.json" | tee "$runs/preflight.txt"

# A failing command is evidence too (session.json keeps its exit code), so
# carry on to the bundle instead of stopping here.
bin/hostlab-session run --out "$runs/device" -- \
    bin/hostlab-ctl get-descriptor "$dev" --repeat $reps || echo "device session exit $?"
bin/hostlab-session run --no-ftrace --out "$runs/device-noprobe" -- \
    bin/hostlab-ctl get-descriptor "$dev" --repeat $reps || echo "noprobe session exit $?"

set -- --out "$out" --preflight "$runs/preflight.json" \
    --session "device=$runs/device" --session "device-noprobe=$runs/device-noprobe"
for s in 00-dwc_otg:dwc_otg 05-reverted:reverted 06-reapplied:reapplied; do
    f="runs/${s%%:*}/preflight.json"
    [ -f "$f" ] && set -- "$@" --state "${s#*:}=$f"
done
bin/hostlab-evidence "$@"
bin/hostlab-analyze "$runs/device" | head -20
owner=$(stat -c %U .)
chown "$owner" runs "$(dirname "$out")"
chown -R "$owner" "$runs" "$out"
echo "summary: $out/test-summary.md"
