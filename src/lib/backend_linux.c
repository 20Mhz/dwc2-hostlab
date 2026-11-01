// SPDX-License-Identifier: MIT
/*
 * Linux usbfs backend.
 *
 * Every operation maps to exactly one usbfs ioctl, so the semantics are those
 * of drivers/usb/core/devio.c: USBDEVFS_CONTROL (sync, killed on timeout),
 * USBDEVFS_SUBMITURB / DISCARDURB (= usb_kill_urb) / REAPURBNDELAY, and
 * USBDEVFS_RESET (= usb_reset_device).
 */
#ifdef __linux__

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <time.h>
#include <unistd.h>

#include <linux/usbdevice_fs.h>

#include "backend_internal.h"

#define SYSFS_USB "/sys/bus/usb/devices"

struct linux_usb_dev {
	struct hl_usb_dev base;
	int fd;
	uint32_t usbfs_caps;
	struct hl_device_info info;
};

struct linux_urb {
	struct usbdevfs_urb urb;
	uint8_t buf[]; /* 8 setup bytes + wLength */
};

static struct linux_usb_dev *ldev(struct hl_usb_dev *d)
{
	return (struct linux_usb_dev *)d;
}

static uint64_t mono_ns(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (uint64_t)ts.tv_sec * 1000000000u + (uint64_t)ts.tv_nsec;
}

static int read_attr(const char *dev, const char *attr, char *buf, size_t len)
{
	char path[512];
	FILE *f;
	size_t n;

	snprintf(path, sizeof(path), SYSFS_USB "/%s/%s", dev, attr);
	f = fopen(path, "re");
	if (!f)
		return -1;
	n = fread(buf, 1, len - 1, f);
	fclose(f);
	buf[n] = '\0';
	while (n && (buf[n - 1] == '\n' || buf[n - 1] == ' '))
		buf[--n] = '\0';
	return 0;
}

static long read_attr_num(const char *dev, const char *attr, int base)
{
	char buf[32];

	if (read_attr(dev, attr, buf, sizeof(buf)))
		return -1;
	return strtol(buf, NULL, base);
}

static enum hl_speed speed_from_sysfs(const char *s)
{
	if (!strcmp(s, "1.5")) return HL_SPEED_LOW;
	if (!strcmp(s, "12")) return HL_SPEED_FULL;
	if (!strcmp(s, "480")) return HL_SPEED_HIGH;
	if (!strcmp(s, "53.3-480")) return HL_SPEED_WIRELESS;
	if (!strcmp(s, "5000")) return HL_SPEED_SUPER;
	if (!strcmp(s, "10000") || !strcmp(s, "20000")) return HL_SPEED_SUPER_PLUS;
	return HL_SPEED_UNKNOWN;
}

static int fill_from_sysfs(const char *name, struct hl_device_info *info)
{
	char buf[64];
	long bus = read_attr_num(name, "busnum", 10);
	long dev = read_attr_num(name, "devnum", 10);

	if (bus < 0 || dev < 0)
		return -1;
	memset(info, 0, sizeof(*info));
	info->busnum = (uint8_t)bus;
	info->devnum = (uint8_t)dev;
	info->vendor_id = (uint16_t)read_attr_num(name, "idVendor", 16);
	info->product_id = (uint16_t)read_attr_num(name, "idProduct", 16);
	info->device_class = (uint8_t)read_attr_num(name, "bDeviceClass", 16);
	if (!read_attr(name, "speed", buf, sizeof(buf)))
		info->speed = speed_from_sysfs(buf);
	if (!read_attr(name, "devpath", buf, sizeof(buf)) && strcmp(buf, "0")) {
		char *p = buf;

		while (*p && info->num_ports < sizeof(info->ports)) {
			info->ports[info->num_ports++] = (uint8_t)strtoul(p, &p, 10);
			if (*p == '.')
				p++;
		}
	}
	snprintf(info->path, sizeof(info->path), "/dev/bus/usb/%03ld/%03ld", bus, dev);
	return 0;
}

static int lx_enumerate(struct hl_backend *be, struct hl_device_info *out,
			size_t max, struct hl_error *err)
{
	struct dirent *de;
	DIR *dir;
	size_t n = 0;

	(void)be;
	dir = opendir(SYSFS_USB);
	if (!dir)
		return hl_fail(err, hl_status_from_errno(errno), errno, "enumerate",
			       "opendir %s: %s", SYSFS_USB, strerror(errno));
	while ((de = readdir(dir))) {
		struct hl_device_info info;

		/* Interfaces ("1-1:1.0") have no busnum attribute. */
		if (de->d_name[0] == '.' || strchr(de->d_name, ':'))
			continue;
		if (fill_from_sysfs(de->d_name, &info))
			continue;
		if (n < max)
			out[n] = info;
		n++;
	}
	closedir(dir);
	return (int)n;
}

/* Accepts /dev/bus/usb/BBB/DDD, BUS:DEV (decimal) or VVVV:PPPP (4 hex digits each). */
static int resolve_locator(const char *loc, char *path, size_t len,
			   struct hl_error *err)
{
	unsigned a, b;
	char ta[8], tb[8];

	if (loc[0] == '/') {
		snprintf(path, len, "%s", loc);
		return 0;
	}
	if (sscanf(loc, "%7[0-9a-fA-F]:%7[0-9a-fA-F]", ta, tb) != 2)
		return hl_fail(err, HL_ERR_INVALID, 0, "open",
			       "cannot parse locator '%s'", loc);
	if (strlen(ta) == 4 && strlen(tb) == 4) {
		struct hl_device_info devs[64];
		int n = lx_enumerate(NULL, devs, 64, err);

		a = (unsigned)strtoul(ta, NULL, 16);
		b = (unsigned)strtoul(tb, NULL, 16);
		for (int i = 0; i < n && i < 64; i++) {
			if (devs[i].vendor_id == a && devs[i].product_id == b) {
				snprintf(path, len, "%s", devs[i].path);
				return 0;
			}
		}
		return hl_fail(err, HL_ERR_NO_DEVICE, 0, "open",
			       "no device %04x:%04x", a, b);
	}
	a = (unsigned)strtoul(ta, NULL, 10);
	b = (unsigned)strtoul(tb, NULL, 10);
	snprintf(path, len, "/dev/bus/usb/%03u/%03u", a, b);
	return 0;
}

static int lx_open(struct hl_backend *be, const char *locator,
		   struct hl_usb_dev **out, struct hl_error *err)
{
	struct linux_usb_dev *d;
	char path[128];
	unsigned bus, dev;
	struct dirent *de;
	DIR *dir;

	(void)be;
	if (resolve_locator(locator, path, sizeof(path), err))
		return -1;
	d = calloc(1, sizeof(*d));
	if (!d)
		return hl_fail(err, HL_ERR_NOMEM, 0, "open", "out of memory");
	d->fd = open(path, O_RDWR | O_CLOEXEC);
	if (d->fd < 0) {
		int e = errno;

		free(d);
		return hl_fail(err, hl_status_from_errno(e), e, "open", "%s: %s",
			       path, strerror(e));
	}
	if (ioctl(d->fd, USBDEVFS_GET_CAPABILITIES, &d->usbfs_caps))
		d->usbfs_caps = 0;

	snprintf(d->info.path, sizeof(d->info.path), "%s", path);
	if (sscanf(path, "/dev/bus/usb/%u/%u", &bus, &dev) == 2 &&
	    (dir = opendir(SYSFS_USB))) {
		while ((de = readdir(dir))) {
			struct hl_device_info info;

			if (de->d_name[0] == '.' || strchr(de->d_name, ':'))
				continue;
			if (!fill_from_sysfs(de->d_name, &info) &&
			    info.busnum == bus && info.devnum == dev) {
				d->info = info;
				break;
			}
		}
		closedir(dir);
	}
	*out = &d->base;
	return 0;
}

static void lx_close(struct hl_usb_dev *dev)
{
	struct linux_usb_dev *d = ldev(dev);

	close(d->fd);
	free(d);
}

static int lx_get_info(struct hl_usb_dev *dev, struct hl_device_info *info,
		       struct hl_error *err)
{
	struct linux_usb_dev *d = ldev(dev);
	int speed;

	(void)err;
	*info = d->info;
	speed = ioctl(d->fd, USBDEVFS_GET_SPEED);
	if (speed >= 0 && speed <= HL_SPEED_SUPER_PLUS)
		info->speed = (enum hl_speed)speed;
#ifdef USBDEVFS_CONNINFO_EX
	if (d->usbfs_caps & USBDEVFS_CAP_CONNINFO_EX) {
		struct usbdevfs_conninfo_ex ci;

		if (!ioctl(d->fd, USBDEVFS_CONNINFO_EX(sizeof(ci)), &ci)) {
			info->busnum = (uint8_t)ci.busnum;
			info->devnum = (uint8_t)ci.devnum;
			info->num_ports = ci.num_ports;
			memcpy(info->ports, ci.ports, sizeof(info->ports));
		}
	}
#endif
	return 0;
}

static uint32_t lx_capabilities(struct hl_usb_dev *dev)
{
	struct linux_usb_dev *d = ldev(dev);
	uint32_t caps = HL_CAP_CONTROL_SYNC | HL_CAP_CONTROL_ASYNC | HL_CAP_CANCEL |
			HL_CAP_RESET_DEVICE | HL_CAP_SPEED;

	if (d->usbfs_caps & USBDEVFS_CAP_ZERO_PACKET)
		caps |= HL_CAP_ZERO_PACKET;
#ifdef USBDEVFS_CAP_MMAP
	if (d->usbfs_caps & USBDEVFS_CAP_MMAP)
		caps |= HL_CAP_MMAP_BUFFERS;
#endif
#ifdef USBDEVFS_CAP_CONNINFO_EX
	if (d->usbfs_caps & USBDEVFS_CAP_CONNINFO_EX)
		caps |= HL_CAP_CONNINFO_EX;
#endif
	return caps;
}

static int lx_control(struct hl_usb_dev *dev, const struct hl_setup *s,
		      uint8_t *data, unsigned timeout_ms, struct hl_error *err)
{
	struct usbdevfs_ctrltransfer ct = {
		.bRequestType = s->bmRequestType,
		.bRequest = s->bRequest,
		.wValue = s->wValue,
		.wIndex = s->wIndex,
		.wLength = s->wLength,
		.timeout = timeout_ms,
		.data = data,
	};
	int r = ioctl(ldev(dev)->fd, USBDEVFS_CONTROL, &ct);

	if (r < 0) {
		int e = errno;

		return hl_fail(err, hl_status_from_errno(e), e, "control",
			       "USBDEVFS_CONTROL: %s", strerror(e));
	}
	return r;
}

static int lx_submit(struct hl_usb_dev *dev, struct hl_xfer *x,
		     struct hl_error *err)
{
	const struct hl_setup *s = &x->setup;
	struct linux_urb *lu = calloc(1, sizeof(*lu) + 8u + s->wLength);

	if (!lu)
		return hl_fail(err, HL_ERR_NOMEM, 0, "submit", "out of memory");
	lu->buf[0] = s->bmRequestType;
	lu->buf[1] = s->bRequest;
	lu->buf[2] = (uint8_t)s->wValue;
	lu->buf[3] = (uint8_t)(s->wValue >> 8);
	lu->buf[4] = (uint8_t)s->wIndex;
	lu->buf[5] = (uint8_t)(s->wIndex >> 8);
	lu->buf[6] = (uint8_t)s->wLength;
	lu->buf[7] = (uint8_t)(s->wLength >> 8);
	if (!hl_setup_is_in(s) && s->wLength)
		memcpy(lu->buf + 8, x->data, s->wLength);
	lu->urb.type = USBDEVFS_URB_TYPE_CONTROL;
	lu->urb.endpoint = 0;
	lu->urb.buffer = lu->buf;
	lu->urb.buffer_length = 8 + s->wLength;
	lu->urb.usercontext = x;

	x->t_submit_ns = mono_ns();
	if (ioctl(ldev(dev)->fd, USBDEVFS_SUBMITURB, &lu->urb)) {
		int e = errno;

		free(lu);
		return hl_fail(err, hl_status_from_errno(e), e, "submit",
			       "USBDEVFS_SUBMITURB: %s", strerror(e));
	}
	x->backend_priv = lu;
	return 0;
}

static int lx_cancel(struct hl_usb_dev *dev, struct hl_xfer *x,
		     struct hl_error *err)
{
	struct linux_urb *lu = x->backend_priv;
	uint64_t t = mono_ns();

	if (ioctl(ldev(dev)->fd, USBDEVFS_DISCARDURB, &lu->urb)) {
		int e = errno;

		/* usbfs only finds URBs still on the pending list
		 * (async_getpending, devio.c:2014). */
		if (e == EINVAL)
			return hl_fail(err, HL_ERR_NOT_PENDING, e, "cancel",
				       "transfer already completed");
		return hl_fail(err, hl_status_from_errno(e), e, "cancel",
			       "USBDEVFS_DISCARDURB: %s", strerror(e));
	}
	x->t_cancel_ns = t;
	return 0;
}

static int lx_reap(struct hl_usb_dev *dev, int timeout_ms, struct hl_xfer **out,
		   struct hl_error *err)
{
	struct linux_usb_dev *d = ldev(dev);
	struct usbdevfs_urb *urb;
	struct linux_urb *lu;
	struct hl_xfer *x;

	for (;;) {
		struct pollfd pfd = { .fd = d->fd, .events = POLLOUT };
		int r;

		if (!ioctl(d->fd, USBDEVFS_REAPURBNDELAY, &urb))
			break;
		if (errno != EAGAIN)
			return hl_fail(err, hl_status_from_errno(errno), errno, "reap",
				       "USBDEVFS_REAPURBNDELAY: %s", strerror(errno));
		if (timeout_ms == 0)
			return hl_fail(err, HL_ERR_AGAIN, 0, "reap", "no completed transfer");
		/* usbdev_poll reports EPOLLOUT when a completion is queued. */
		r = poll(&pfd, 1, timeout_ms);
		if (r == 0)
			return hl_fail(err, HL_ERR_AGAIN, 0, "reap",
				       "no completion within %d ms", timeout_ms);
		if (r < 0 && errno != EINTR)
			return hl_fail(err, hl_status_from_errno(errno), errno, "reap",
				       "poll: %s", strerror(errno));
		if (r > 0 && (pfd.revents & (POLLHUP | POLLERR)) && !(pfd.revents & POLLOUT))
			return hl_fail(err, HL_ERR_NO_DEVICE, 0, "reap", "device gone");
	}

	lu = (struct linux_urb *)urb;
	x = urb->usercontext;
	x->t_reap_ns = mono_ns();
	x->urb_status = urb->status;
	x->status = hl_status_from_errno(urb->status);
	x->actual_length = urb->actual_length > 0 ? (uint32_t)urb->actual_length : 0;
	if (hl_setup_is_in(&x->setup) && x->actual_length)
		memcpy(x->data, lu->buf + 8, x->actual_length);
	free(lu);
	*out = x;
	return 0;
}

static int lx_reset(struct hl_usb_dev *dev, struct hl_error *err)
{
	if (ioctl(ldev(dev)->fd, USBDEVFS_RESET, 0)) {
		int e = errno;

		return hl_fail(err, hl_status_from_errno(e), e, "reset",
			       "USBDEVFS_RESET: %s", strerror(e));
	}
	return 0;
}

static uint64_t lx_now(struct hl_usb_dev *dev)
{
	(void)dev;
	return mono_ns();
}

const struct hl_backend_ops hl_linux_usbfs_ops = {
	.name = "linux-usbfs",
	.enumerate = lx_enumerate,
	.open = lx_open,
	.close = lx_close,
	.get_info = lx_get_info,
	.capabilities = lx_capabilities,
	.control = lx_control,
	.submit = lx_submit,
	.cancel = lx_cancel,
	.reap = lx_reap,
	.reset = lx_reset,
	.now_ns = lx_now,
};

#endif /* __linux__ */
