/* SPDX-License-Identifier: MIT */
/*
 * Safety policy for tools that issue control requests.
 *
 * Default: standard device-to-host requests only, never to a hub (hubs,
 * including root hubs, are owned by the kernel hub driver), wLength bounded
 * by usbfs' own PAGE_SIZE limit (core/devio.c:1185). Each relaxation needs an
 * explicit flag so experiments are visible in command lines and logs.
 */
#ifndef HOSTLAB_POLICY_H
#define HOSTLAB_POLICY_H

#include "hostlab/usb_backend.h"

#ifdef __cplusplus
extern "C" {
#endif

#define HL_POLICY_MAX_WLENGTH 4096u

enum hl_policy_flags {
	HL_POLICY_ALLOW_WRITE       = 1u << 0, /* host-to-device requests */
	HL_POLICY_ALLOW_NONSTANDARD = 1u << 1, /* class / vendor requests */
	HL_POLICY_ALLOW_HUB         = 1u << 2, /* any request to a hub */
};

/* Returns 0 if allowed, -1 with err->status == HL_ERR_ACCESS otherwise. */
int hl_policy_check_control(const struct hl_setup *s,
			    const struct hl_device_info *info, unsigned flags,
			    struct hl_error *err);

#ifdef __cplusplus
}
#endif

#endif /* HOSTLAB_POLICY_H */
