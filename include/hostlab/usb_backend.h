/* SPDX-License-Identifier: MIT */
/*
 * hostlab USB transport abstraction (provisional, API v0.1).
 *
 * The operations mirror what Linux actually lets userspace express (see
 * docs/usb-control-transfers.md §7): whole control transfers, whole-URB
 * cancellation, device reset. There is deliberately no per-phase or per-packet
 * operation: no backend over usbfs can provide one, and the DWC2 hardware
 * generates tokens itself.
 *
 * Thread safety: a device handle must be used from one thread at a time.
 */
#ifndef HOSTLAB_USB_BACKEND_H
#define HOSTLAB_USB_BACKEND_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define HL_USB_API_MAJOR 0
#define HL_USB_API_MINOR 1

/* Runtime version, for callers linked against a different build. */
unsigned hl_usb_api_version(void); /* (major << 16) | minor */

enum hl_status {
	HL_OK = 0,
	HL_ERR_INVALID,     /* bad argument */
	HL_ERR_NO_DEVICE,   /* device gone (ENODEV, ESHUTDOWN) */
	HL_ERR_ACCESS,      /* EACCES, EPERM */
	HL_ERR_BUSY,        /* interface claimed by another driver */
	HL_ERR_TIMEOUT,     /* caller's timeout expired; URB was killed */
	HL_ERR_STALL,       /* EPIPE: device returned STALL */
	HL_ERR_PROTOCOL,    /* EPROTO, EILSEQ: transaction errors (3 strikes in dwc2) */
	HL_ERR_OVERFLOW,    /* EOVERFLOW: babble */
	HL_ERR_SHORT,       /* EREMOTEIO: short transfer with SHORT_NOT_OK */
	HL_ERR_CANCELLED,   /* ENOENT, ECONNRESET: URB was unlinked */
	HL_ERR_NOT_PENDING, /* cancel of a transfer that already completed */
	HL_ERR_IO,          /* EIO and anything else from the HCD */
	HL_ERR_NOMEM,
	HL_ERR_UNSUPPORTED, /* backend cannot perform this operation */
	HL_ERR_AGAIN,       /* nothing to reap yet */
	HL_ERR_OTHER,
};

const char *hl_status_name(enum hl_status st);

/* Structured error detail; filled by every call that can fail. */
struct hl_error {
	enum hl_status status;
	int sys_errno;     /* 0 if not from a syscall */
	const char *op;    /* static string naming the failed operation */
	char msg[160];
};

/* Map a negative or positive errno from usbfs / URB status. */
enum hl_status hl_status_from_errno(int err);

/* Capabilities reported by a backend for an open device. */
enum hl_cap {
	HL_CAP_CONTROL_SYNC   = 1u << 0,
	HL_CAP_CONTROL_ASYNC  = 1u << 1,
	HL_CAP_CANCEL         = 1u << 2,
	HL_CAP_RESET_DEVICE   = 1u << 3,
	HL_CAP_SPEED          = 1u << 4,
	HL_CAP_ZERO_PACKET    = 1u << 5, /* USBDEVFS_CAP_ZERO_PACKET */
	HL_CAP_MMAP_BUFFERS   = 1u << 6, /* USBDEVFS_CAP_MMAP */
	HL_CAP_CONNINFO_EX    = 1u << 7, /* port path available */
	HL_CAP_VIRTUAL_CLOCK  = 1u << 8, /* timestamps are simulated (mock) */
	/* Never set by any current backend; listed so capability queries are explicit. */
	HL_CAP_PHASE_CONTROL  = 1u << 30,
	HL_CAP_TOKEN_CONTROL  = 1u << 31,
};

enum hl_speed {
	HL_SPEED_UNKNOWN = 0,
	HL_SPEED_LOW,
	HL_SPEED_FULL,
	HL_SPEED_HIGH,
	HL_SPEED_WIRELESS,
	HL_SPEED_SUPER,
	HL_SPEED_SUPER_PLUS,
};

const char *hl_speed_name(enum hl_speed s);

struct hl_device_info {
	uint8_t busnum;
	uint8_t devnum;
	uint16_t vendor_id;
	uint16_t product_id;
	uint8_t device_class;
	enum hl_speed speed;
	uint8_t num_ports;      /* depth of port path, 0 if unknown */
	uint8_t ports[7];       /* port path from the root hub */
	char path[128];         /* backend-specific locator, e.g. /dev/bus/usb/001/004 */
};

struct hl_setup {
	uint8_t bmRequestType;
	uint8_t bRequest;
	uint16_t wValue;
	uint16_t wIndex;
	uint16_t wLength;
};

/* One asynchronous control transfer. Owned by the caller; must stay valid
 * until it has been reaped. */
struct hl_xfer {
	struct hl_setup setup;
	uint8_t *data;          /* wLength bytes; IN data lands here */
	enum hl_status status;  /* valid after reap */
	int urb_status;         /* raw negative errno from the kernel, 0 on success */
	uint32_t actual_length; /* data bytes, excluding the 8 setup bytes */
	uint64_t t_submit_ns;   /* CLOCK_MONOTONIC (or virtual) at submit */
	uint64_t t_cancel_ns;   /* 0 if never cancelled */
	uint64_t t_reap_ns;
	void *user;             /* caller cookie */
	void *backend_priv;     /* do not touch */
};

struct hl_usb_dev;

/* Backend lifecycle. `name` is "linux-usbfs" or "mock". */
struct hl_backend;
struct hl_backend *hl_backend_get(const char *name);
const char *hl_backend_name(const struct hl_backend *be);

/* Enumerate devices. Fills up to `max` entries, returns the number found
 * (which may exceed `max`) or -1 on error. */
int hl_usb_enumerate(struct hl_backend *be, struct hl_device_info *out,
		     size_t max, struct hl_error *err);

int hl_usb_open(struct hl_backend *be, const char *locator,
		struct hl_usb_dev **out, struct hl_error *err);
void hl_usb_close(struct hl_usb_dev *dev);

int hl_usb_get_info(struct hl_usb_dev *dev, struct hl_device_info *info,
		    struct hl_error *err);
uint32_t hl_usb_capabilities(struct hl_usb_dev *dev);

/* Synchronous control transfer. Returns bytes transferred (>= 0) or -1.
 * `timeout_ms` 0 means no timeout. On Linux the timeout has jiffy
 * granularity (10 ms on the Pi Zero W kernel). */
int hl_usb_control(struct hl_usb_dev *dev, const struct hl_setup *setup,
		   uint8_t *data, unsigned timeout_ms, struct hl_error *err);

/* Asynchronous control transfer: submit, optionally cancel, then reap. */
int hl_usb_submit_control(struct hl_usb_dev *dev, struct hl_xfer *x,
			  struct hl_error *err);
int hl_usb_cancel(struct hl_usb_dev *dev, struct hl_xfer *x,
		  struct hl_error *err);
/* Waits up to `timeout_ms` (0 = poll once, -1 = forever) for one completed
 * transfer. Returns 0 and stores it in *out, or -1 with HL_ERR_AGAIN. */
int hl_usb_reap(struct hl_usb_dev *dev, int timeout_ms, struct hl_xfer **out,
		struct hl_error *err);

/* Port reset followed by re-enumeration by usbcore. The handle stays valid
 * if the device comes back with identical descriptors. */
int hl_usb_reset_device(struct hl_usb_dev *dev, struct hl_error *err);

/* Clock used for hl_xfer timestamps (CLOCK_MONOTONIC on Linux). */
uint64_t hl_usb_now_ns(struct hl_usb_dev *dev);

#ifdef __cplusplus
}
#endif

#endif /* HOSTLAB_USB_BACKEND_H */
