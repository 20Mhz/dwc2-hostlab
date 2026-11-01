#!/bin/sh
# Run ON the Pi. Switch the USB controller from dwc_otg (Raspberry Pi OS
# default) to dwc2 in host mode, or revert.
#
#   sudo scripts/pi/enable-dwc2.sh            show the current state, change nothing
#   sudo scripts/pi/enable-dwc2.sh --apply    back up config.txt, append the hostlab block
#   sudo scripts/pi/enable-dwc2.sh --revert   remove the hostlab block (backups are kept)
#
# A reboot is needed either way. If the Pi does not come back, see
# docs/environment.md "Recovery": the change is three lines at the end of
# config.txt on the SD card's FAT boot partition, editable from any computer.
#
# config.txt is split into sections by conditional filters ([all], [pi0w],
# [cm5], ...). Stock Raspberry Pi OS (trixie) already contains
# "dtoverlay=dwc2,dr_mode=host" under [cm5], which does not apply to a Zero W,
# so only lines in sections that apply to this board count as "enabled".
# HOSTLAB_CONFIG_TXT overrides the file (tests).
set -eu

LINE="dtoverlay=dwc2,dr_mode=host"
MARK="# dwc2-hostlab: mainline dwc2 driver, host mode (remove these 3 lines to revert)"

CFG="${HOSTLAB_CONFIG_TXT:-}"
if [ -z "$CFG" ]; then
    for c in /boot/firmware/config.txt /boot/config.txt; do
        [ -f "$c" ] && { CFG=$c; break; }
    done
fi
[ -n "$CFG" ] && [ -f "$CFG" ] || { echo "config.txt not found" >&2; exit 1; }

# Print "LINENO<TAB>SECTION<TAB>TEXT" for every dtoverlay=dwc2 line, with
# SECTION "applies" when no filter, [all], [pi0] or [pi0w] is in force, else
# the filter text. Filters other than these are treated as not applying,
# which is correct for a Zero W except for [gpio*]/[EDID=*]/serial filters
# (not used by hostlab; reported so they can be checked by hand).
dwc2_lines() {
    awk '
        /^[ \t]*\[[^]]*\][ \t]*$/ {
            f = $0; gsub(/[ \t\[\]]/, "", f)
            sect = (f == "all" || f == "pi0" || f == "pi0w") ? "applies" : "[" f "]"
            next
        }
        /^[ \t]*dtoverlay[ \t]*=[ \t]*dwc2([ \t,]|$)/ {
            print NR "\t" (sect == "" ? "applies" : sect) "\t" $0
        }' "$CFG"
}

effective() { dwc2_lines | awk -F'\t' '$2 == "applies"'; }

bound() {
    for d in dwc2 dwc_otg; do
        for e in /sys/bus/platform/drivers/$d/*usb*; do
            [ -e "$e" ] && printf '%s:%s ' "$d" "${e##*/}"
        done
    done
    echo
}

case "${1:-}" in
--apply)
    if grep -qxF "$MARK" "$CFG"; then
        echo "$CFG already has the hostlab block"; exit 0
    fi
    if [ -n "$(effective)" ]; then
        echo "$CFG already enables dwc2 for this board (not touched):"; effective; exit 0
    fi
    backup="$CFG.hostlab-$(date +%Y%m%d-%H%M%S)"
    cp -p "$CFG" "$backup"
    # Start the block on its own line.
    [ -z "$(tail -c 1 "$CFG")" ] || printf '\n' >> "$CFG"
    printf '%s\n[all]\n%s\n' "$MARK" "$LINE" >> "$CFG"
    sync
    echo "backup: $backup"
    echo "appended to $CFG:"; tail -n 3 "$CFG" | sed 's/^/  /'
    echo "reboot to take effect"
    ;;
--revert)
    grep -qxF "$MARK" "$CFG" || { echo "no hostlab block in $CFG; nothing to revert"; exit 0; }
    tmp="$CFG.hostlab-tmp"
    # Drop the marker and the two lines that follow it, only if they are ours.
    awk -v mark="$MARK" -v line="$LINE" '
        { l[NR] = $0 }
        END {
            for (i = 1; i <= NR; i++) {
                if (l[i] == mark && l[i+1] == "[all]" && l[i+2] == line) { i += 2; continue }
                print l[i]
            }
        }' "$CFG" > "$tmp"
    if grep -qxF "$MARK" "$tmp"; then
        rm -f "$tmp"; echo "hostlab block in $CFG was edited; revert by hand" >&2; exit 1
    fi
    cp -p "$CFG" "$CFG.hostlab-prerevert-$(date +%Y%m%d-%H%M%S)"
    cat "$tmp" > "$CFG" && rm -f "$tmp"
    sync
    echo "removed the hostlab block from $CFG; reboot to take effect"
    ;;
"")
    echo "config: $CFG"
    echo "dtoverlay=dwc2 lines:"
    dwc2_lines | awk -F'\t' '{ printf "  line %s %-10s %s\n", $1, $2, $3 }'
    if grep -qxF "$MARK" "$CFG"; then
        echo "state: hostlab block present (revert with --revert)"
    elif [ -n "$(effective)" ]; then
        echo "state: enabled by a line hostlab did not add"
    else
        echo "state: not enabled for this board; --apply would append:"
        printf '  %s\n  [all]\n  %s\n' "$MARK" "$LINE"
    fi
    echo "bound now: $(bound)"
    ;;
*)
    echo "usage: $0 [--apply|--revert]" >&2; exit 2 ;;
esac
