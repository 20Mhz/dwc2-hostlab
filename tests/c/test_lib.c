// SPDX-License-Identifier: MIT
#include <errno.h>
#include <stdlib.h>

#include "harness.h"
#include "hostlab/jsonl.h"
#include "hostlab/mock_backend.h"
#include "hostlab/policy.h"
#include "hostlab/usb_backend.h"
#include "hostlab/usbmon.h"

int hl_test_failures;
int hl_test_checks;

static const struct hl_setup get_dev_desc = {
	.bmRequestType = 0x80, .bRequest = 6, .wValue = 0x0100, .wLength = 18,
};

/* ---- usbmon decoder ---- */

static void test_usbmon_roundtrip(void)
{
	struct hl_mon_event in = {
		.id = 0xc1a2b3c0u, .type = 'S', .xfer_type = HL_MON_CTRL,
		.epnum = 0x80, .devnum = 4, .busnum = 1, .flag_setup = 0,
		.flag_data = '<', .ts_sec = 1793500000, .ts_usec = 123456,
		.status = -115, .len_urb = 18, .len_cap = 0,
		.setup = { 0x80, 0x06, 0x00, 0x01, 0x00, 0x00, 0x12, 0x00 },
		.interval = 0, .start_frame = 0, .xfer_flags = 0x200, .ndesc = 0,
	};
	struct hl_mon_event out;
	uint8_t buf[HL_MON_HDR_SIZE];

	hl_mon_encode(&in, buf);
	CHECK_EQ(hl_mon_decode(buf, sizeof(buf), &out), 0);
	CHECK_EQ(out.id, in.id);
	CHECK_EQ(out.type, 'S');
	CHECK_EQ(out.epnum, 0x80);
	CHECK_EQ(out.devnum, 4);
	CHECK_EQ(out.busnum, 1);
	CHECK_EQ(out.ts_sec, 1793500000);
	CHECK_EQ(out.ts_usec, 123456);
	CHECK_EQ(out.status, -115);
	CHECK_EQ(out.len_urb, 18);
	CHECK_EQ(out.xfer_flags, 0x200);
	CHECK(!memcmp(out.setup, in.setup, 8));
}

static void test_usbmon_layout(void)
{
	/* Offsets from struct mon_bin_hdr (mon_bin.c:91-116). */
	struct hl_mon_event ev = { .type = 'C', .xfer_type = HL_MON_BULK };
	uint8_t buf[HL_MON_HDR_SIZE];
	int32_t v;

	ev.status = -2;
	ev.len_cap = 0x11223344u;
	ev.ndesc = 7;
	hl_mon_encode(&ev, buf);
	CHECK_EQ(buf[8], 'C');
	CHECK_EQ(buf[9], HL_MON_BULK);
	memcpy(&v, buf + 28, 4);
	CHECK_EQ(v, -2);
	memcpy(&v, buf + 36, 4);
	CHECK_EQ((uint32_t)v, 0x11223344u);
	memcpy(&v, buf + 60, 4);
	CHECK_EQ(v, 7);
}

static void test_usbmon_rejects(void)
{
	uint8_t buf[HL_MON_HDR_SIZE] = { 0 };
	struct hl_mon_event ev;

	CHECK_EQ(hl_mon_decode(buf, 48, &ev), -1); /* API0-sized header */
	buf[8] = 'X';
	CHECK_EQ(hl_mon_decode(buf, sizeof(buf), &ev), -1);
}

/* ---- JSONL ---- */

static void test_jsonl_escaping(void)
{
	char out[256] = { 0 };
	FILE *f = tmpfile();
	struct hl_jsonl j;
	size_t n;

	CHECK(f != NULL);
	if (!f)
		return;
	hl_jsonl_init(&j, f);
	hl_jsonl_begin(&j);
	hl_jsonl_str(&j, "s", "a\"b\\c\n");
	hl_jsonl_i64(&j, "n", -5);
	hl_jsonl_hex(&j, "h", (const uint8_t *)"\x01\xff", 2);
	hl_jsonl_bool(&j, "b", 1);
	hl_jsonl_null(&j, "z");
	hl_jsonl_end(&j);
	rewind(f);
	n = fread(out, 1, sizeof(out) - 1, f);
	fclose(f);
	out[n] = '\0';
	CHECK_STR(out, "{\"s\":\"a\\\"b\\\\c\\u000a\",\"n\":-5,\"h\":\"01ff\",\"b\":true,\"z\":null}\n");
}

/* ---- status mapping ---- */

static void test_errno_mapping(void)
{
	CHECK_EQ(hl_status_from_errno(0), HL_OK);
	CHECK_EQ(hl_status_from_errno(-EPIPE), HL_ERR_STALL);
	CHECK_EQ(hl_status_from_errno(EPROTO), HL_ERR_PROTOCOL);
	CHECK_EQ(hl_status_from_errno(-EILSEQ), HL_ERR_PROTOCOL);
	CHECK_EQ(hl_status_from_errno(-ENOENT), HL_ERR_CANCELLED);
	CHECK_EQ(hl_status_from_errno(-ECONNRESET), HL_ERR_CANCELLED);
	CHECK_EQ(hl_status_from_errno(-ETIMEDOUT), HL_ERR_TIMEOUT);
	CHECK_EQ(hl_status_from_errno(-ESHUTDOWN), HL_ERR_NO_DEVICE);
	CHECK_EQ(hl_status_from_errno(-EOVERFLOW), HL_ERR_OVERFLOW);
	CHECK_STR(hl_status_name(HL_ERR_NOT_PENDING), "not_pending");
	CHECK_EQ(hl_usb_api_version(), (HL_USB_API_MAJOR << 16) | HL_USB_API_MINOR);
}

/* ---- policy ---- */

static void test_policy(void)
{
	struct hl_device_info dev = { .device_class = 0xff };
	struct hl_device_info hub = { .device_class = 0x09 };
	struct hl_setup set_cfg = { .bmRequestType = 0x00, .bRequest = 9, .wValue = 1 };
	struct hl_setup vendor_in = { .bmRequestType = 0xc0, .bRequest = 1, .wLength = 4 };
	struct hl_setup reserved = { .bmRequestType = 0xe0 };
	struct hl_setup big = get_dev_desc;
	struct hl_error err;

	big.wLength = 4097;
	CHECK_EQ(hl_policy_check_control(&get_dev_desc, &dev, 0, &err), 0);
	CHECK_EQ(hl_policy_check_control(&get_dev_desc, &hub, 0, &err), -1);
	CHECK_EQ(err.status, HL_ERR_ACCESS);
	CHECK_EQ(hl_policy_check_control(&get_dev_desc, &hub, HL_POLICY_ALLOW_HUB, &err), 0);
	CHECK_EQ(hl_policy_check_control(&set_cfg, &dev, 0, &err), -1);
	CHECK_EQ(hl_policy_check_control(&set_cfg, &dev, HL_POLICY_ALLOW_WRITE, &err), 0);
	CHECK_EQ(hl_policy_check_control(&vendor_in, &dev, 0, &err), -1);
	CHECK_EQ(hl_policy_check_control(&vendor_in, &dev, HL_POLICY_ALLOW_NONSTANDARD, &err), 0);
	CHECK_EQ(hl_policy_check_control(&reserved, &dev, ~0u, &err), -1);
	CHECK_EQ(hl_policy_check_control(&big, &dev, ~0u, &err), -1);
	CHECK_EQ(err.status, HL_ERR_INVALID);
}

/* ---- mock backend ---- */

static struct hl_backend *mock_with_device(struct hl_usb_dev **dev)
{
	static const uint8_t dd[18] = { 18, 1, 0, 2, 0, 0, 0, 64, 0x09, 0x12, 0x01, 0, 0, 1, 0, 0, 0, 1 };
	struct hl_device_info info = { .busnum = 1, .devnum = 5, .speed = HL_SPEED_FULL };
	struct hl_backend *be = hl_mock_create();
	struct hl_error err;

	hl_mock_add_device(be, &info, dd, sizeof(dd), NULL, 0);
	CHECK_EQ(hl_usb_open(be, "1:5", dev, &err), 0);
	return be;
}

static void test_mock_sync_control(void)
{
	struct hl_usb_dev *dev;
	struct hl_backend *be = mock_with_device(&dev);
	struct hl_setup get_status = { .bmRequestType = 0x80, .bRequest = 0, .wLength = 2 };
	struct hl_setup unknown = { .bmRequestType = 0xc0, .bRequest = 0x42, .wLength = 1 };
	uint8_t buf[64];
	struct hl_error err;

	CHECK_EQ(hl_usb_control(dev, &get_dev_desc, buf, 100, &err), 18);
	CHECK_EQ(buf[0], 18);
	CHECK_EQ(buf[8], 0x09);
	CHECK_EQ(hl_mock_now(be), 1000000); /* default 1 ms latency */
	CHECK_EQ(hl_usb_control(dev, &get_status, buf, 100, &err), 2);
	CHECK_EQ(hl_usb_control(dev, &unknown, buf, 100, &err), -1);
	CHECK_EQ(err.status, HL_ERR_STALL);
	CHECK_EQ(hl_mock_log_count(be), 3);
	CHECK_EQ(hl_mock_log_get(be, 2)->status, HL_ERR_STALL);

	/* NULL data with wLength > 0 is rejected before reaching the backend. */
	CHECK_EQ(hl_usb_control(dev, &get_dev_desc, NULL, 100, &err), -1);
	CHECK_EQ(err.status, HL_ERR_INVALID);
	CHECK_EQ(hl_mock_log_count(be), 3);

	hl_usb_close(dev);
	hl_mock_destroy(be);
}

static void test_mock_short_descriptor(void)
{
	struct hl_usb_dev *dev;
	struct hl_backend *be = mock_with_device(&dev);
	struct hl_setup s = get_dev_desc;
	uint8_t buf[64];
	struct hl_error err;

	s.wLength = 8; /* usbcore's first enumeration read asks for 8 or 64 */
	CHECK_EQ(hl_usb_control(dev, &s, buf, 100, &err), 8);
	s.wLength = 64;
	CHECK_EQ(hl_usb_control(dev, &s, buf, 100, &err), 18);
	hl_usb_close(dev);
	hl_mock_destroy(be);
}

static void test_mock_timeout(void)
{
	struct hl_usb_dev *dev;
	struct hl_backend *be = mock_with_device(&dev);
	struct hl_mock_response nak_forever = {
		.match = get_dev_desc, .match_mask = HL_MOCK_MATCH_ALL,
		.status = HL_OK, .latency_ns = 5ull * 1000 * 1000 * 1000,
	};
	uint8_t buf[64];
	struct hl_error err;

	CHECK_EQ(hl_mock_push_response(be, 5, &nak_forever), 0);
	CHECK_EQ(hl_usb_control(dev, &get_dev_desc, buf, 250, &err), -1);
	CHECK_EQ(err.status, HL_ERR_TIMEOUT);
	CHECK_EQ(hl_mock_now(be), 250ull * 1000 * 1000);
	/* The programmed response was consumed; the next request gets the default. */
	CHECK_EQ(hl_usb_control(dev, &get_dev_desc, buf, 250, &err), 18);
	hl_usb_close(dev);
	hl_mock_destroy(be);
}

static void test_mock_cancel_pending(void)
{
	struct hl_usb_dev *dev;
	struct hl_backend *be = mock_with_device(&dev);
	struct hl_xfer x = { .setup = get_dev_desc }, *done;
	uint8_t buf[64];
	struct hl_error err;

	x.data = buf;
	CHECK_EQ(hl_usb_submit_control(dev, &x, &err), 0);
	CHECK_EQ(hl_usb_submit_control(dev, &x, &err), -1); /* already in flight */
	hl_mock_advance(be, 400 * 1000);                    /* 0.4 ms < 1 ms latency */
	CHECK_EQ(hl_usb_cancel(dev, &x, &err), 0);
	CHECK_EQ(hl_usb_reap(dev, 0, &done, &err), 0);
	CHECK(done == &x);
	CHECK_EQ(x.status, HL_ERR_CANCELLED);
	CHECK_EQ(x.urb_status, -ENOENT);
	CHECK_EQ(x.actual_length, 0);
	CHECK_EQ(x.t_cancel_ns, 400 * 1000);
	CHECK(x.backend_priv == NULL);
	CHECK_EQ(hl_mock_log_get(be, 0)->cancelled, 1);
	/* Nothing left to reap. */
	CHECK_EQ(hl_usb_reap(dev, 0, &done, &err), -1);
	CHECK_EQ(err.status, HL_ERR_AGAIN);
	hl_usb_close(dev);
	hl_mock_destroy(be);
}

static void test_mock_cancel_too_late(void)
{
	struct hl_usb_dev *dev;
	struct hl_backend *be = mock_with_device(&dev);
	struct hl_xfer x = { .setup = get_dev_desc }, *done;
	uint8_t buf[64];
	struct hl_error err;

	x.data = buf;
	CHECK_EQ(hl_usb_submit_control(dev, &x, &err), 0);
	hl_mock_advance(be, 2 * 1000 * 1000);
	CHECK_EQ(hl_usb_cancel(dev, &x, &err), -1);
	CHECK_EQ(err.status, HL_ERR_NOT_PENDING);
	CHECK_EQ(hl_usb_reap(dev, 0, &done, &err), 0);
	CHECK_EQ(x.status, HL_OK);
	CHECK_EQ(x.actual_length, 18);
	CHECK_EQ(x.t_cancel_ns, 0);
	hl_usb_close(dev);
	hl_mock_destroy(be);
}

static void test_mock_reap_order_and_wait(void)
{
	struct hl_usb_dev *dev;
	struct hl_backend *be = mock_with_device(&dev);
	struct hl_mock_response slow = {
		.match = { .bRequest = 6 }, .match_mask = HL_MOCK_MATCH_REQUEST,
		.status = HL_OK, .latency_ns = 3000000,
	};
	struct hl_xfer a = { .setup = get_dev_desc }, b = { .setup = get_dev_desc };
	struct hl_xfer c = { .setup = get_dev_desc }, *done;
	uint8_t ba[64], bb[64], bc[64];
	struct hl_error err;

	a.data = ba;
	b.data = bb;
	c.data = bc;
	CHECK_EQ(hl_mock_push_response(be, 5, &slow), 0);
	CHECK_EQ(hl_usb_submit_control(dev, &a, &err), 0); /* 3 ms */
	CHECK_EQ(hl_usb_submit_control(dev, &b, &err), 0); /* 1 ms */
	CHECK_EQ(hl_usb_submit_control(dev, &c, &err), 0); /* 1 ms */
	CHECK_EQ(hl_usb_reap(dev, 0, &done, &err), -1);
	CHECK_EQ(hl_usb_reap(dev, -1, &done, &err), 0);
	CHECK(done == &b || done == &c);
	CHECK_EQ(hl_mock_now(be), 1000000);
	/* The transfer moved within the pending table must still be cancellable. */
	CHECK_EQ(hl_usb_cancel(dev, &a, &err), 0);
	CHECK_EQ(hl_usb_reap(dev, -1, &done, &err), 0);
	CHECK_EQ(hl_usb_reap(dev, -1, &done, &err), 0);
	CHECK_EQ(a.status, HL_ERR_CANCELLED);
	CHECK_EQ(hl_usb_reap(dev, 5, &done, &err), -1);
	hl_usb_close(dev);
	hl_mock_destroy(be);
}

static void test_mock_reset(void)
{
	struct hl_usb_dev *dev;
	struct hl_backend *be = mock_with_device(&dev);
	struct hl_xfer x = { .setup = get_dev_desc }, *done;
	uint8_t buf[64];
	struct hl_error err;

	x.data = buf;
	CHECK_EQ(hl_usb_reset_device(dev, &err), 0);
	CHECK_EQ(hl_mock_reset_count(be, 5), 1);
	CHECK_EQ(hl_usb_submit_control(dev, &x, &err), 0);
	CHECK_EQ(hl_usb_reset_device(dev, &err), -1);
	CHECK_EQ(err.status, HL_ERR_UNSUPPORTED);
	CHECK_EQ(hl_usb_reap(dev, -1, &done, &err), 0);
	hl_usb_close(dev);
	hl_mock_destroy(be);
}

static void test_mock_open_errors(void)
{
	struct hl_backend *be = hl_mock_create();
	struct hl_usb_dev *dev = NULL;
	struct hl_error err;

	CHECK_EQ(hl_usb_open(be, "garbage", &dev, &err), -1);
	CHECK_EQ(err.status, HL_ERR_INVALID);
	CHECK_EQ(hl_usb_open(be, "/dev/bus/usb/001/009", &dev, &err), -1);
	CHECK_EQ(err.status, HL_ERR_NO_DEVICE);
	CHECK(hl_backend_get("nonexistent") == NULL);
	CHECK(hl_backend_get("mock") != NULL);
	hl_mock_destroy(be);
}

static void test_mock_capabilities(void)
{
	struct hl_usb_dev *dev;
	struct hl_backend *be = mock_with_device(&dev);
	uint32_t caps = hl_usb_capabilities(dev);

	CHECK(caps & HL_CAP_CANCEL);
	CHECK(caps & HL_CAP_VIRTUAL_CLOCK);
	CHECK(!(caps & HL_CAP_PHASE_CONTROL));
	CHECK(!(caps & HL_CAP_TOKEN_CONTROL));
	hl_usb_close(dev);
	hl_mock_destroy(be);
}

static const struct hl_test tests[] = {
	{ "usbmon_roundtrip", test_usbmon_roundtrip },
	{ "usbmon_layout", test_usbmon_layout },
	{ "usbmon_rejects", test_usbmon_rejects },
	{ "jsonl_escaping", test_jsonl_escaping },
	{ "errno_mapping", test_errno_mapping },
	{ "policy", test_policy },
	{ "mock_sync_control", test_mock_sync_control },
	{ "mock_short_descriptor", test_mock_short_descriptor },
	{ "mock_timeout", test_mock_timeout },
	{ "mock_cancel_pending", test_mock_cancel_pending },
	{ "mock_cancel_too_late", test_mock_cancel_too_late },
	{ "mock_reap_order_and_wait", test_mock_reap_order_and_wait },
	{ "mock_reset", test_mock_reset },
	{ "mock_open_errors", test_mock_open_errors },
	{ "mock_capabilities", test_mock_capabilities },
};

int main(void)
{
	for (size_t i = 0; i < sizeof(tests) / sizeof(tests[0]); i++) {
		int before = hl_test_failures;

		tests[i].fn();
		printf("%-28s %s\n", tests[i].name,
		       hl_test_failures == before ? "ok" : "FAILED");
	}
	printf("%d checks, %d failures\n", hl_test_checks, hl_test_failures);
	return hl_test_failures ? 1 : 0;
}
