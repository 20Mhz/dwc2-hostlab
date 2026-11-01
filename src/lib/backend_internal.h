/* SPDX-License-Identifier: MIT */
#ifndef HOSTLAB_BACKEND_INTERNAL_H
#define HOSTLAB_BACKEND_INTERNAL_H

#include "hostlab/usb_backend.h"

struct hl_backend_ops {
	const char *name;
	int (*enumerate)(struct hl_backend *be, struct hl_device_info *out,
			 size_t max, struct hl_error *err);
	int (*open)(struct hl_backend *be, const char *locator,
		    struct hl_usb_dev **out, struct hl_error *err);
	void (*close)(struct hl_usb_dev *dev);
	int (*get_info)(struct hl_usb_dev *dev, struct hl_device_info *info,
			struct hl_error *err);
	uint32_t (*capabilities)(struct hl_usb_dev *dev);
	int (*control)(struct hl_usb_dev *dev, const struct hl_setup *setup,
		       uint8_t *data, unsigned timeout_ms, struct hl_error *err);
	int (*submit)(struct hl_usb_dev *dev, struct hl_xfer *x,
		      struct hl_error *err);
	int (*cancel)(struct hl_usb_dev *dev, struct hl_xfer *x,
		      struct hl_error *err);
	int (*reap)(struct hl_usb_dev *dev, int timeout_ms, struct hl_xfer **out,
		    struct hl_error *err);
	int (*reset)(struct hl_usb_dev *dev, struct hl_error *err);
	uint64_t (*now_ns)(struct hl_usb_dev *dev);
};

struct hl_backend {
	const struct hl_backend_ops *ops;
	void *priv;
};

/* Every backend's device struct starts with this. */
struct hl_usb_dev {
	struct hl_backend *be;
	unsigned in_flight; /* submitted, not yet reaped */
};

/* Fill *err (if non-NULL) and return -1. */
int hl_fail(struct hl_error *err, enum hl_status st, int sys_errno,
	    const char *op, const char *fmt, ...)
	__attribute__((format(printf, 5, 6)));

static inline void hl_clear_error(struct hl_error *err)
{
	if (err) {
		err->status = HL_OK;
		err->sys_errno = 0;
		err->op = "";
		err->msg[0] = '\0';
	}
}

/* Setup direction helper: true for device-to-host. */
static inline int hl_setup_is_in(const struct hl_setup *s)
{
	return (s->bmRequestType & 0x80) != 0;
}

extern const struct hl_backend_ops hl_mock_ops;
#ifdef __linux__
extern const struct hl_backend_ops hl_linux_usbfs_ops;
#endif

#endif /* HOSTLAB_BACKEND_INTERNAL_H */
