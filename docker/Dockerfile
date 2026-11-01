# Build environment for dwc2-hostlab.
#
# Userspace tools are built as *static* binaries with Debian's armel
# cross compiler (ARMv5TE libc, compiled with -march=armv6kz) so they run on
# the BCM2835's ARM1176JZF-S without depending on the target's glibc build.
# qemu-user-static is used to execute them with `-cpu arm1176`, which traps
# any ARMv7-only instruction that would SIGILL on a Pi Zero W.
#
# The armhf cross compiler is used for kernel and module builds, matching the
# Raspberry Pi documentation for 32-bit (bcmrpi_defconfig) kernels.
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        bc bison flex libssl-dev libelf-dev kmod cpio \
        binutils-arm-linux-gnueabi gcc-arm-linux-gnueabi libc6-dev-armel-cross \
        crossbuild-essential-armhf \
        ca-certificates git make python3 qemu-user-static file \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /work
