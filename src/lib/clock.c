// SPDX-License-Identifier: MIT
#include <time.h>

#include "hostlab/hl_clock.h"

static uint64_t read_ns(clockid_t id)
{
	struct timespec ts;

	if (clock_gettime(id, &ts))
		return 0;
	return (uint64_t)ts.tv_sec * 1000000000u + (uint64_t)ts.tv_nsec;
}

uint64_t hl_clock_mono_ns(void)
{
	return read_ns(CLOCK_MONOTONIC);
}

void hl_clock_snapshot(struct hl_clock_snap *s)
{
	uint64_t best_width = UINT64_MAX;

	/* Keep the tightest of a few brackets; a preemption between the reads
	 * only widens one of them. */
	for (int i = 0; i < 5; i++) {
		uint64_t m1 = read_ns(CLOCK_MONOTONIC);
		uint64_t r = read_ns(CLOCK_REALTIME);
		uint64_t m2 = read_ns(CLOCK_MONOTONIC);

		if (m2 - m1 < best_width) {
			best_width = m2 - m1;
			s->mono_ns = m1 + (m2 - m1) / 2;
			s->real_ns = r;
			s->uncert_ns = m2 - m1;
		}
	}
#ifdef CLOCK_BOOTTIME
	s->boot_ns = read_ns(CLOCK_BOOTTIME);
#else
	s->boot_ns = 0;
#endif
}

void hl_clock_emit(struct hl_jsonl *j, const char *src, const struct hl_clock_snap *s)
{
	hl_jsonl_begin(j);
	hl_jsonl_str(j, "src", src);
	hl_jsonl_str(j, "ev", "clock");
	hl_jsonl_u64(j, "mono_ns", s->mono_ns);
	hl_jsonl_u64(j, "real_ns", s->real_ns);
	hl_jsonl_u64(j, "boot_ns", s->boot_ns);
	hl_jsonl_u64(j, "uncert_ns", s->uncert_ns);
	hl_jsonl_end(j);
}
