#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copy hostlab to a Pi over SSH. Needs only ssh and tar on both ends (rsync is
# not part of Raspberry Pi OS Lite).
#
#   scripts/deploy.sh [--dry-run] HOST [DEST]     (DEST default: ~/dwc2-hostlab)
#
# Build first: make armv6
set -euo pipefail

dry=0
if [ "${1:-}" = "--dry-run" ]; then dry=1; shift; fi
host="${1:?usage: deploy.sh [--dry-run] HOST [DEST]}"
dest="${2:-dwc2-hostlab}"
repo_root="$(cd "$(dirname "$0")/.." && pwd)"

for f in hostlab-ctl hostlab-mon; do
    [ -x "$repo_root/build/armv6/$f" ] || { echo "missing build/armv6/$f; run make armv6" >&2; exit 1; }
    # A stale binary once ran an experiment with an old option set.
    newer=$(find "$repo_root/src/lib" "$repo_root/include" "$repo_root/src/tools/$f.c" \
                 -newer "$repo_root/build/armv6/$f" -type f | head -1)
    [ -z "$newer" ] || { echo "build/armv6/$f is older than $newer; run make armv6" >&2; exit 1; }
done

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
mkdir -p "$stage/bin" "$stage/python/hostlab" "$stage/scripts/pi"
cp "$repo_root"/build/armv6/hostlab-{ctl,mon} "$stage/bin/"
cp "$repo_root"/bin/hostlab-* "$stage/bin/"
cp "$repo_root"/python/hostlab/*.py "$stage/python/hostlab/"
cp "$repo_root"/scripts/pi/*.sh "$stage/scripts/pi/"
# Commit plus "-dirty" when the working tree has uncommitted changes, so
# evidence collected on the Pi names exactly what was deployed.
git -C "$repo_root" describe --always --dirty --abbrev=40 > "$stage/REVISION" 2>/dev/null \
    || echo unknown > "$stage/REVISION"

if [ "$dry" = 1 ]; then
    echo "would copy to $host:$dest:"
    (cd "$stage" && find . -type f | sort)
    exit 0
fi
# COPYFILE_DISABLE / --no-xattrs: keep macOS metadata out of the archive
# (bsdtar and GNU tar >= 1.27 both accept --no-xattrs).
COPYFILE_DISABLE=1 tar --no-xattrs -C "$stage" -cf - . | ssh "$host" "mkdir -p '$dest' && tar -C '$dest' -xf -"
ssh "$host" "'$dest'/bin/hostlab-ctl --help >/dev/null && echo 'deployed $(cat "$stage/REVISION") to $host:$dest'"
