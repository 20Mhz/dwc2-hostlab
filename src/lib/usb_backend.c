// SPDX-License-Identifier: MIT
/*
 * Backend-independent entry points: argument validation, bookkeeping of
 * in-flight transfers and dispatch to the selected backend.
 */
#include <errno.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#include "backend_internal.h"
#include "hostlab/mock_backend.h"

unsigned hl_usb_api_version(void)
{
	return (HL_USB_API_MAJOR << 16) | HL_USB_API_MINOR;
}

const char *hl_status_name(enum hl_status st)
{
	switch (st) {
	case HL_OK: return "ok";
	case HL_ERR_INVALID: return "invalid";
	case HL_ERR_NO_DEVICE: return "no_device";
	case HL_ERR_ACCESS: return "access";
	case HL_ERR_BUSY: return "busy";
	case HL_ERR_TIMEOUT: return "timeout";
	case HL_ERR_STALL: return "stall";
	case HL_ERR_PROTOCOL: return "protocol";
	case HL_ERR_OVERFLOW: return "overflow";
	case HL_ERR_SHORT: return "short";
	case HL_ERR_CANCELLED: return "cancelled";
	case HL_ERR_NOT_PENDING: return "not_pending";
	case HL_ERR_IO: return "io";
	case HL_ERR_NOMEM: return "nomem";
	case HL_ERR_UNSUPPORTED: return "unsupported";
	case HL_ERR_AGAIN: return "again";
	case HL_ERR_OTHER: return "other";
	}
	return "unknown";
}

const char *hl_speed_name(enum hl_speed s)
{
	switch (s) {
	case HL_SPEED_LOW: return "low";
	case HL_SPEED_FULL: return "full";
	case HL_SPEED_HIGH: return "high";
	case HL_SPEED_WIRELESS: return "wireless";
	case HL_SPEED_SUPER: return "super";
	case HL_SPEED_SUPER_PLUS: return "super_plus";
	case HL_SPEED_UNKNOWN: break;
	}
	return "unknown";
}

enum hl_status hl_status_from_errno(int e)
{
	if (e < 0)
		e = -e;
	switch (e) {
	case 0: return HL_OK;
	case EINVAL: return HL_ERR_INVALID;
	case ENODEV:
	case ESHUTDOWN: return HL_ERR_NO_DEVICE;
	case EACCES:
	case EPERM: return HL_ERR_ACCESS;
	case EBUSY: return HL_ERR_BUSY;
	case ETIMEDOUT: return HL_ERR_TIMEOUT;
	case EPIPE: return HL_ERR_STALL;
	case EPROTO:
	case EILSEQ: return HL_ERR_PROTOCOL;
	case EOVERFLOW: return HL_ERR_OVERFLOW;
#ifdef EREMOTEIO
	case EREMOTEIO: return HL_ERR_SHORT;
#endif
	case ENOENT:
	case ECONNRESET: return HL_ERR_CANCELLED;
	case EIO: return HL_ERR_IO;
	case ENOMEM: return HL_ERR_NOMEM;
	case EAGAIN: return HL_ERR_AGAIN;
	case ENOTTY:
	case ENOSYS: return HL_ERR_UNSUPPORTED;
	}
	return HL_ERR_OTHER;
}

int hl_fail(struct hl_error *err, enum hl_status st, int sys_errno,
	    const char *op, const char *fmt, ...)
{
	if (err) {
		va_list ap;

		err->status = st;
		err->sys_errno = sys_errno;
		err->op = op;
		va_start(ap, fmt);
		vsnprintf(err->msg, sizeof(err->msg), fmt, ap);
		va_end(ap);
	}
	return -1;
}

struct hl_backend *hl_backend_get(const char *name)
{
#ifdef __linux__
	static struct hl_backend linux_be = { &hl_linux_usbfs_ops, NULL };
#endif

	if (!name)
		return NULL;
#ifdef __linux__
	if (!strcmp(name, "linux-usbfs"))
		return &linux_be;
#endif
	if (!strcmp(name, "mock"))
		return hl_mock_default();
	return NULL;
}

const char *hl_backend_name(const struct hl_backend *be)
{
	return be ? be->ops->name : "(null)";
}

int hl_usb_enumerate(struct hl_backend *be, struct hl_device_info *out,
		     size_t max, struct hl_error *err)
{
	hl_clear_error(err);
	if (!be || (max && !out))
		return hl_fail(err, HL_ERR_INVALID, 0, "enumerate", "bad argument");
	return be->ops->enumerate(be, out, max, err);
}

int hl_usb_open(struct hl_backend *be, const char *locator,
		struct hl_usb_dev **out, struct hl_error *err)
{
	hl_clear_error(err);
	if (!be || !locator || !out)
		return hl_fail(err, HL_ERR_INVALID, 0, "open", "bad argument");
	*out = NULL;
	if (be->ops->open(be, locator, out, err))
		return -1;
	(*out)->be = be;
	(*out)->in_flight = 0;
	return 0;
}

void hl_usb_close(struct hl_usb_dev *dev)
{
	if (dev)
		dev->be->ops->close(dev);
}

int hl_usb_get_info(struct hl_usb_dev *dev, struct hl_device_info *info,
		    struct hl_error *err)
{
	hl_clear_error(err);
	if (!dev || !info)
		return hl_fail(err, HL_ERR_INVALID, 0, "get_info", "bad argument");
	memset(info, 0, sizeof(*info));
	return dev->be->ops->get_info(dev, info, err);
}

uint32_t hl_usb_capabilities(struct hl_usb_dev *dev)
{
	return dev ? dev->be->ops->capabilities(dev) : 0;
}

static int check_setup(const struct hl_setup *s, const uint8_t *data,
		       const char *op, struct hl_error *err)
{
	if (!s)
		return hl_fail(err, HL_ERR_INVALID, 0, op, "setup is NULL");
	if (s->wLength && !data)
		return hl_fail(err, HL_ERR_INVALID, 0, op,
			       "wLength=%u but data buffer is NULL", s->wLength);
	return 0;
}

int hl_usb_control(struct hl_usb_dev *dev, const struct hl_setup *setup,
		   uint8_t *data, unsigned timeout_ms, struct hl_error *err)
{
	hl_clear_error(err);
	if (!dev)
		return hl_fail(err, HL_ERR_INVALID, 0, "control", "dev is NULL");
	if (check_setup(setup, data, "control", err))
		return -1;
	return dev->be->ops->control(dev, setup, data, timeout_ms, err);
}

int hl_usb_submit_control(struct hl_usb_dev *dev, struct hl_xfer *x,
			  struct hl_error *err)
{
	hl_clear_error(err);
	if (!dev || !x)
		return hl_fail(err, HL_ERR_INVALID, 0, "submit", "bad argument");
	if (x->backend_priv)
		return hl_fail(err, HL_ERR_INVALID, 0, "submit",
			       "transfer is already in flight");
	if (check_setup(&x->setup, x->data, "submit", err))
		return -1;
	x->status = HL_OK;
	x->urb_status = 0;
	x->actual_length = 0;
	x->t_submit_ns = x->t_cancel_ns = x->t_reap_ns = 0;
	if (dev->be->ops->submit(dev, x, err))
		return -1;
	dev->in_flight++;
	return 0;
}

int hl_usb_cancel(struct hl_usb_dev *dev, struct hl_xfer *x,
		  struct hl_error *err)
{
	hl_clear_error(err);
	if (!dev || !x)
		return hl_fail(err, HL_ERR_INVALID, 0, "cancel", "bad argument");
	if (!x->backend_priv)
		return hl_fail(err, HL_ERR_INVALID, 0, "cancel",
			       "transfer is not in flight");
	return dev->be->ops->cancel(dev, x, err);
}

int hl_usb_reap(struct hl_usb_dev *dev, int timeout_ms, struct hl_xfer **out,
		struct hl_error *err)
{
	hl_clear_error(err);
	if (!dev || !out)
		return hl_fail(err, HL_ERR_INVALID, 0, "reap", "bad argument");
	*out = NULL;
	if (!dev->in_flight)
		return hl_fail(err, HL_ERR_AGAIN, 0, "reap", "nothing in flight");
	if (dev->be->ops->reap(dev, timeout_ms, out, err))
		return -1;
	(*out)->backend_priv = NULL;
	dev->in_flight--;
	return 0;
}

int hl_usb_reset_device(struct hl_usb_dev *dev, struct hl_error *err)
{
	hl_clear_error(err);
	if (!dev)
		return hl_fail(err, HL_ERR_INVALID, 0, "reset", "dev is NULL");
	return dev->be->ops->reset(dev, err);
}

uint64_t hl_usb_now_ns(struct hl_usb_dev *dev)
{
	return dev ? dev->be->ops->now_ns(dev) : 0;
}
