# SPDX-License-Identifier: MIT
"""E3 report tests on the synthetic session of test_analyze (not hardware data)."""
import json
import os
import tempfile
import unittest

from hostlab.report import (cancel_report, channel_accounting, classify, decode_setup,
                            expected_runs, model_report, reset_report)
from test_analyze import MON, SETUP, TRACE, URB_A, URB_B, k, mon, us


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

    def test_lost_race_in_unlink(self):
        # Hardware pattern: usb_hcd_unlink_urb entered, the completion IRQ and
        # BH giveback ran before it read use_count, so no dwc2 dequeue.
        trace = [TRACE[1],
                 k("1000.000100", "urb_enqueue", f"urb={URB_A:#x}"),
                 k("1000.000120", "hc_start", "hc=0"),
                 k("1000.000300", "kill", f"urb={URB_A:#x}"),
                 k("1000.000306", "unlink", f"urb={URB_A:#x} status=-2"),
                 k("1000.000320", "giveback", f"urb={URB_A:#x} status=0"), TRACE[-1]]
        mons = [MON[1], mon("1000.000090", "S", URB_A, -115, setup=SETUP),
                mon("1000.000400", "C", URB_A, 0, len_cap=18), MON[-1]]
        user = [async_rec(0, "1000.000080", "ok", "ok", 18, delay=40)]
        with tempfile.TemporaryDirectory() as d:
            for name, recs in (("usbmon.jsonl", mons), ("user.jsonl", user)):
                with open(os.path.join(d, name), "w") as f:
                    f.write("".join(json.dumps(r) + "\n" for r in recs))
            with open(os.path.join(d, "ftrace.txt"), "w") as f:
                f.write("\n".join(trace) + "\n")
            rep = cancel_report(d)
        self.assertEqual(rep["classes"], {"accepted_but_completed": 1})
        self.assertEqual(rep["lost_race"], {"in_unlink": 1})

    def test_accepted_but_completed(self):
        self.assertEqual(classify({"status": "ok", "cancel": "ok"}, None),
                         "accepted_but_completed")
        self.assertEqual(classify({"status": "cancelled", "cancel": "ok"}, None),
                         "cancelled_unobserved")


def state_trace(urb, base, data_pid=2):
    t = lambda off: f"{base}.{off:06d}"
    return [
        k(t(110), "urb_enqueue", f"urb={urb:#x}"),
        k(t(120), "hc_start", "hc=0 dev=4 ep=0 in=0 pid=3 len=8"),
        k(t(200), "xfercomp", "hc=0 phase=0 hcint=0x23"),
        k(t(205), "hc_release", "hc=0 reason=1"),
        k(t(210), "hc_start", f"hc=1 dev=4 ep=0 in=1 pid={data_pid} len=18"),
        k(t(300), "xfercomp", "hc=1 phase=1 hcint=0x23"),
        k(t(305), "hc_release", "hc=1 reason=1"),
        k(t(310), "hc_start", "hc=2 dev=4 ep=0 in=0 pid=2 len=0"),
        k(t(400), "xfercomp", "hc=2 phase=2 hcint=0x23"),
        k(t(402), "giveback", f"urb={urb:#x} status=0"),
        k(t(405), "hc_release", "hc=2 reason=2")]


class ModelReportTests(unittest.TestCase):
    def test_expected_runs(self):
        self.assertEqual([(r["phase"], r["in"], r["pid"], r["len"])
                          for r in expected_runs("8006000100001200")],
                         [("SETUP", 0, 3, 8), ("DATA", 1, 2, 18), ("STATUS", 0, 2, 0)])
        self.assertEqual([(r["phase"], r["in"]) for r in expected_runs("0009010000000000")],
                         [("SETUP", 0), ("STATUS", 1)])

    def test_conforming_and_violating_urbs(self):
        trace = [TRACE[1]] + state_trace(URB_A, 1000) + state_trace(URB_B, 1001, data_pid=0)
        trace.append("      python3-300   [000] .....  1002.000000: tracing_mark_write: "
                     "hostlab_stop mono_ns=1001999990000 real_ns=0")
        mons = [MON[1]]
        for urb, base in ((URB_A, 1000), (URB_B, 1001)):
            mons += [mon(f"{base}.000101", "S", urb, -115, setup=SETUP),
                     mon(f"{base}.000451", "C", urb, 0, len_cap=18)]
        mons.append(MON[-1])
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "ftrace.txt"), "w") as f:
                f.write("\n".join(trace) + "\n")
            with open(os.path.join(d, "usbmon.jsonl"), "w") as f:
                f.write("".join(json.dumps(r) + "\n" for r in mons))
            r = model_report(d)
        self.assertEqual((r["urbs"], r["conforming"], r["violation_count"]), (2, 1, 1))
        self.assertIn("'pid': (0, 2)", r["violations"][0]["problems"][0])
        self.assertEqual(r["unattributed_channel_events"], 0)  # STATUS release inside window
        self.assertEqual(r["next_phase_channel"], {"different": 4})
        self.assertEqual(r["hcint_at_xfercomp"], {"XFERCOMPL|CHHLTD|ACK": 6})
        self.assertEqual(r["transitions"]["start[SETUP,pid=SETUP,out,len=8] -> "
                                          "xfercomp[control_phase=SETUP]"], 2)


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
