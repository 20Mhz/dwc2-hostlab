/* SPDX-License-Identifier: MIT */
/*
 * Mock USB backend for unit tests and demos.
 *
 * Time is virtual: it only advances through hl_mock_advance() or when a
 * blocking call (control, reap with timeout) has to wait for a simulated
 * completion. Results are therefore fully deterministic.
 *
 * Semantics copied from Linux usbfs (docs/usb-control-transfers.md §5):
 *  - cancelling a transfer the HCD has not completed yet completes it with
 *    HL_ERR_CANCELLED and actual_length 0 (dwc2 does not report partial
 *    progress on dequeue);
 *  - a transfer the HCD has completed but usbcore has not yet given back
 *    (window set by hl_mock_set_giveback_delay, default 0) accepts the
 *    cancel (returns 0) and still completes with its real status. This was
 *    observed on a real kernel (docs/evidence/linux-vm-roothub-cancel-sweep.jsonl);
 *  - cancelling a completed-but-unreaped transfer fails with
 *    HL_ERR_NOT_PENDING (usbfs DISCARDURB returns -EINVAL);
 *  - a synchronous transfer whose latency exceeds its timeout fails with
 *    HL_ERR_TIMEOUT after exactly timeout_ms of virtual time.
 * Requests with no programmed response get a default answer for standard
 * GET_DESCRIPTOR (device, configuration) and GET_STATUS, and STALL otherwise.
 */
#ifndef HOSTLAB_MOCK_BACKEND_H
#define HOSTLAB_MOCK_BACKEND_H

#include "hostlab/usb_backend.h"

#ifdef __cplusplus
extern "C" {
#endif

enum {
	HL_MOCK_MATCH_REQTYPE = 1u << 0,
	HL_MOCK_MATCH_REQUEST = 1u << 1,
	HL_MOCK_MATCH_VALUE   = 1u << 2,
	HL_MOCK_MATCH_INDEX   = 1u << 3,
	HL_MOCK_MATCH_LENGTH  = 1u << 4,
	HL_MOCK_MATCH_ALL     = 0x1f,
};

#define HL_MOCK_REPEAT_FOREVER 0xffffffffu

struct hl_mock_response {
	struct hl_setup match;
	uint32_t match_mask;   /* HL_MOCK_MATCH_*; 0 matches any request */
	enum hl_status status;
	const uint8_t *data;   /* IN payload, copied at push time */
	uint16_t data_len;
	uint64_t latency_ns;   /* submit -> completion, virtual time */
	unsigned repeat;       /* number of uses; 0 means 1 */
};

struct hl_mock_log_entry {
	uint8_t devnum;
	struct hl_setup setup;
	uint64_t t_submit_ns;
	uint64_t t_done_ns;
	enum hl_status status;
	uint32_t actual_length;
	int cancelled;
	int async;
};

struct hl_backend *hl_mock_create(void);
void hl_mock_destroy(struct hl_backend *be);
/* Shared instance used by hl_backend_get("mock"); has one default device,
 * bus 1 address 2, 1209:0001 (pid.codes test PID). */
struct hl_backend *hl_mock_default(void);

int hl_mock_add_device(struct hl_backend *be, const struct hl_device_info *info,
		       const uint8_t *device_desc, size_t device_desc_len,
		       const uint8_t *config_desc, size_t config_desc_len);
int hl_mock_push_response(struct hl_backend *be, uint8_t devnum,
			  const struct hl_mock_response *r);
void hl_mock_set_default_latency(struct hl_backend *be, uint64_t ns);
/* Delay between HCD completion and giveback (usbcore BH). */
void hl_mock_set_giveback_delay(struct hl_backend *be, uint64_t ns);
void hl_mock_advance(struct hl_backend *be, uint64_t ns);
uint64_t hl_mock_now(struct hl_backend *be);

size_t hl_mock_log_count(struct hl_backend *be);
const struct hl_mock_log_entry *hl_mock_log_get(struct hl_backend *be, size_t i);
unsigned hl_mock_reset_count(struct hl_backend *be, uint8_t devnum);

#ifdef __cplusplus
}
#endif

#endif /* HOSTLAB_MOCK_BACKEND_H */
