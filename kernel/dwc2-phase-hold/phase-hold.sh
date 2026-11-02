#!/bin/sh
# SPDX-License-Identifier: GPL-2.0
# Run ON the Pi as root. Build, load and unload the experimental dwc2 with
# the control-phase hold. Nothing is installed: the module is loaded with
# insmod from the build directory, so a reboot always returns to the stock
# driver.
#
#   phase-hold.sh build DWC2_SRC_DIR   copy pristine dwc2 sources (359f37f), patch, build
#   phase-hold.sh load [hold]          replace the stock dwc2 with the build;
#                                       "hold" sets phase_hold=1 (default: 0, i.e. off)
#   phase-hold.sh stock                unload the build, load the stock dwc2
#   phase-hold.sh status
#
# USB is unavailable for a moment while the driver is swapped; SSH over Wi-Fi
# (SDIO) does not depend on it.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
build=/tmp/dwc2-phase-hold
kdir="/lib/modules/$(uname -r)/build"

case "${1:-status}" in
build)
    src="${2:?DWC2_SRC_DIR with pristine drivers/usb/dwc2 sources}"
    # Patch a fresh copy, then update only files whose content changed so an
    # existing build tree recompiles just those (the full build takes ~5 min).
    fresh=$(mktemp -d)
    cp "$src"/*.c "$src"/*.h "$src"/Kconfig "$fresh"/
    (cd "$fresh" && patch -p4 --quiet < "$here/0001-usb-dwc2-experimental-control-phase-hold.patch")
    cp "$here/Kbuild" "$fresh/Kbuild"
    mkdir -p "$build"
    for f in "$fresh"/*; do
        cmp -s "$f" "$build/${f##*/}" || cp "$f" "$build/"
    done
    rm -rf "$fresh"
    make -s -C "$kdir" M="$build" modules
    ls -l "$build/dwc2.ko"
    ;;
load)
    [ -f "$build/dwc2.ko" ] || { echo "build first" >&2; exit 1; }
    param=0; [ "${2:-}" = hold ] && param=1
    modprobe -r dwc2
    # insmod does not resolve dependencies; the stock dwc2 needs "roles"
    # (modules.dep), which modprobe -r unloads with it.
    modprobe roles
    insmod "$build/dwc2.ko" phase_hold=$param || { echo "insmod failed; restoring stock dwc2" >&2; modprobe dwc2; exit 1; }
    sleep 3
    echo "loaded $build/dwc2.ko phase_hold=$param"
    ;;
stock)
    rmmod dwc2 2>/dev/null || true
    modprobe dwc2
    sleep 3
    echo "stock dwc2 loaded"
    ;;
status)
    echo "phase_hold param: $(cat /sys/module/dwc2/parameters/phase_hold 2>/dev/null || echo 'absent (stock driver)')"
    f=/sys/kernel/debug/usb/20980000.usb/phase_hold
    [ -e "$f" ] && echo "phase_hold: $(cat $f)" || echo "phase_hold debugfs file: absent"
    echo "taint: $(cat /proc/sys/kernel/tainted)"
    ;;
*)
    echo "usage: $0 build DIR | load [hold] | stock | status" >&2; exit 2 ;;
esac
