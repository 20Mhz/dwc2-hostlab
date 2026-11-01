// SPDX-License-Identifier: MIT
#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include "hostlab/jsonl.h"
#include "hostlab/usbmon.h"

/* Offsets of struct mon_bin_hdr (drivers/usb/mon/mon_bin.c:91-116). */
enum {
	OFF_ID = 0,
	OFF_TYPE = 8,
	OFF_XFER_TYPE = 9,
	OFF_EPNUM = 10,
	OFF_DEVNUM = 11,
	OFF_BUSNUM = 12,
	OFF_FLAG_SETUP = 14,
	OFF_FLAG_DATA = 15,
	OFF_TS_SEC = 16,
	OFF_TS_USEC = 24,
	OFF_STATUS = 28,
	OFF_LEN_URB = 32,
	OFF_LEN_CAP = 36,
	OFF_SETUP = 40,       /* union with iso.error_count / iso.numdesc */
	OFF_INTERVAL = 48,
	OFF_START_FRAME = 52,
	OFF_XFER_FLAGS = 56,
	OFF_NDESC = 60,
};

#define GET(field, off) memcpy(&(field), buf + (off), sizeof(field))
#define PUT(field, off) memcpy(buf + (off), &(field), sizeof(field))

int hl_mon_decode(const uint8_t *buf, size_t len, struct hl_mon_event *ev)
{
	if (len < HL_MON_HDR_SIZE)
		return -1;
	memset(ev, 0, sizeof(*ev));
	GET(ev->id, OFF_ID);
	ev->type = (char)buf[OFF_TYPE];
	ev->xfer_type = buf[OFF_XFER_TYPE];
	ev->epnum = buf[OFF_EPNUM];
	ev->devnum = buf[OFF_DEVNUM];
	GET(ev->busnum, OFF_BUSNUM);
	ev->flag_setup = (char)buf[OFF_FLAG_SETUP];
	ev->flag_data = (char)buf[OFF_FLAG_DATA];
	GET(ev->ts_sec, OFF_TS_SEC);
	GET(ev->ts_usec, OFF_TS_USEC);
	GET(ev->status, OFF_STATUS);
	GET(ev->len_urb, OFF_LEN_URB);
	GET(ev->len_cap, OFF_LEN_CAP);
	memcpy(ev->setup, buf + OFF_SETUP, sizeof(ev->setup));
	GET(ev->iso_error_count, OFF_SETUP);
	GET(ev->iso_numdesc, OFF_SETUP + 4);
	GET(ev->interval, OFF_INTERVAL);
	GET(ev->start_frame, OFF_START_FRAME);
	GET(ev->xfer_flags, OFF_XFER_FLAGS);
	GET(ev->ndesc, OFF_NDESC);
	if (ev->type != 'S' && ev->type != 'C' && ev->type != 'E')
		return -1;
	return 0;
}

void hl_mon_encode(const struct hl_mon_event *ev, uint8_t buf[HL_MON_HDR_SIZE])
{
	memset(buf, 0, HL_MON_HDR_SIZE);
	PUT(ev->id, OFF_ID);
	buf[OFF_TYPE] = (uint8_t)ev->type;
	buf[OFF_XFER_TYPE] = ev->xfer_type;
	buf[OFF_EPNUM] = ev->epnum;
	buf[OFF_DEVNUM] = ev->devnum;
	PUT(ev->busnum, OFF_BUSNUM);
	buf[OFF_FLAG_SETUP] = (uint8_t)ev->flag_setup;
	buf[OFF_FLAG_DATA] = (uint8_t)ev->flag_data;
	PUT(ev->ts_sec, OFF_TS_SEC);
	PUT(ev->ts_usec, OFF_TS_USEC);
	PUT(ev->status, OFF_STATUS);
	PUT(ev->len_urb, OFF_LEN_URB);
	PUT(ev->len_cap, OFF_LEN_CAP);
	if (ev->xfer_type == HL_MON_ISO) {
		PUT(ev->iso_error_count, OFF_SETUP);
		PUT(ev->iso_numdesc, OFF_SETUP + 4);
	} else {
		memcpy(buf + OFF_SETUP, ev->setup, sizeof(ev->setup));
	}
	PUT(ev->interval, OFF_INTERVAL);
	PUT(ev->start_frame, OFF_START_FRAME);
	PUT(ev->xfer_flags, OFF_XFER_FLAGS);
	PUT(ev->ndesc, OFF_NDESC);
}

const char *hl_mon_xfer_type_name(uint8_t t)
{
	switch (t) {
	case HL_MON_ISO: return "iso";
	case HL_MON_INTR: return "intr";
	case HL_MON_CTRL: return "ctrl";
	case HL_MON_BULK: return "bulk";
	}
	return "unknown";
}

void hl_mon_emit(struct hl_jsonl *j, const struct hl_mon_event *ev,
		 const uint8_t *data, size_t data_len)
{
	char urb[24], type[2] = { ev->type, '\0' };

	/* Same spelling as a kprobe "x32"/"x64" fetch argument. */
	snprintf(urb, sizeof(urb), "0x%" PRIx64, ev->id);
	hl_jsonl_begin(j);
	hl_jsonl_str(j, "src", "usbmon");
	hl_jsonl_str(j, "ev", "urb");
	hl_jsonl_str(j, "type", type);
	hl_jsonl_str(j, "urb", urb);
	hl_jsonl_u64(j, "real_us", (uint64_t)ev->ts_sec * 1000000u + (uint64_t)ev->ts_usec);
	hl_jsonl_str(j, "xfer", hl_mon_xfer_type_name(ev->xfer_type));
	hl_jsonl_u64(j, "bus", ev->busnum);
	hl_jsonl_u64(j, "dev", ev->devnum);
	hl_jsonl_u64(j, "ep", ev->epnum & 0x7f);
	hl_jsonl_bool(j, "in", (ev->epnum & 0x80) != 0);
	hl_jsonl_i64(j, "status", ev->status);
	hl_jsonl_u64(j, "len_urb", ev->len_urb);
	hl_jsonl_u64(j, "len_cap", ev->len_cap);
	if (ev->xfer_type == HL_MON_CTRL && ev->type == 'S' && ev->flag_setup == 0)
		hl_jsonl_hex(j, "setup", ev->setup, sizeof(ev->setup));
	if (data && data_len)
		hl_jsonl_hex(j, "data", data, data_len);
	if (ev->xfer_type == HL_MON_INTR || ev->xfer_type == HL_MON_ISO)
		hl_jsonl_i64(j, "interval", ev->interval);
	hl_jsonl_u64(j, "xfer_flags", ev->xfer_flags);
	hl_jsonl_end(j);
}
