// SPDX-License-Identifier: MIT
/*
 * hostlab-ctl: issue control transfers through a hostlab backend and record
 * userspace-side events as JSON Lines (CLOCK_MONOTONIC timestamps).
 *
 *   hostlab-ctl [options] list
 *   hostlab-ctl [options] info DEVICE
 *   hostlab-ctl [options] get-descriptor DEVICE [--type device|config] [--length N]
 *   hostlab-ctl [options] control DEVICE --setup BM,BREQ,WVAL,WIDX,WLEN [--data HEX]
 *   hostlab-ctl [options] cancel-sweep DEVICE --delays-us A:B:STEP [--setup ...]
 *   hostlab-ctl [options] reset DEVICE --allow-reset
 *
 * DEVICE: /dev/bus/usb/BBB/DDD, BUS:DEV, or VVVV:PPPP.
 * Requests are restricted by include/hostlab/policy.h unless relaxed with
 * --allow-write / --allow-nonstandard / --allow-hub.
 */
#include <errno.h>
#include <getopt.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "hostlab/hl_clock.h"
#include "hostlab/jsonl.h"
#include "hostlab/mock_backend.h"
#include "hostlab/policy.h"
#include "hostlab/usb_backend.h"
#include "hostlab/version.h"

struct opts {
	const char *backend;
	const char *out;
	const char *setup;
	const char *data_hex;
	const char *delays;
	const char *desc_type;
	unsigned length;
	unsigned repeat;
	unsigned timeout_ms;
	unsigned policy;
	int allow_reset;
};

static struct hl_jsonl J;

static void usage(FILE *f)
{
	fprintf(f,
		"usage: hostlab-ctl [options] COMMAND [DEVICE] [command options]\n"
		"commands: list | info | get-descriptor | control | cancel-sweep | reset\n"
		"options:\n"
		"  --backend NAME        linux-usbfs (default on Linux) or mock\n"
		"  -o, --out FILE        JSONL output (default stdout)\n"
		"  --repeat N            repeat the operation N times (default 1)\n"
		"  --timeout MS          per-transfer timeout (default 1000; 0 = none)\n"
		"  --setup B,B,W,W,W     bmRequestType,bRequest,wValue,wIndex,wLength (hex)\n"
		"  --data HEX            OUT data stage\n"
		"  --type device|config  get-descriptor type (default device)\n"
		"  --length N            get-descriptor wLength (default 18)\n"
		"  --delays-us A:B:STEP  cancel-sweep delays between submit and cancel\n"
		"  --allow-write --allow-nonstandard --allow-hub --allow-reset\n");
}

static int parse_setup(const char *s, struct hl_setup *out)
{
	unsigned v[5];

	if (sscanf(s, "%x,%x,%x,%x,%x", &v[0], &v[1], &v[2], &v[3], &v[4]) != 5 ||
	    v[0] > 0xff || v[1] > 0xff || v[2] > 0xffff || v[3] > 0xffff || v[4] > 0xffff)
		return -1;
	out->bmRequestType = (uint8_t)v[0];
	out->bRequest = (uint8_t)v[1];
	out->wValue = (uint16_t)v[2];
	out->wIndex = (uint16_t)v[3];
	out->wLength = (uint16_t)v[4];
	return 0;
}

static int parse_hex(const char *s, uint8_t *buf, size_t max, size_t *len)
{
	size_t n = strlen(s);

	if (n % 2 || n / 2 > max)
		return -1;
	for (size_t i = 0; i < n / 2; i++) {
		unsigned b;

		if (sscanf(s + 2 * i, "%2x", &b) != 1)
			return -1;
		buf[i] = (uint8_t)b;
	}
	*len = n / 2;
	return 0;
}

static void setup_bytes(const struct hl_setup *s, uint8_t b[8])
{
	b[0] = s->bmRequestType;
	b[1] = s->bRequest;
	b[2] = (uint8_t)s->wValue;
	b[3] = (uint8_t)(s->wValue >> 8);
	b[4] = (uint8_t)s->wIndex;
	b[5] = (uint8_t)(s->wIndex >> 8);
	b[6] = (uint8_t)s->wLength;
	b[7] = (uint8_t)(s->wLength >> 8);
}

static void emit_error(const char *op, const struct hl_error *err)
{
	hl_jsonl_begin(&J);
	hl_jsonl_str(&J, "src", "user");
	hl_jsonl_str(&J, "ev", "error");
	hl_jsonl_str(&J, "op", op);
	hl_jsonl_str(&J, "status", hl_status_name(err->status));
	hl_jsonl_i64(&J, "errno", err->sys_errno);
	hl_jsonl_str(&J, "msg", err->msg);
	hl_jsonl_end(&J);
	fprintf(stderr, "hostlab-ctl: %s: %s (%s)\n", op, err->msg,
		hl_status_name(err->status));
}

static void emit_meta(const struct opts *o, const char *cmd)
{
	struct hl_clock_snap snap;

	hl_jsonl_begin(&J);
	hl_jsonl_str(&J, "src", "user");
	hl_jsonl_str(&J, "ev", "meta");
	hl_jsonl_str(&J, "tool", "hostlab-ctl");
	hl_jsonl_str(&J, "version", HOSTLAB_VERSION);
	hl_jsonl_u64(&J, "schema", HL_TRACE_SCHEMA);
	hl_jsonl_u64(&J, "api", hl_usb_api_version());
	hl_jsonl_str(&J, "backend", o->backend);
	hl_jsonl_str(&J, "command", cmd);
	hl_jsonl_str(&J, "timebase", strcmp(o->backend, "mock") ? "monotonic_ns" : "virtual_ns");
	hl_jsonl_end(&J);
	hl_clock_snapshot(&snap);
	hl_clock_emit(&J, "user", &snap);
}

static void emit_info(const char *ev, const struct hl_device_info *d, uint32_t caps)
{
	char ports[32] = "";
	size_t off = 0;

	for (unsigned i = 0; i < d->num_ports && off < sizeof(ports) - 4; i++)
		off += (size_t)snprintf(ports + off, sizeof(ports) - off, "%s%u",
					i ? "." : "", d->ports[i]);
	hl_jsonl_begin(&J);
	hl_jsonl_str(&J, "src", "user");
	hl_jsonl_str(&J, "ev", ev);
	hl_jsonl_str(&J, "path", d->path);
	hl_jsonl_u64(&J, "bus", d->busnum);
	hl_jsonl_u64(&J, "dev", d->devnum);
	hl_jsonl_u64(&J, "vid", d->vendor_id);
	hl_jsonl_u64(&J, "pid", d->product_id);
	hl_jsonl_u64(&J, "class", d->device_class);
	hl_jsonl_str(&J, "speed", hl_speed_name(d->speed));
	hl_jsonl_str(&J, "ports", ports);
	if (caps) {
		hl_jsonl_u64(&J, "caps", caps);
		hl_jsonl_bool(&J, "cap_async", !!(caps & HL_CAP_CONTROL_ASYNC));
		hl_jsonl_bool(&J, "cap_cancel", !!(caps & HL_CAP_CANCEL));
		hl_jsonl_bool(&J, "cap_reset", !!(caps & HL_CAP_RESET_DEVICE));
		hl_jsonl_bool(&J, "cap_mmap", !!(caps & HL_CAP_MMAP_BUFFERS));
		hl_jsonl_bool(&J, "cap_phase_control", !!(caps & HL_CAP_PHASE_CONTROL));
		hl_jsonl_bool(&J, "virtual_clock", !!(caps & HL_CAP_VIRTUAL_CLOCK));
	}
	hl_jsonl_end(&J);
}

/* Wait `us` microseconds on the device's clock. Busy-waits on real hardware:
 * nanosleep() granularity on an ARM1176 is far coarser than the delays a
 * cancellation sweep needs. */
static void wait_us(struct hl_backend *be, struct hl_usb_dev *dev, uint64_t us)
{
	uint64_t end;

	if (hl_usb_capabilities(dev) & HL_CAP_VIRTUAL_CLOCK) {
		hl_mock_advance(be, us * 1000u);
		return;
	}
	end = hl_usb_now_ns(dev) + us * 1000u;
	while (hl_usb_now_ns(dev) < end)
		;
}

static int do_control(struct hl_usb_dev *dev, const struct opts *o,
		      const struct hl_setup *s, const uint8_t *out_data,
		      size_t out_len, unsigned seq)
{
	uint8_t buf[HL_POLICY_MAX_WLENGTH], sb[8];
	struct hl_error err;
	uint64_t t0, t1;
	int r;

	memset(buf, 0, sizeof(buf));
	if (out_len)
		memcpy(buf, out_data, out_len);
	t0 = hl_usb_now_ns(dev);
	r = hl_usb_control(dev, s, buf, o->timeout_ms, &err);
	t1 = hl_usb_now_ns(dev);

	setup_bytes(s, sb);
	hl_jsonl_begin(&J);
	hl_jsonl_str(&J, "src", "user");
	hl_jsonl_str(&J, "ev", "xfer");
	hl_jsonl_str(&J, "op", "control");
	hl_jsonl_u64(&J, "seq", seq);
	hl_jsonl_hex(&J, "setup", sb, sizeof(sb));
	hl_jsonl_u64(&J, "t_submit_ns", t0);
	hl_jsonl_u64(&J, "t_done_ns", t1);
	hl_jsonl_u64(&J, "timeout_ms", o->timeout_ms);
	hl_jsonl_str(&J, "status", hl_status_name(r < 0 ? err.status : HL_OK));
	hl_jsonl_i64(&J, "errno", r < 0 ? err.sys_errno : 0);
	hl_jsonl_u64(&J, "actual", r < 0 ? 0 : (uint64_t)r);
	if (r > 0 && (s->bmRequestType & 0x80))
		hl_jsonl_hex(&J, "data", buf, (size_t)r);
	hl_jsonl_end(&J);
	return r < 0 ? -1 : 0;
}

static int cancel_sweep(struct hl_backend *be, struct hl_usb_dev *dev,
			const struct opts *o, const struct hl_setup *s)
{
	unsigned a = 0, b = 0, step = 1;
	unsigned counts[3][HL_ERR_OTHER + 1];
	static const char *cancel_names[3] = { "ok", "not_pending", "error" };
	unsigned seq = 0;

	memset(counts, 0, sizeof(counts));
	if (!o->delays || sscanf(o->delays, "%u:%u:%u", &a, &b, &step) != 3 || !step || b < a) {
		fprintf(stderr, "hostlab-ctl: --delays-us A:B:STEP required (A <= B, STEP > 0)\n");
		return 2;
	}
	for (unsigned d = a; d <= b; d += step) {
		for (unsigned rep = 0; rep < o->repeat; rep++, seq++) {
			uint8_t buf[HL_POLICY_MAX_WLENGTH], sb[8];
			struct hl_xfer x, *done = NULL;
			struct hl_error err;
			int cres;

			memset(&x, 0, sizeof(x));
			memset(buf, 0, sizeof(buf));
			x.setup = *s;
			x.data = buf;
			if (hl_usb_submit_control(dev, &x, &err)) {
				emit_error("submit", &err);
				return 1;
			}
			wait_us(be, dev, d);
			if (!hl_usb_cancel(dev, &x, &err))
				cres = 0;
			else
				cres = err.status == HL_ERR_NOT_PENDING ? 1 : 2;
			if (hl_usb_reap(dev, o->timeout_ms ? (int)o->timeout_ms : -1, &done, &err)) {
				emit_error("reap", &err);
				return 1;
			}
			counts[cres][done->status]++;

			setup_bytes(s, sb);
			hl_jsonl_begin(&J);
			hl_jsonl_str(&J, "src", "user");
			hl_jsonl_str(&J, "ev", "xfer");
			hl_jsonl_str(&J, "op", "async");
			hl_jsonl_u64(&J, "seq", seq);
			hl_jsonl_u64(&J, "delay_us", d);
			hl_jsonl_hex(&J, "setup", sb, sizeof(sb));
			hl_jsonl_u64(&J, "t_submit_ns", done->t_submit_ns);
			if (done->t_cancel_ns)
				hl_jsonl_u64(&J, "t_cancel_ns", done->t_cancel_ns);
			else
				hl_jsonl_null(&J, "t_cancel_ns");
			hl_jsonl_u64(&J, "t_reap_ns", done->t_reap_ns);
			hl_jsonl_str(&J, "cancel", cancel_names[cres]);
			hl_jsonl_str(&J, "status", hl_status_name(done->status));
			hl_jsonl_i64(&J, "urb_status", done->urb_status);
			hl_jsonl_u64(&J, "actual", done->actual_length);
			hl_jsonl_end(&J);
		}
	}

	for (int c = 0; c < 3; c++) {
		for (int st = 0; st <= HL_ERR_OTHER; st++) {
			if (!counts[c][st])
				continue;
			hl_jsonl_begin(&J);
			hl_jsonl_str(&J, "src", "user");
			hl_jsonl_str(&J, "ev", "summary");
			hl_jsonl_str(&J, "cancel", cancel_names[c]);
			hl_jsonl_str(&J, "status", hl_status_name((enum hl_status)st));
			hl_jsonl_u64(&J, "count", counts[c][st]);
			hl_jsonl_end(&J);
		}
	}
	return 0;
}

int main(int argc, char **argv)
{
	enum { O_BACKEND = 256, O_REPEAT, O_TIMEOUT, O_SETUP, O_DATA, O_TYPE,
	       O_LENGTH, O_DELAYS, O_AW, O_ANS, O_AH, O_AR };
	static const struct option longopts[] = {
		{ "backend", required_argument, NULL, O_BACKEND },
		{ "out", required_argument, NULL, 'o' },
		{ "repeat", required_argument, NULL, O_REPEAT },
		{ "timeout", required_argument, NULL, O_TIMEOUT },
		{ "setup", required_argument, NULL, O_SETUP },
		{ "data", required_argument, NULL, O_DATA },
		{ "type", required_argument, NULL, O_TYPE },
		{ "length", required_argument, NULL, O_LENGTH },
		{ "delays-us", required_argument, NULL, O_DELAYS },
		{ "allow-write", no_argument, NULL, O_AW },
		{ "allow-nonstandard", no_argument, NULL, O_ANS },
		{ "allow-hub", no_argument, NULL, O_AH },
		{ "allow-reset", no_argument, NULL, O_AR },
		{ "help", no_argument, NULL, 'h' },
		{ NULL, 0, NULL, 0 },
	};
	struct opts o = {
#ifdef __linux__
		.backend = "linux-usbfs",
#else
		.backend = "mock",
#endif
		.desc_type = "device", .length = 18, .repeat = 1, .timeout_ms = 1000,
	};
	struct hl_device_info info;
	struct hl_usb_dev *dev = NULL;
	struct hl_backend *be;
	struct hl_setup setup;
	struct hl_error err;
	uint8_t out_data[HL_POLICY_MAX_WLENGTH];
	size_t out_len = 0;
	const char *cmd, *loc;
	FILE *out = stdout;
	int opt, rc = 0;

	while ((opt = getopt_long(argc, argv, "o:h", longopts, NULL)) != -1) {
		switch (opt) {
		case O_BACKEND: o.backend = optarg; break;
		case 'o': o.out = optarg; break;
		case O_REPEAT: o.repeat = (unsigned)strtoul(optarg, NULL, 10); break;
		case O_TIMEOUT: o.timeout_ms = (unsigned)strtoul(optarg, NULL, 10); break;
		case O_SETUP: o.setup = optarg; break;
		case O_DATA: o.data_hex = optarg; break;
		case O_TYPE: o.desc_type = optarg; break;
		case O_LENGTH: o.length = (unsigned)strtoul(optarg, NULL, 10); break;
		case O_DELAYS: o.delays = optarg; break;
		case O_AW: o.policy |= HL_POLICY_ALLOW_WRITE; break;
		case O_ANS: o.policy |= HL_POLICY_ALLOW_NONSTANDARD; break;
		case O_AH: o.policy |= HL_POLICY_ALLOW_HUB; break;
		case O_AR: o.allow_reset = 1; break;
		case 'h': usage(stdout); return 0;
		default: usage(stderr); return 2;
		}
	}
	if (optind >= argc) {
		usage(stderr);
		return 2;
	}
	cmd = argv[optind++];
	loc = optind < argc ? argv[optind++] : NULL;
	if (!o.repeat)
		o.repeat = 1;

	be = hl_backend_get(o.backend);
	if (!be) {
		fprintf(stderr, "hostlab-ctl: unknown backend '%s'\n", o.backend);
		return 2;
	}
	if (o.out && !(out = fopen(o.out, "w"))) {
		fprintf(stderr, "hostlab-ctl: %s: %s\n", o.out, strerror(errno));
		return 1;
	}
	hl_jsonl_init(&J, out);
	emit_meta(&o, cmd);

	if (!strcmp(cmd, "list")) {
		struct hl_device_info devs[64];
		int n = hl_usb_enumerate(be, devs, 64, &err);

		if (n < 0) {
			emit_error("enumerate", &err);
			return 1;
		}
		for (int i = 0; i < n && i < 64; i++)
			emit_info("device", &devs[i], 0);
		return 0;
	}

	if (!loc) {
		fprintf(stderr, "hostlab-ctl: %s needs a DEVICE\n", cmd);
		return 2;
	}
	if (hl_usb_open(be, loc, &dev, &err) || hl_usb_get_info(dev, &info, &err)) {
		emit_error("open", &err);
		return 1;
	}

	if (!strcmp(cmd, "info")) {
		emit_info("device", &info, hl_usb_capabilities(dev));
	} else if (!strcmp(cmd, "get-descriptor") || !strcmp(cmd, "control") ||
		   !strcmp(cmd, "cancel-sweep")) {
		if (o.setup) {
			if (parse_setup(o.setup, &setup)) {
				fprintf(stderr, "hostlab-ctl: bad --setup '%s'\n", o.setup);
				rc = 2;
				goto out;
			}
		} else if (!strcmp(cmd, "control")) {
			fprintf(stderr, "hostlab-ctl: control needs --setup\n");
			rc = 2;
			goto out;
		} else {
			setup = (struct hl_setup){
				.bmRequestType = 0x80, .bRequest = 6,
				.wValue = (uint16_t)((!strcmp(o.desc_type, "config") ? 2 : 1) << 8),
				.wLength = (uint16_t)o.length,
			};
		}
		if (o.data_hex && parse_hex(o.data_hex, out_data, sizeof(out_data), &out_len)) {
			fprintf(stderr, "hostlab-ctl: bad --data\n");
			rc = 2;
			goto out;
		}
		if (!(setup.bmRequestType & 0x80) && out_len != setup.wLength) {
			fprintf(stderr, "hostlab-ctl: --data has %zu bytes, wLength is %u\n",
				out_len, setup.wLength);
			rc = 2;
			goto out;
		}
		if (hl_policy_check_control(&setup, &info, o.policy, &err)) {
			emit_error("policy", &err);
			rc = 1;
			goto out;
		}
		if (!strcmp(cmd, "cancel-sweep")) {
			rc = cancel_sweep(be, dev, &o, &setup);
		} else {
			for (unsigned i = 0; i < o.repeat; i++)
				if (do_control(dev, &o, &setup, out_data, out_len, i))
					rc = 1;
		}
	} else if (!strcmp(cmd, "reset")) {
		if (!o.allow_reset) {
			fprintf(stderr, "hostlab-ctl: reset needs --allow-reset\n");
			rc = 2;
			goto out;
		}
		if (info.device_class == 0x09 && !(o.policy & HL_POLICY_ALLOW_HUB)) {
			fprintf(stderr, "hostlab-ctl: refusing to reset a hub\n");
			rc = 1;
			goto out;
		}
		{
			uint64_t t0 = hl_usb_now_ns(dev);
			int r = hl_usb_reset_device(dev, &err);
			uint64_t t1 = hl_usb_now_ns(dev);

			hl_jsonl_begin(&J);
			hl_jsonl_str(&J, "src", "user");
			hl_jsonl_str(&J, "ev", "reset");
			hl_jsonl_u64(&J, "t_start_ns", t0);
			hl_jsonl_u64(&J, "t_done_ns", t1);
			hl_jsonl_str(&J, "status", hl_status_name(r ? err.status : HL_OK));
			hl_jsonl_i64(&J, "errno", r ? err.sys_errno : 0);
			hl_jsonl_end(&J);
			rc = r ? 1 : 0;
		}
	} else {
		fprintf(stderr, "hostlab-ctl: unknown command '%s'\n", cmd);
		rc = 2;
	}

out:
	{
		struct hl_clock_snap snap;

		hl_clock_snapshot(&snap);
		hl_clock_emit(&J, "user", &snap);
	}
	hl_usb_close(dev);
	if (out != stdout)
		fclose(out);
	return rc;
}
