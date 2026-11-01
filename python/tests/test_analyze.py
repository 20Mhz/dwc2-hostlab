# SPDX-License-Identifier: MIT
"""Analyser tests on a synthetic session (constructed by hand, not captured).

URB A: GET_DESCRIPTOR(device, 18) completing normally in three channel runs.
URB B: the same request, killed by usbfs during its second run (DATA).
"""
import json
import os
import tempfile
import unittest

from hostlab.analyze import _phase_labels, analyze, format_text

OFF_NS = 1_700_000_000 * 1_000_000_000  # REALTIME - MONOTONIC
URB_A, URB_B = 0xC1000000, 0xC1000100
SETUP = "8006000100001200"


def us(t):  # seconds.micro string -> mono ns
    s, f = t.split(".")
    return int(s) * 1_000_000_000 + int(f) * 1000


def mon(t, typ, urb, status, **kw):
    rec = {"src": "usbmon", "ev": "urb", "type": typ, "urb": hex(urb),
           "real_us": (us(t) + OFF_NS) // 1000, "xfer": "ctrl", "bus": 1, "dev": 4,
           "ep": 0, "in": True, "status": status, "len_urb": 18, "len_cap": 0}
    rec.update(kw)
    return rec


def k(t, name, args):
    return f"   hostlab-ctl-321   [000] d..1.  {t}: hl_{name}: ({name}+0x0/0x100) {args}"


TRACE = [
    "# tracer: nop",
    "      python3-300   [000] .....  1000.000050: tracing_mark_write: hostlab_start mono_ns=1000000050000 real_ns=0",
    k("1000.000100", "submit", f"urb={URB_A:#x}"),
    k("1000.000110", "urb_enqueue", f"urb={URB_A:#x}"),
    k("1000.000120", "hc_start", "hc=0"),
    k("1000.000200", "hc_irq", "hc=0"),
    k("1000.000201", "xfercomp", "hc=0"),
    k("1000.000205", "hc_release", "hc=0 reason=1"),
    k("1000.000210", "hc_start", "hc=1"),
    k("1000.000300", "xfercomp", "hc=1"),
    k("1000.000305", "hc_release", "hc=1 reason=1"),
    k("1000.000310", "hc_start", "hc=0"),
    k("1000.000400", "xfercomp", "hc=0"),
    k("1000.000401", "host_complete", "status=0"),
    k("1000.000402", "giveback", f"urb={URB_A:#x} status=0"),
    k("1000.000405", "hc_release", "hc=0 reason=2"),
    k("1000.000450", "giveback_bh", f"urb={URB_A:#x}"),
    k("1000.001000", "submit", f"urb={URB_B:#x}"),
    k("1000.001010", "urb_enqueue", f"urb={URB_B:#x}"),
    k("1000.001020", "hc_start", "hc=2"),
    k("1000.001100", "xfercomp", "hc=2"),
    k("1000.001105", "hc_release", "hc=2 reason=1"),
    k("1000.001110", "hc_start", "hc=3"),
    k("1000.001150", "kill", f"urb={URB_B:#x}"),
    k("1000.001152", "urb_dequeue", f"urb={URB_B:#x} status=-2"),
    k("1000.001153", "hc_halt", "hc=3 reason=13"),
    k("1000.001155", "giveback", f"urb={URB_B:#x} status=-2"),
    k("1000.001170", "giveback_bh", f"urb={URB_B:#x}"),
    k("1000.001180", "hc_irq", "hc=3"),
    k("1000.001185", "hc_release", "hc=3 reason=13"),
    "      python3-300   [000] .....  1000.002000: tracing_mark_write: hostlab_stop mono_ns=1000001990000 real_ns=0",
]

MON = [
    {"src": "usbmon", "ev": "meta", "tool": "hostlab-mon"},
    {"src": "usbmon", "ev": "clock", "mono_ns": us("999.000000"),
     "real_ns": us("999.000000") + OFF_NS, "boot_ns": 0, "uncert_ns": 800},
    mon("1000.000101", "S", URB_A, -115, setup=SETUP),
    mon("1000.000451", "C", URB_A, 0, len_cap=18),
    mon("1000.001001", "S", URB_B, -115, setup=SETUP),
    mon("1000.001171", "C", URB_B, -2),
    {"src": "usbmon", "ev": "clock", "mono_ns": us("1001.000000"),
     "real_ns": us("1001.000000") + OFF_NS, "boot_ns": 0, "uncert_ns": 900},
]


class AnalyzeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        with open(os.path.join(d, "ftrace.txt"), "w") as f:
            f.write("\n".join(TRACE) + "\n")
        with open(os.path.join(d, "usbmon.jsonl"), "w") as f:
            f.write("".join(json.dumps(r) + "\n" for r in MON))
        with open(os.path.join(d, "user.jsonl"), "w") as f:
            f.write(json.dumps({"src": "user", "ev": "meta", "backend": "linux-usbfs"}) + "\n")
        with open(os.path.join(d, "session.json"), "w") as f:
            json.dump({"markers": [{"tag": "hostlab_start", "mono_before_ns": 1000000050000,
                                    "mono_after_ns": 1000000060000}]}, f)
        self.rep = analyze(d)
        self.by_urb = {int(u["urb"], 16): u for u in self.rep["urbs"]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_clock_mapping(self):
        self.assertEqual(self.rep["clock"]["usbmon_real_minus_mono_ns"], OFF_NS)
        self.assertEqual(self.rep["clock"]["drift_ns"], 0)
        self.assertEqual(self.by_urb[URB_A]["usbmon_latency_ns"], 350_000)
        self.assertEqual(self.rep["warnings"], [])

    def test_normal_control_transfer_phases(self):
        a = self.by_urb[URB_A]
        self.assertEqual(a["status"], 0)
        self.assertEqual(a["channel_runs"], 3)
        self.assertEqual(a["phase_label_certainty"], "exact")
        self.assertEqual([(p["label"], p["hc"]) for p in a["phases"]],
                         [("SETUP", 0), ("DATA", 1), ("STATUS", 0)])
        self.assertEqual(a["enqueue_to_first_start_ns"], 10_000)
        self.assertEqual(a["giveback_bh_delay_ns"], 48_000)
        self.assertNotIn("cancel", a)

    def test_cancelled_transfer(self):
        b = self.by_urb[URB_B]
        self.assertEqual(b["status"], -2)
        c = b["cancel"]
        self.assertEqual(c["dequeue_status"], -2)
        self.assertEqual(c["runs_started_before_dequeue"], 2)
        self.assertEqual(c["phase_at_dequeue"], "DATA")
        self.assertEqual(c["phase_label_certainty"], "prefix")
        self.assertEqual(c["dequeue_to_release_ns"], 33_000)
        self.assertTrue(c["giveback_before_channel_release"])

    def test_channel_attribution(self):
        ch = self.rep["channel_events"]
        self.assertEqual(ch["ambiguous"], 0)
        self.assertEqual(ch["unattributed"], 2)  # B's halt IRQ + release after its giveback
        self.assertEqual(ch["channel_numbers_seen"], [0, 1, 2, 3])

    def test_marker_offset_and_text(self):
        self.assertEqual(self.rep["clock"]["ftrace_marker_offsets_ns"], [0, 10_000])
        (b,) = self.rep["clock"]["ftrace_marker_brackets"]
        self.assertTrue(b["in_bracket"])
        self.assertEqual(b["after_minus_ftrace_ns"], 10_000)
        text = format_text(self.rep)
        self.assertIn("phases=SETUP@hc0,DATA@hc1,STATUS@hc0", text)
        self.assertIn("cancel(phase=DATA", text)

    def test_join_consistency(self):
        c = self.rep["consistency"]
        self.assertEqual(c["device_urbs_in_usbmon"], 2)
        self.assertEqual(c["with_dwc2_enqueue"], 2)
        self.assertEqual(c["usbmon_without_enqueue"], 0)
        self.assertEqual(c["enqueue_without_usbmon"], 0)
        self.assertIsNone(c["host_channels"])  # no probe.json in this session

    def test_urbs_before_trace_start_are_not_join_failures(self):
        # usbmon starts before ftrace; a device URB in that gap has no kprobe events.
        early = [mon("999.500000", "S", 0xC2000000, -115, setup=SETUP),
                 mon("999.500300", "C", 0xC2000000, 0)]
        with open(os.path.join(self.tmp.name, "usbmon.jsonl"), "w") as f:
            f.write("".join(json.dumps(r) + "\n" for r in MON[:2] + early + MON[2:]))
        c = analyze(self.tmp.name)["consistency"]
        self.assertEqual(c["device_urbs_in_usbmon"], 2)
        self.assertEqual(c["usbmon_without_enqueue"], 0)
        self.assertEqual(c["usbmon_unpaired_with_kprobes"], 0)

    def test_channel_range_uses_probe(self):
        with open(os.path.join(self.tmp.name, "probe.json"), "w") as f:
            json.dump({"controller": {"summary": {"host_channels": 3}}}, f)
        c = analyze(self.tmp.name)["consistency"]
        self.assertEqual(c["host_channels"], 3)
        self.assertEqual(c["channel_numbers_out_of_range"], [3])

    def test_empty_session(self):
        with tempfile.TemporaryDirectory() as d:
            rep = analyze(d)
        self.assertEqual(rep["urbs"], [])
        self.assertEqual(rep["consistency"]["device_urbs_in_usbmon"], 0)


class AddressReuseTests(unittest.TestCase):
    """The slab reuses URB addresses (seen on a Pi Zero W: 20 usbfs transfers
    in a row on one address). Each submit..giveback must be its own URB."""

    def test_reused_address_gives_separate_instances(self):
        start = "      python3-300   [000] .....  999.900000: tracing_mark_write: hostlab_start mono_ns=999899990000 real_ns=0"
        stop = "      python3-300   [000] .....  1003.000000: tracing_mark_write: hostlab_stop mono_ns=1002999990000 real_ns=0"
        trace, mons = [start], []
        for n in range(3):
            base = 1000 + n  # one transfer per second, same address
            trace += [k(f"{base}.000100", "submit", f"urb={URB_A:#x}"),
                      k(f"{base}.000110", "urb_enqueue", f"urb={URB_A:#x}"),
                      k(f"{base}.000120", "hc_start", "hc=0"),
                      k(f"{base}.000210", "hc_start", "hc=1"),
                      k(f"{base}.000402", "giveback", f"urb={URB_A:#x} status=0"),
                      k(f"{base}.000450", "giveback_bh", f"urb={URB_A:#x}")]
            mons += [mon(f"{base}.000101", "S", URB_A, -115, setup="8006000100000000"),
                     mon(f"{base}.000451", "C", URB_A, 0)]
        trace.append(stop)
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "ftrace.txt"), "w") as f:
                f.write("\n".join(trace) + "\n")
            with open(os.path.join(d, "usbmon.jsonl"), "w") as f:
                f.write("".join(json.dumps(r) + "\n" for r in [MON[1]] + mons + [MON[-1]]))
            rep = analyze(d)
        self.assertEqual([u["instance"] for u in rep["urbs"]], [0, 1, 2])
        self.assertTrue(all(u["channel_runs"] == 2 for u in rep["urbs"]))
        self.assertTrue(all(u["phase_label_certainty"] == "exact" for u in rep["urbs"]))
        self.assertEqual(rep["consistency"]["with_dwc2_enqueue"], 3)
        self.assertEqual(rep["consistency"]["address_reuse_max"], 3)
        self.assertEqual(rep["consistency"]["usbmon_unpaired_with_kprobes"], 0)


class PhaseLabelTests(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(_phase_labels(2, 0), (["SETUP", "STATUS"], "exact"))
        self.assertEqual(_phase_labels(4, 18), (["run0", "run1", "run2", "run3"], "unknown"))
        self.assertEqual(_phase_labels(1, 18, truncated=True), (["SETUP"], "prefix"))
        self.assertEqual(_phase_labels(3, None)[1], "unknown")


if __name__ == "__main__":
    unittest.main()
