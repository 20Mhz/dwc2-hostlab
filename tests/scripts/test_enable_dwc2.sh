#!/bin/sh
# SPDX-License-Identifier: MIT
# Tests for scripts/pi/enable-dwc2.sh against a copy of the stock Raspberry Pi
# OS (trixie) config.txt layout, which already has the dwc2 overlay under [cm5].
set -u
here=$(cd "$(dirname "$0")" && pwd)
script="$here/../../scripts/pi/enable-dwc2.sh"
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
export HOSTLAB_CONFIG_TXT="$tmp/config.txt"
fail=0
check() { if eval "$2"; then echo "enable-dwc2: $1 ok"; else echo "enable-dwc2: $1 FAILED"; fail=1; fi; }

cat > "$tmp/config.txt" <<'CFG'
dtparam=audio=on
arm_boost=1

[cm4]
otg_mode=1

[cm5]
dtoverlay=dwc2,dr_mode=host

[all]
enable_uart=1
CFG
cp "$tmp/config.txt" "$tmp/orig"

out=$(sh "$script")
check "[cm5] line is not counted as enabled" 'echo "$out" | grep -q "state: not enabled"'
check "status changes nothing" 'cmp -s "$tmp/config.txt" "$tmp/orig"'

sh "$script" --apply >/dev/null
check "apply appends the block" '[ "$(tail -n 2 "$tmp/config.txt")" = "$(printf "[all]\ndtoverlay=dwc2,dr_mode=host")" ]'
check "apply keeps a backup" '[ "$(ls "$tmp"/config.txt.hostlab-2* | wc -l)" -eq 1 ] && cmp -s "$tmp"/config.txt.hostlab-2* "$tmp/orig"'
check "status sees the block" 'sh "$script" | grep -q "hostlab block present"'
sh "$script" --apply >/dev/null
check "apply is idempotent" '[ "$(grep -c "^# dwc2-hostlab" "$tmp/config.txt")" -eq 1 ]'

echo "dtparam=i2c_arm=on" >> "$tmp/config.txt"   # an edit made after --apply
sh "$script" --revert >/dev/null
check "revert removes only the block" '[ "$(diff "$tmp/orig" "$tmp/config.txt" | grep "^>" | sed "s/^> //")" = "dtparam=i2c_arm=on" ]'
check "revert again is a no-op" 'sh "$script" --revert | grep -q "nothing to revert"'

printf 'dtparam=audio=on' > "$tmp/config.txt"   # no trailing newline
sh "$script" --apply >/dev/null; sh "$script" --revert >/dev/null
check "round trip on a file without final newline" '[ "$(cat "$tmp/config.txt")" = "dtparam=audio=on" ]'

printf '[pi0w]\ndtoverlay=dwc2\n' > "$tmp/config.txt"
check "[pi0w] line counts as enabled" 'sh "$script" | grep -q "enabled by a line hostlab did not add"'
check "apply leaves an existing setting alone" 'sh "$script" --apply | grep -q "not touched"'
exit $fail
