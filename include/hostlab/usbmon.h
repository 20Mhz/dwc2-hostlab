/* SPDX-License-Identifier: MIT */
/*
 * Decoder for the usbmon binary API (drivers/usb/mon/mon_bin.c).
 *
 * The kernel exports no uapi header for it; the layout below is the 64-byte
 * "API 1" header (PKT_SZ_API1, mon_bin.c:91-116,167). Fields are decoded
 * from fixed offsets in host byte order, so the decoder does not depend on
 * the compiler's struct layout. `id` is the kernel's `struct urb *` value
 * (mon_bin.c:577), which correlates with kprobe arguments.
 */
#ifndef HOSTLAB_USBMON_H
#define HOSTLAB_USBMON_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define HL_MON_HDR_SIZE 64

enum hl_mon_xfer_type {
	HL_MON_ISO = 0,
	HL_MON_INTR = 1,
	HL_MON_CTRL = 2,
	HL_MON_BULK = 3,
};

struct hl_mon_event {
	uint64_t id;          /* kernel URB address */
	char type;            /* 'S' submit, 'C' callback, 'E' submit error */
	uint8_t xfer_type;    /* enum hl_mon_xfer_type */
	uint8_t epnum;        /* bit 7 = IN */
	uint8_t devnum;
	uint16_t busnum;
	char flag_setup;      /* 0 when setup[] is valid */
	char flag_data;       /* 0 when data was captured */
	int64_t ts_sec;       /* CLOCK_REALTIME (ktime_get_real_ts64) */
	int32_t ts_usec;
	int32_t status;
	uint32_t len_urb;
	uint32_t len_cap;
	uint8_t setup[8];     /* valid for control 'S' when flag_setup == 0 */
	int32_t iso_error_count;
	int32_t iso_numdesc;
	int32_t interval;
	int32_t start_frame;
	uint32_t xfer_flags;
	uint32_t ndesc;
};

/* Decode a header. Returns 0, or -1 if len < HL_MON_HDR_SIZE or the event
 * type is not one of S/C/E. */
int hl_mon_decode(const uint8_t *buf, size_t len, struct hl_mon_event *ev);

/* Encode (used by tests and fixture generation). */
void hl_mon_encode(const struct hl_mon_event *ev, uint8_t buf[HL_MON_HDR_SIZE]);

const char *hl_mon_xfer_type_name(uint8_t t);

struct hl_jsonl;
/* Write one {"src":"usbmon","ev":"urb",...} record. `data` may be NULL. */
void hl_mon_emit(struct hl_jsonl *j, const struct hl_mon_event *ev,
		 const uint8_t *data, size_t data_len);

#ifdef __cplusplus
}
#endif

#endif /* HOSTLAB_USBMON_H */
