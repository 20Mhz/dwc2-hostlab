/* SPDX-License-Identifier: MIT */
/*
 * Clock correlation.
 *
 * Three time bases meet in one experiment:
 *   - usbmon binary events: CLOCK_REALTIME, microseconds (mon_bin.c:505)
 *   - ftrace: whatever trace_clock is set to; hostlab selects "mono"
 *   - hostlab userspace events: CLOCK_MONOTONIC, nanoseconds
 * A snapshot samples REALTIME between two MONOTONIC reads, so the analyser
 * can map REALTIME to MONOTONIC with a known uncertainty. Snapshots are taken
 * at the start and end of every capture to bound NTP slew in between.
 */
#ifndef HOSTLAB_HL_CLOCK_H
#define HOSTLAB_HL_CLOCK_H

#include <stdint.h>

#include "hostlab/jsonl.h"

#ifdef __cplusplus
extern "C" {
#endif

struct hl_clock_snap {
	uint64_t mono_ns;     /* midpoint of the bracketing MONOTONIC reads */
	uint64_t real_ns;
	uint64_t boot_ns;     /* 0 where CLOCK_BOOTTIME does not exist */
	uint64_t uncert_ns;   /* width of the bracket */
};

void hl_clock_snapshot(struct hl_clock_snap *s);
uint64_t hl_clock_mono_ns(void);

/* Emit {"ev":"clock",...} with the given source tag. */
void hl_clock_emit(struct hl_jsonl *j, const char *src, const struct hl_clock_snap *s);

#ifdef __cplusplus
}
#endif

#endif /* HOSTLAB_HL_CLOCK_H */
