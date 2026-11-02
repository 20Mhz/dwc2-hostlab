# SPDX-License-Identifier: MIT
"""E3 report tests on the synthetic session of test_analyze (not hardware data)."""
import json
import os
import tempfile
import unittest

from hostlab.report import (cancel_report, channel_accounting, classify, decode_setup,
                            reset_report)
from test_analyze import MON, OFF_NS, TRACE, k, mon, us


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


class ResetReportTests(unittest.TestCase):
    def test_decode_setup(self):
        self.assertEqual(decode_setup("8006000100004000"), "GET_DESCRIPTOR(DEVICE,64)")
        self.assertEqual(decode_setup("0005020000000000"), "SET_ADDRESS(2)")
        self.assertEqual(decode_setup("c05f000000000200"), "vendor(c05f000000000200)")

    def test_channel_accounting(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ftrace.txt")
            with open(path, "w") as f:
                f.write("\n".join(TRACE) + "\n")
            acc = channel_accounting(path)
        # TRACE: hc0,1,0 for A; hc2,3 for B; every start released (B's hc3 by URB_DEQUEUE)
        self.assertEqual(acc, {"starts": 5, "releases": 5, "out_of_order": 0,
                               "open_at_end": []})

    def test_reset_sequence_and_pending(self):
        user = [{"src": "user", "ev": "reset", "seq": 0, "pending": 2,
                 "t_start_ns": us("1000.000000"), "t_done_ns": us("1000.100000"),
                 "status": "ok", "errno": 0}] + [
                {"src": "user", "ev": "xfer", "op": "pending_across_reset", "seq": 0,
                 "index": i, "t_submit_ns": 0, "t_reap_ns": 0, "status": st,
                 "urb_status": us_, "actual": 0}
                for i, (st, us_) in enumerate([("ok", 0), ("no_device", -108)])]
        mons = [MON[1],
                mon("1000.010000", "S", 0xC1, -115, dev=0, setup="8006000100004000"),
                mon("1000.020000", "S", 0xC2, -115, dev=0, setup="0005020000000000"),
                mon("1000.030000", "S", 0xC3, -115, dev=2, setup="8006000100001200"),
                mon("1000.200000", "S", 0xC4, -115, dev=2, setup="8006000100001200"),
                MON[-1]]
        trace = [k("1000.005000", "port_reset", "port=1")]
        with tempfile.TemporaryDirectory() as d:
            for name, recs in (("user.jsonl", user), ("usbmon.jsonl", mons)):
                with open(os.path.join(d, name), "w") as f:
                    f.write("".join(json.dumps(r) + "\n" for r in recs))
            with open(os.path.join(d, "ftrace.txt"), "w") as f:
                f.write("\n".join(trace) + "\n")
            rep = reset_report(d)
        row = rep["rows"][0]
        self.assertEqual(row["requests"], ["dev0:GET_DESCRIPTOR(DEVICE,64)",
                                           "dev0:SET_ADDRESS(2)",
                                           "dev2:GET_DESCRIPTOR(DEVICE,18)"])
        self.assertEqual(row["port_resets_after_ns"], [5_000_000])
        self.assertEqual(rep["pending_outcomes"], {"ok": 1, "no_device": 1})


if __name__ == "__main__":
    unittest.main()
