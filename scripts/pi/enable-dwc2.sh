#!/bin/sh
# Run ON the Pi. Switch the USB controller from dwc_otg (Raspberry Pi OS
# default) to dwc2 in host mode, or revert.
#
#   sudo scripts/pi/enable-dwc2.sh            show what would change
#   sudo scripts/pi/enable-dwc2.sh --apply    back up config.txt, add the overlay
#   sudo scripts/pi/enable-dwc2.sh --revert   restore the newest backup
#
# A reboot is needed either way. If the Pi does not come back, see
# docs/environment.md "Recovery": the change is one line in config.txt on the
# SD card's boot partition, editable from any computer.
set -eu

LINE="dtoverlay=dwc2,dr_mode=host"
for c in /boot/firmware/config.txt /boot/config.txt; do
    [ -f "$c" ] && { CFG=$c; break; }
done
[ -n "${CFG:-}" ] || { echo "config.txt not found" >&2; exit 1; }

case "${1:-}" in
--apply)
    if grep -q "^dtoverlay=dwc2" "$CFG"; then
        echo "$CFG already has: $(grep '^dtoverlay=dwc2' "$CFG")"; exit 0
    fi
    backup="$CFG.hostlab-$(date +%Y%m%d-%H%M%S)"
    cp -p "$CFG" "$backup"
    printf '\n# dwc2-hostlab: mainline dwc2 driver, host mode\n[all]\n%s\n' "$LINE" >> "$CFG"
    sync
    echo "backup: $backup"
    echo "added '$LINE' to $CFG; reboot to take effect"
    ;;
--revert)
    backup=$(ls -1t "$CFG".hostlab-* 2>/dev/null | head -1)
    [ -n "$backup" ] || { echo "no $CFG.hostlab-* backup found" >&2; exit 1; }
    cp -p "$backup" "$CFG"
    sync
    echo "restored $CFG from $backup; reboot to take effect"
    ;;
"")
    echo "config: $CFG"
    if grep -q "^dtoverlay=dwc2" "$CFG"; then
        echo "already enabled: $(grep '^dtoverlay=dwc2' "$CFG")"
    else
        echo "would append: [all] / $LINE   (run with --apply)"
    fi
    echo "bound driver now: $(ls /sys/bus/platform/drivers/dwc2 2>/dev/null | grep usb || true)" \
         "$(ls /sys/bus/platform/drivers/dwc_otg 2>/dev/null | grep usb | sed 's/^/dwc_otg:/' || true)"
    ;;
*)
    echo "usage: $0 [--apply|--revert]" >&2; exit 2 ;;
esac
