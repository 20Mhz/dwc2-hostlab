// SPDX-License-Identifier: MIT
/* Deterministic mock backend; see include/hostlab/mock_backend.h. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "backend_internal.h"
#include "hostlab/mock_backend.h"

#define MOCK_MAX_DEVICES   8
#define MOCK_MAX_RESPONSES 64
#define MOCK_MAX_PENDING   32
#define MOCK_MAX_LOG       512
#define MOCK_MAX_DESC      256
#define MOCK_MAX_PAYLOAD   256

/* Simulated time for a port reset plus re-enumeration. dwc2 holds reset for
 * 50 ms (dwc2/hcd.c:3702); the remainder is a nominal enumeration cost. */
#define MOCK_RESET_NS (60ull * 1000 * 1000)

struct mock_device {
	int present;
	struct hl_device_info info;
	uint8_t dev_desc[MOCK_MAX_DESC];
	size_t dev_desc_len;
	uint8_t cfg_desc[MOCK_MAX_DESC];
	size_t cfg_desc_len;
	unsigned resets;
};

struct mock_response {
	int used;
	uint8_t devnum;
	struct hl_mock_response r;
	uint8_t payload[MOCK_MAX_PAYLOAD];
	unsigned remaining;
};

struct mock_pending {
	struct hl_xfer *x;
	uint64_t t_hcd_done; /* HCD has completed the URB */
	uint64_t t_done;     /* giveback done; reapable, no longer cancellable */
	size_t log_index;
};

struct mock_state {
	uint64_t now;
	uint64_t default_latency;
	uint64_t giveback_delay;
	struct mock_device dev[MOCK_MAX_DEVICES];
	struct mock_response resp[MOCK_MAX_RESPONSES];
	struct hl_mock_log_entry log[MOCK_MAX_LOG];
	size_t log_len;
};

struct mock_usb_dev {
	struct hl_usb_dev base;
	struct mock_state *st;
	struct mock_device *md;
	struct mock_pending pending[MOCK_MAX_PENDING];
	unsigned npending;
};

static struct mock_state *state_of(struct hl_backend *be)
{
	return be->priv;
}

static struct mock_usb_dev *mdev(struct hl_usb_dev *d)
{
	return (struct mock_usb_dev *)d;
}

struct hl_backend *hl_mock_create(void)
{
	struct hl_backend *be = calloc(1, sizeof(*be));
	struct mock_state *st = calloc(1, sizeof(*st));

	if (!be || !st) {
		free(be);
		free(st);
		return NULL;
	}
	st->default_latency = 1000 * 1000; /* 1 ms */
	be->ops = &hl_mock_ops;
	be->priv = st;
	return be;
}

void hl_mock_destroy(struct hl_backend *be)
{
	if (be) {
		free(be->priv);
		free(be);
	}
}

struct hl_backend *hl_mock_default(void)
{
	static struct hl_backend *be;
	static const uint8_t dev_desc[18] = {
		18, 1, 0x00, 0x02, 0xff, 0x00, 0x00, 64,
		0x09, 0x12, 0x01, 0x00, 0x00, 0x01, 0, 0, 0, 1,
	};
	static const uint8_t cfg_desc[18] = {
		9, 2, 18, 0, 1, 1, 0, 0x80, 50,
		9, 4, 0, 0, 0, 0xff, 0x00, 0x00, 0,
	};

	if (!be) {
		struct hl_device_info info = {
			.busnum = 1, .devnum = 2,
			.vendor_id = 0x1209, .product_id = 0x0001,
			.device_class = 0xff, .speed = HL_SPEED_HIGH,
			.num_ports = 1, .ports = { 1 },
		};

		be = hl_mock_create();
		if (be)
			hl_mock_add_device(be, &info, dev_desc, sizeof(dev_desc),
					   cfg_desc, sizeof(cfg_desc));
	}
	return be;
}

int hl_mock_add_device(struct hl_backend *be, const struct hl_device_info *info,
		       const uint8_t *device_desc, size_t device_desc_len,
		       const uint8_t *config_desc, size_t config_desc_len)
{
	struct mock_state *st = state_of(be);
	struct mock_device *md = NULL;

	if (device_desc_len > MOCK_MAX_DESC || config_desc_len > MOCK_MAX_DESC)
		return -1;
	for (int i = 0; i < MOCK_MAX_DEVICES; i++) {
		if (!st->dev[i].present) {
			md = &st->dev[i];
			break;
		}
	}
	if (!md)
		return -1;
	memset(md, 0, sizeof(*md));
	md->present = 1;
	md->info = *info;
	snprintf(md->info.path, sizeof(md->info.path), "mock:%u:%u",
		 info->busnum, info->devnum);
	if (device_desc_len)
		memcpy(md->dev_desc, device_desc, device_desc_len);
	md->dev_desc_len = device_desc_len;
	if (config_desc_len)
		memcpy(md->cfg_desc, config_desc, config_desc_len);
	md->cfg_desc_len = config_desc_len;
	return 0;
}

int hl_mock_push_response(struct hl_backend *be, uint8_t devnum,
			  const struct hl_mock_response *r)
{
	struct mock_state *st = state_of(be);

	if (r->data_len > MOCK_MAX_PAYLOAD || (r->data_len && !r->data))
		return -1;
	for (int i = 0; i < MOCK_MAX_RESPONSES; i++) {
		struct mock_response *m = &st->resp[i];

		if (m->used)
			continue;
		m->used = 1;
		m->devnum = devnum;
		m->r = *r;
		if (r->data_len)
			memcpy(m->payload, r->data, r->data_len);
		m->r.data = m->payload;
		m->remaining = r->repeat ? r->repeat : 1;
		return 0;
	}
	return -1;
}

void hl_mock_set_default_latency(struct hl_backend *be, uint64_t ns)
{
	state_of(be)->default_latency = ns;
}

void hl_mock_set_giveback_delay(struct hl_backend *be, uint64_t ns)
{
	state_of(be)->giveback_delay = ns;
}

void hl_mock_advance(struct hl_backend *be, uint64_t ns)
{
	state_of(be)->now += ns;
}

uint64_t hl_mock_now(struct hl_backend *be)
{
	return state_of(be)->now;
}

size_t hl_mock_log_count(struct hl_backend *be)
{
	return state_of(be)->log_len;
}

const struct hl_mock_log_entry *hl_mock_log_get(struct hl_backend *be, size_t i)
{
	struct mock_state *st = state_of(be);

	return i < st->log_len ? &st->log[i] : NULL;
}

unsigned hl_mock_reset_count(struct hl_backend *be, uint8_t devnum)
{
	struct mock_state *st = state_of(be);

	for (int i = 0; i < MOCK_MAX_DEVICES; i++)
		if (st->dev[i].present && st->dev[i].info.devnum == devnum)
			return st->dev[i].resets;
	return 0;
}

static int matches(const struct hl_mock_response *r, const struct hl_setup *s)
{
	const struct hl_setup *m = &r->match;

	return (!(r->match_mask & HL_MOCK_MATCH_REQTYPE) || m->bmRequestType == s->bmRequestType) &&
	       (!(r->match_mask & HL_MOCK_MATCH_REQUEST) || m->bRequest == s->bRequest) &&
	       (!(r->match_mask & HL_MOCK_MATCH_VALUE) || m->wValue == s->wValue) &&
	       (!(r->match_mask & HL_MOCK_MATCH_INDEX) || m->wIndex == s->wIndex) &&
	       (!(r->match_mask & HL_MOCK_MATCH_LENGTH) || m->wLength == s->wLength);
}

/* Decide the outcome of a request: status, IN payload, latency. */
static void resolve(struct mock_usb_dev *d, const struct hl_setup *s,
		    enum hl_status *status, const uint8_t **payload,
		    size_t *payload_len, uint64_t *latency)
{
	struct mock_state *st = d->st;

	*latency = st->default_latency;
	*payload = NULL;
	*payload_len = 0;

	for (int i = 0; i < MOCK_MAX_RESPONSES; i++) {
		struct mock_response *m = &st->resp[i];

		if (!m->used || m->devnum != d->md->info.devnum || !matches(&m->r, s))
			continue;
		*status = m->r.status;
		*payload = m->payload;
		*payload_len = m->r.data_len;
		*latency = m->r.latency_ns;
		if (m->remaining != HL_MOCK_REPEAT_FOREVER && --m->remaining == 0)
			m->used = 0;
		return;
	}

	*status = HL_ERR_STALL;
	if (s->bmRequestType == 0x80 && s->bRequest == 6) { /* GET_DESCRIPTOR */
		switch (s->wValue >> 8) {
		case 1:
			*payload = d->md->dev_desc;
			*payload_len = d->md->dev_desc_len;
			break;
		case 2:
			*payload = d->md->cfg_desc;
			*payload_len = d->md->cfg_desc_len;
			break;
		}
		if (*payload_len)
			*status = HL_OK;
	} else if ((s->bmRequestType & 0xfc) == 0x80 && s->bRequest == 0 &&
		   s->wLength == 2) { /* GET_STATUS */
		static const uint8_t zero[2];

		*payload = zero;
		*payload_len = sizeof(zero);
		*status = HL_OK;
	}
}

static uint32_t deliver(const struct hl_setup *s, uint8_t *data,
			enum hl_status status, const uint8_t *payload,
			size_t payload_len)
{
	size_t n;

	if (status != HL_OK)
		return 0;
	if (!hl_setup_is_in(s))
		return s->wLength; /* OUT: device accepted everything */
	n = payload_len < s->wLength ? payload_len : s->wLength;
	if (n)
		memcpy(data, payload, n);
	return (uint32_t)n;
}

static size_t log_append(struct mock_state *st, const struct hl_mock_log_entry *e)
{
	if (st->log_len < MOCK_MAX_LOG)
		st->log[st->log_len] = *e;
	return st->log_len++;
}

static struct hl_mock_log_entry *log_at(struct mock_state *st, size_t i)
{
	return i < MOCK_MAX_LOG ? &st->log[i] : NULL;
}

static struct mock_device *find_device(struct mock_state *st, unsigned bus,
				       unsigned dev)
{
	for (int i = 0; i < MOCK_MAX_DEVICES; i++)
		if (st->dev[i].present && st->dev[i].info.busnum == bus &&
		    st->dev[i].info.devnum == dev)
			return &st->dev[i];
	return NULL;
}

static int mock_enumerate(struct hl_backend *be, struct hl_device_info *out,
			  size_t max, struct hl_error *err)
{
	struct mock_state *st = state_of(be);
	size_t n = 0;

	(void)err;
	for (int i = 0; i < MOCK_MAX_DEVICES; i++) {
		if (!st->dev[i].present)
			continue;
		if (n < max)
			out[n] = st->dev[i].info;
		n++;
	}
	return (int)n;
}

static int parse_locator(const char *loc, unsigned *bus, unsigned *dev)
{
	if (!strncmp(loc, "mock:", 5))
		loc += 5;
	if (sscanf(loc, "%u:%u", bus, dev) == 2)
		return 0;
	if (sscanf(loc, "/dev/bus/usb/%u/%u", bus, dev) == 2)
		return 0;
	return -1;
}

static int mock_open(struct hl_backend *be, const char *locator,
		     struct hl_usb_dev **out, struct hl_error *err)
{
	struct mock_state *st = state_of(be);
	struct mock_usb_dev *d;
	struct mock_device *md;
	unsigned bus, dev;

	if (parse_locator(locator, &bus, &dev))
		return hl_fail(err, HL_ERR_INVALID, 0, "open",
			       "cannot parse locator '%s'", locator);
	md = find_device(st, bus, dev);
	if (!md)
		return hl_fail(err, HL_ERR_NO_DEVICE, 0, "open",
			       "no mock device %u:%u", bus, dev);
	d = calloc(1, sizeof(*d));
	if (!d)
		return hl_fail(err, HL_ERR_NOMEM, 0, "open", "out of memory");
	d->st = st;
	d->md = md;
	*out = &d->base;
	return 0;
}

static void mock_close(struct hl_usb_dev *dev)
{
	free(mdev(dev));
}

static int mock_get_info(struct hl_usb_dev *dev, struct hl_device_info *info,
			 struct hl_error *err)
{
	(void)err;
	*info = mdev(dev)->md->info;
	return 0;
}

static uint32_t mock_capabilities(struct hl_usb_dev *dev)
{
	(void)dev;
	return HL_CAP_CONTROL_SYNC | HL_CAP_CONTROL_ASYNC | HL_CAP_CANCEL |
	       HL_CAP_RESET_DEVICE | HL_CAP_SPEED | HL_CAP_CONNINFO_EX |
	       HL_CAP_VIRTUAL_CLOCK;
}

static int mock_control(struct hl_usb_dev *dev, const struct hl_setup *setup,
			uint8_t *data, unsigned timeout_ms, struct hl_error *err)
{
	struct mock_usb_dev *d = mdev(dev);
	struct mock_state *st = d->st;
	struct hl_mock_log_entry e = { .devnum = d->md->info.devnum, .setup = *setup };
	const uint8_t *payload;
	size_t payload_len;
	uint64_t latency, timeout_ns = (uint64_t)timeout_ms * 1000000u;
	enum hl_status status;

	resolve(d, setup, &status, &payload, &payload_len, &latency);
	e.t_submit_ns = st->now;
	if (timeout_ms && latency > timeout_ns) {
		st->now += timeout_ns;
		e.t_done_ns = st->now;
		e.status = HL_ERR_TIMEOUT;
		log_append(st, &e);
		return hl_fail(err, HL_ERR_TIMEOUT, 0, "control",
			       "timed out after %u ms", timeout_ms);
	}
	st->now += latency;
	e.t_done_ns = st->now;
	e.status = status;
	e.actual_length = deliver(setup, data, status, payload, payload_len);
	log_append(st, &e);
	if (status != HL_OK)
		return hl_fail(err, status, 0, "control", "mock device returned %s",
			       hl_status_name(status));
	return (int)e.actual_length;
}

static int mock_submit(struct hl_usb_dev *dev, struct hl_xfer *x,
		       struct hl_error *err)
{
	struct mock_usb_dev *d = mdev(dev);
	struct mock_state *st = d->st;
	struct hl_mock_log_entry e = {
		.devnum = d->md->info.devnum, .setup = x->setup, .async = 1,
	};
	struct mock_pending *p;
	const uint8_t *payload;
	size_t payload_len;
	uint64_t latency;
	enum hl_status status;

	if (d->npending >= MOCK_MAX_PENDING)
		return hl_fail(err, HL_ERR_NOMEM, 0, "submit", "too many pending transfers");
	resolve(d, &x->setup, &status, &payload, &payload_len, &latency);
	x->t_submit_ns = st->now;
	x->status = status;
	x->urb_status = 0;
	x->actual_length = deliver(&x->setup, x->data, status, payload, payload_len);
	e.t_submit_ns = st->now;
	e.t_done_ns = st->now + latency + st->giveback_delay;
	e.status = status;
	e.actual_length = x->actual_length;

	p = &d->pending[d->npending++];
	p->x = x;
	p->t_hcd_done = st->now + latency;
	p->t_done = p->t_hcd_done + st->giveback_delay;
	p->log_index = log_append(st, &e);
	x->backend_priv = p;
	return 0;
}

static int mock_cancel(struct hl_usb_dev *dev, struct hl_xfer *x,
		       struct hl_error *err)
{
	struct mock_usb_dev *d = mdev(dev);
	struct mock_pending *p = x->backend_priv;
	struct hl_mock_log_entry *e;

	if (p->t_done <= d->st->now)
		return hl_fail(err, HL_ERR_NOT_PENDING, 0, "cancel",
			       "transfer already completed");
	x->t_cancel_ns = d->st->now;
	/* Completed by the HCD but not yet given back: usbfs still accepts the
	 * discard, usb_kill_urb cannot unlink it, the URB keeps its real status. */
	if (p->t_hcd_done <= d->st->now)
		return 0;
	p->t_done = d->st->now;
	x->status = HL_ERR_CANCELLED;
	x->actual_length = 0;
	e = log_at(d->st, p->log_index);
	if (e) {
		e->cancelled = 1;
		e->status = HL_ERR_CANCELLED;
		e->actual_length = 0;
		e->t_done_ns = d->st->now;
	}
	return 0;
}

static int mock_reap(struct hl_usb_dev *dev, int timeout_ms, struct hl_xfer **out,
		     struct hl_error *err)
{
	struct mock_usb_dev *d = mdev(dev);
	struct mock_state *st = d->st;
	unsigned best = 0;

	for (unsigned i = 1; i < d->npending; i++)
		if (d->pending[i].t_done < d->pending[best].t_done)
			best = i;

	if (d->pending[best].t_done > st->now) {
		uint64_t wait = d->pending[best].t_done - st->now;

		if (timeout_ms == 0)
			return hl_fail(err, HL_ERR_AGAIN, 0, "reap", "no completed transfer");
		if (timeout_ms > 0 && wait > (uint64_t)timeout_ms * 1000000u) {
			st->now += (uint64_t)timeout_ms * 1000000u;
			return hl_fail(err, HL_ERR_AGAIN, 0, "reap",
				       "no completion within %d ms", timeout_ms);
		}
		st->now = d->pending[best].t_done;
	}

	*out = d->pending[best].x;
	(*out)->t_reap_ns = st->now;
	if ((*out)->status == HL_ERR_CANCELLED)
		(*out)->urb_status = -2; /* -ENOENT, as reported by usb_kill_urb */
	if (best != --d->npending) {
		d->pending[best] = d->pending[d->npending];
		d->pending[best].x->backend_priv = &d->pending[best];
	}
	return 0;
}

static int mock_reset(struct hl_usb_dev *dev, struct hl_error *err)
{
	struct mock_usb_dev *d = mdev(dev);

	/* What usbfs does with URBs still pending across USBDEVFS_RESET is not
	 * yet verified on hardware (H6), so the mock refuses to model it. */
	if (d->npending)
		return hl_fail(err, HL_ERR_UNSUPPORTED, 0, "reset",
			       "mock does not model reset with pending transfers");
	d->st->now += MOCK_RESET_NS;
	d->md->resets++;
	return 0;
}

static uint64_t mock_now(struct hl_usb_dev *dev)
{
	return mdev(dev)->st->now;
}

const struct hl_backend_ops hl_mock_ops = {
	.name = "mock",
	.enumerate = mock_enumerate,
	.open = mock_open,
	.close = mock_close,
	.get_info = mock_get_info,
	.capabilities = mock_capabilities,
	.control = mock_control,
	.submit = mock_submit,
	.cancel = mock_cancel,
	.reap = mock_reap,
	.reset = mock_reset,
	.now_ns = mock_now,
};
