// SPDX-License-Identifier: MIT
/*
 * hostlab-mon: capture usbmon binary events as JSON Lines.
 *
 *   hostlab-mon [-b BUS] [-o FILE] [-s SNAPLEN] [-d DEVNUM] [-t SECONDS] [-n COUNT]
 *
 * Reads /dev/usbmonBUS (0 = all buses) with MON_IOCX_GETX, one event per
 * ioctl. Read-only: usbmon is a passive tap in usbcore (core/hcd.c:1527,1643)
 * and does not alter scheduling beyond its own copy cost.
 *
 * Output starts with a "meta" record and a clock snapshot, and ends with
 * usbmon's drop counters and a second clock snapshot.
 */
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "hostlab/hl_clock.h"
#include "hostlab/jsonl.h"
#include "hostlab/usbmon.h"
#include "hostlab/version.h"

#ifdef __linux__
#include <sys/ioctl.h>

/* drivers/usb/mon/mon_bin.c:35-46,131-140; not exported as uapi. */
struct mon_bin_stats {
	uint32_t queued;
	uint32_t dropped;
};

struct mon_bin_get {
	void *hdr;
	void *data;
	size_t alloc;
};

#define MON_IOC_MAGIC 0x92
#define MON_IOCG_STATS _IOR(MON_IOC_MAGIC, 3, struct mon_bin_stats)
#define MON_IOCX_GETX _IOW(MON_IOC_MAGIC, 10, struct mon_bin_get)

static volatile sig_atomic_t stop;

static void on_signal(int sig)
{
	(void)sig;
	stop = 1;
}
#endif

static void usage(FILE *f)
{
	fprintf(f,
		"usage: hostlab-mon [-b BUS] [-o FILE] [-s SNAPLEN] [-d DEVNUM]\n"
		"                   [-t SECONDS] [-n COUNT]\n"
		"  -b BUS      usbmon bus (0 = all buses, default 0)\n"
		"  -o FILE     output JSONL (default stdout)\n"
		"  -s SNAPLEN  data bytes to keep per event (default 256)\n"
		"  -d DEVNUM   only record events for this device address\n"
		"  -t SECONDS  stop after SECONDS\n"
		"  -n COUNT    stop after COUNT recorded events\n");
}

int main(int argc, char **argv)
{
	unsigned bus = 0, snaplen = 256, seconds = 0;
	long devfilter = -1, count = -1;
	const char *outpath = NULL;
	int opt;

	while ((opt = getopt(argc, argv, "b:o:s:d:t:n:h")) != -1) {
		switch (opt) {
		case 'b': bus = (unsigned)strtoul(optarg, NULL, 10); break;
		case 'o': outpath = optarg; break;
		case 's': snaplen = (unsigned)strtoul(optarg, NULL, 10); break;
		case 'd': devfilter = strtol(optarg, NULL, 10); break;
		case 't': seconds = (unsigned)strtoul(optarg, NULL, 10); break;
		case 'n': count = strtol(optarg, NULL, 10); break;
		case 'h': usage(stdout); return 0;
		default: usage(stderr); return 2;
		}
	}
	if (snaplen > 65536) {
		fprintf(stderr, "hostlab-mon: snaplen too large\n");
		return 2;
	}

#ifndef __linux__
	(void)bus; (void)seconds; (void)devfilter; (void)count; (void)outpath;
	fprintf(stderr, "hostlab-mon: usbmon requires Linux\n");
	return 2;
#else
	{
		char path[32];
		uint8_t hdr[HL_MON_HDR_SIZE];
		uint8_t *data = malloc(snaplen ? snaplen : 1);
		struct mon_bin_stats stats = { 0, 0 };
		struct hl_clock_snap snap;
		struct sigaction sa;
		struct hl_jsonl j;
		FILE *out = stdout;
		long recorded = 0;
		int fd;

		if (!data)
			return 1;
		snprintf(path, sizeof(path), "/dev/usbmon%u", bus);
		fd = open(path, O_RDONLY | O_CLOEXEC);
		if (fd < 0) {
			fprintf(stderr, "hostlab-mon: %s: %s%s\n", path, strerror(errno),
				errno == ENOENT ? " (modprobe usbmon?)" : "");
			return 1;
		}
		if (outpath && !(out = fopen(outpath, "w"))) {
			fprintf(stderr, "hostlab-mon: %s: %s\n", outpath, strerror(errno));
			return 1;
		}

		/* No SA_RESTART: a signal must interrupt the blocking GETX. */
		memset(&sa, 0, sizeof(sa));
		sa.sa_handler = on_signal;
		sigaction(SIGINT, &sa, NULL);
		sigaction(SIGTERM, &sa, NULL);
		sigaction(SIGALRM, &sa, NULL);
		if (seconds)
			alarm(seconds);

		hl_jsonl_init(&j, out);
		hl_jsonl_begin(&j);
		hl_jsonl_str(&j, "src", "usbmon");
		hl_jsonl_str(&j, "ev", "meta");
		hl_jsonl_str(&j, "tool", "hostlab-mon");
		hl_jsonl_str(&j, "version", HOSTLAB_VERSION);
		hl_jsonl_u64(&j, "schema", HL_TRACE_SCHEMA);
		hl_jsonl_str(&j, "device", path);
		hl_jsonl_u64(&j, "snaplen", snaplen);
		hl_jsonl_str(&j, "timebase", "realtime_us");
		hl_jsonl_end(&j);
		hl_clock_snapshot(&snap);
		hl_clock_emit(&j, "usbmon", &snap);

		while (!stop && (count < 0 || recorded < count)) {
			struct mon_bin_get get = { hdr, data, snaplen };
			struct hl_mon_event ev;
			size_t n;

			if (ioctl(fd, MON_IOCX_GETX, &get)) {
				if (errno == EINTR)
					continue;
				fprintf(stderr, "hostlab-mon: MON_IOCX_GETX: %s\n",
					strerror(errno));
				break;
			}
			if (hl_mon_decode(hdr, sizeof(hdr), &ev))
				continue;
			if (devfilter >= 0 && ev.devnum != devfilter)
				continue;
			n = ev.flag_data == 0 ? (ev.len_cap < snaplen ? ev.len_cap : snaplen) : 0;
			hl_mon_emit(&j, &ev, data, n);
			recorded++;
		}

		if (ioctl(fd, MON_IOCG_STATS, &stats))
			memset(&stats, 0, sizeof(stats));
		hl_jsonl_begin(&j);
		hl_jsonl_str(&j, "src", "usbmon");
		hl_jsonl_str(&j, "ev", "stats");
		hl_jsonl_u64(&j, "recorded", (uint64_t)recorded);
		hl_jsonl_u64(&j, "queued", stats.queued);
		hl_jsonl_u64(&j, "dropped", stats.dropped);
		hl_jsonl_end(&j);
		hl_clock_snapshot(&snap);
		hl_clock_emit(&j, "usbmon", &snap);

		close(fd);
		if (out != stdout)
			fclose(out);
		free(data);
		return 0;
	}
#endif
}
