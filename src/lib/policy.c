// SPDX-License-Identifier: MIT
#include "backend_internal.h"
#include "hostlab/policy.h"

int hl_policy_check_control(const struct hl_setup *s,
			    const struct hl_device_info *info, unsigned flags,
			    struct hl_error *err)
{
	unsigned type = (s->bmRequestType >> 5) & 3;

	if (s->wLength > HL_POLICY_MAX_WLENGTH)
		return hl_fail(err, HL_ERR_INVALID, 0, "policy",
			       "wLength %u exceeds %u", s->wLength,
			       HL_POLICY_MAX_WLENGTH);
	if (info && info->device_class == 0x09 && !(flags & HL_POLICY_ALLOW_HUB))
		return hl_fail(err, HL_ERR_ACCESS, 0, "policy",
			       "device %u:%u is a hub; owned by the hub driver",
			       info->busnum, info->devnum);
	if (!hl_setup_is_in(s) && !(flags & HL_POLICY_ALLOW_WRITE))
		return hl_fail(err, HL_ERR_ACCESS, 0, "policy",
			       "host-to-device request needs --allow-write");
	if (type == 3)
		return hl_fail(err, HL_ERR_INVALID, 0, "policy",
			       "reserved request type in bmRequestType 0x%02x",
			       s->bmRequestType);
	if (type != 0 && !(flags & HL_POLICY_ALLOW_NONSTANDARD))
		return hl_fail(err, HL_ERR_ACCESS, 0, "policy",
			       "class/vendor request needs --allow-nonstandard");
	return 0;
}
