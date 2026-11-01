#!/bin/sh
# SPDX-License-Identifier: MIT
# Run ON the Pi as root before a session: load the modules hostlab reads and
# make sure debugfs/tracefs are mounted. Changes nothing persistently.
set -eu

modprobe usbmon
modprobe configs 2>/dev/null || echo "note: 'configs' module unavailable; /proc/config.gz absent"
mountpoint -q /sys/kernel/debug || mount -t debugfs none /sys/kernel/debug
[ -e /sys/kernel/tracing/trace ] || mount -t tracefs nodev /sys/kernel/tracing
echo "usbmon: $(ls /dev/usbmon* 2>/dev/null | tr '\n' ' ')"
echo "tracefs: $(cat /sys/kernel/tracing/trace_clock)"
