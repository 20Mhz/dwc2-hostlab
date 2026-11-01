#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Build static ARMv6 (ARM1176JZF-S) binaries and run the C test suite under
# qemu-arm-static -cpu arm1176, inside the project's Docker image.
#
# Debian's armel libc is ARMv5TE code, so a static link with -march=armv6kz
# contains no ARMv7-only instructions; the qemu run proves it, and the
# negative control below proves qemu would catch them if there were any.
set -euo pipefail

IMAGE="${HOSTLAB_IMAGE:-dwc2-hostlab-build}"
repo_root="$(cd "$(dirname "$0")/.." && pwd)"

docker build -q -t "$IMAGE" "$repo_root/docker" >/dev/null

docker run --rm -v "$repo_root:/work" -w /work "$IMAGE" bash -euo pipefail -c '
make -s BUILD=build/armv6 CC=arm-linux-gnueabi-gcc AR=arm-linux-gnueabi-ar \
     CFLAGS="-O2 -march=armv6kz -marm" LDFLAGS=-static all
for f in build/armv6/hostlab-ctl build/armv6/hostlab-mon build/armv6/test_lib; do
    arch=$(arm-linux-gnueabi-readelf -A "$f" | sed -n "s/.*Tag_CPU_arch: //p")
    echo "$f: Tag_CPU_arch=$arch"
    [ "$arch" = "v6KZ" ] || { echo "unexpected architecture"; exit 1; }
done
make -s BUILD=build/armv6 TEST_RUNNER="qemu-arm-static -cpu arm1176" test-c

# Negative control: code that executes an ARMv7-only instruction (MOVW is
# ARMv6T2+, absent on ARM1176) must die on the emulated ARM1176 but run on a
# v7 CPU model. Otherwise the arm1176 run above proves nothing.
cat > /tmp/v7.c <<EOF
int main(void) { int r; __asm__ volatile("movw %0, #1" : "=r"(r)); return r - 1; }
EOF
arm-linux-gnueabi-gcc -static -O2 -march=armv7-a -marm -o /tmp/v7 /tmp/v7.c
ulimit -c 0
qemu-arm-static -cpu cortex-a7 /tmp/v7 || { echo "negative control broken on cortex-a7"; exit 1; }
if (cd /tmp && qemu-arm-static -cpu arm1176 /tmp/v7) 2>/dev/null; then
    echo "negative control: ARMv7 instruction ran on arm1176 - check is ineffective"; exit 1
fi
echo "negative control: ARMv7 instruction trapped on arm1176 emulation (expected)"
'
