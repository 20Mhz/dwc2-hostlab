# SPDX-License-Identifier: MIT
"""E3 report tests on the synthetic session of test_analyze (not hardware data)."""
import json
import os
import tempfile
import unittest

from hostlab.report import cancel_report, classify
from test_analyze import MON, TRACE, us


def async_rec(seq, t_submit, cancel, status, actual=0, delay=0):
    return {"src": "user", "ev": "xfer", "op": "async", "seq": seq, "delay_us": delay,
            "setup": "8006000100001200", "t_submit_ns": us(t_submit),
            "t_cancel_ns": us(t_submit) + 50_000 if cancel == "ok" else None,
            "t_reap_ns": us(t_submit) + 500_000, "cancel": cancel, "status": status,
            "urb_status": -2 if status == "cancelled" else 0, "actual": actual}


class CancelReportTests(unittest.TestCase):
    def test_join_and_classes(self):
        user = [async_rec(0, "1000.000090", "not_pending", "ok", 18, delay=400),
                async_rec(1, "1000.000990", "ok", "cancelled", 0, delay=100)]
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "ftrace.txt"), "w") as f:
                f.write("\n".join(TRACE) + "\n")
            with open(os.path.join(d, "usbmon.jsonl"), "w") as f:
                f.write("".join(json.dumps(r) + "\n" for r in MON))
            with open(os.path.join(d, "user.jsonl"), "w") as f:
                f.write("".join(json.dumps(r) + "\n" for r in user))
            rep = cancel_report(d)
        self.assertEqual(rep["joined"], 2)
        self.assertEqual(rep["classes"], {"too_late": 1, "cancelled_in_DATA": 1})
        b = rep["rows"][1]
        self.assertEqual(b["dequeue_to_release_ns"], 33_000)
        self.assertEqual(b["dequeue_to_halt_ns"], 1_000)
        self.assertEqual(b["halted_hc"], 3)
        self.assertEqual(rep["h3"]["status"], "INCONCLUSIVE")  # nothing cancelled in STATUS
        self.assertEqual(rep["h3"]["cancelled_in_DATA"], 1)

    def test_accepted_but_completed(self):
        self.assertEqual(classify({"status": "ok", "cancel": "ok"}, None),
                         "accepted_but_completed")
        self.assertEqual(classify({"status": "cancelled", "cancel": "ok"}, None),
                         "cancelled_unobserved")


if __name__ == "__main__":
    unittest.main()
