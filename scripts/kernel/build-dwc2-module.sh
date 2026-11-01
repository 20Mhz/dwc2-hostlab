#!/usr/bin/env bash
# Build the Raspberry Pi kernel's dwc2 module for the Pi Zero W (bcmrpi_defconfig)
# and extract evidence about it: the resolved .config, the module, its symbol
# table and the list of functions that carry an ftrace (mcount) call site.
#
# Runs entirely inside Docker. The kernel source lives in a named Docker volume
# because the Linux tree contains paths that collide on case-insensitive APFS.
#
# Usage: scripts/kernel/build-dwc2-module.sh [rpi-linux-ref]
#   rpi-linux-ref defaults to the commit recorded in docs/references.md.
set -euo pipefail

REF="${1:-${RPI_LINUX_REF:-b3495ea401d709af0701a85babcba5e3d788a602}}"
BRANCH="${RPI_LINUX_BRANCH:-rpi-6.18.y}"
IMAGE="${HOSTLAB_IMAGE:-dwc2-hostlab-build}"
VOLUME="${HOSTLAB_KSRC_VOLUME:-dwc2-hostlab-ksrc}"
JOBS="${JOBS:-6}"

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
out="$repo_root/build/kernel"
mkdir -p "$out"

docker build -q -t "$IMAGE" "$repo_root/docker" >/dev/null
docker volume create "$VOLUME" >/dev/null

docker run --rm \
    -v "$VOLUME:/src" -v "$out:/out" \
    -e REF="$REF" -e BRANCH="$BRANCH" -e JOBS="$JOBS" \
    "$IMAGE" bash -euo pipefail -c '
cd /src
if [ ! -d linux/.git ]; then
    git clone --quiet --depth 1 --branch "$BRANCH" \
        https://github.com/raspberrypi/linux.git linux
fi
cd linux
if [ "$(git rev-parse HEAD)" != "$REF" ]; then
    git fetch --quiet --depth 1 origin "$REF"
    git checkout --quiet "$REF"
fi
export ARCH=arm CROSS_COMPILE=arm-linux-gnueabihf-
# No full vmlinux build, so Module.symvers is absent; unresolved symbols are
# expected and harmless for symbol/ftrace analysis.
export KBUILD_MODPOST_WARN=1
make -s bcmrpi_defconfig
make -s -j"$JOBS" modules_prepare
make -s -j"$JOBS" M=drivers/usb/dwc2 modules
make -s -j"$JOBS" M=drivers/usb/mon modules
# usbcore is built in (CONFIG_USB=y); compiling its objects is enough to see
# which of its functions survive inlining.
make -s -j"$JOBS" drivers/usb/core/

cp .config /out/bcmrpi_defconfig.resolved
cp drivers/usb/dwc2/dwc2.ko /out/
cp drivers/usb/mon/usbmon.ko /out/ 2>/dev/null || true
git rev-parse HEAD > /out/rpi-linux.commit
make -s kernelrelease > /out/kernelrelease
arm-linux-gnueabihf-gcc --version | head -1 > /out/compiler

# A function is ftrace-able when its body contains a call to the mcount hook.
mcount_functions() {
    arm-linux-gnueabihf-objdump -dr "$@" \
      | awk "/^[0-9a-f]+ <[^>]+>:\$/ { fn=\$2; gsub(/[<>:]/, \"\", fn) }
             /R_ARM_(CALL|JUMP24|PC24)[[:space:]]+__gnu_mcount_nc/ { print fn }" \
      | sort -u
}
arm-linux-gnueabihf-nm drivers/usb/dwc2/dwc2.ko | sort -k3 > /out/dwc2.nm
mcount_functions drivers/usb/dwc2/dwc2.ko > /out/dwc2.mcount-functions
mcount_functions drivers/usb/core/*.o > /out/usbcore.mcount-functions
'

echo "kernel release: $(cat "$out/kernelrelease")"
echo "commit:         $(cat "$out/rpi-linux.commit")"
echo "traceable dwc2 functions:    $(wc -l < "$out/dwc2.mcount-functions")"
echo "traceable usbcore functions: $(wc -l < "$out/usbcore.mcount-functions")"
