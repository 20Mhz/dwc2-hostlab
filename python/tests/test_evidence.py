# SPDX-License-Identifier: MIT
"""Evidence bundle tests on synthetic inputs (not hardware data)."""
import argparse
import json
import os
import tempfile
import unittest

from hostlab import evidence
from hostlab.evidence import BLOCKED, PASS, is_string_descriptor_request, redact_usbmon

PREFLIGHT = {
    "generated_utc": "2026-11-01T00:00:00Z",
    "board": {"model": "Raspberry Pi Zero W Rev 1.1"},
    "os": {"kernel_release": "6.12.47+rpt-rpi-v6", "pretty_name": "test"},
    "boot_config": {"hostlab_block": True},
    "usb_devices": [{"name": "usb1", "busnum": 1, "devnum": 1, "interfaces": []}],
    "checks": [{"id": "board", "status": "ok", "detail": "Zero W"},
               {"id": "driver_binding", "status": "ok", "detail": "dwc2"},
               {"id": "usb_device", "status": "fail", "detail": "only root hubs"}],
    "probe": {"controller": {"driver": "dwc2", "params": {"host_dma": 1, "dma_desc_enable": 0},
                             "summary": {"host_dma_mode": "buffer_dma", "host_channels": 8,
                                         "hw_host_channels": 8}},
              "facilities": {"ftrace": {"hostlab_probes_usable": ["a"],
                                        "hostlab_probes_missing": []}}},
}


class RedactionTests(unittest.TestCase):
    def test_string_descriptor_detection(self):
        self.assertTrue(is_string_descriptor_request("80060303090404ff"))
        self.assertFalse(is_string_descriptor_request("8006000100001200"))  # device
        self.assertFalse(is_string_descriptor_request(None))

    def test_redacts_completion_of_string_request_only(self):
        recs = [{"ev": "urb", "type": "S", "urb": "0x1", "setup": "80060303090404ff"},
                {"ev": "urb", "type": "C", "urb": "0x1", "data": "1a03530045005200"},
                {"ev": "urb", "type": "S", "urb": "0x1", "setup": "8006000100001200"},
                {"ev": "urb", "type": "C", "urb": "0x1", "data": "12010002"}]
        out, n = redact_usbmon(recs)
        self.assertEqual(n, 1)
        self.assertNotIn("data", out[1])
        self.assertEqual(out[1]["data_redacted"], "string_descriptor")
        self.assertEqual(out[3]["data"], "12010002")


class ScrubTests(unittest.TestCase):
    def test_home_and_serial(self):
        self.assertEqual(evidence.scrub({"p": ["/home/someone/dwc2-hostlab/bin/x"]}),
                         {"p": ["~/dwc2-hostlab/bin/x"]})
        line = "[ 9.3] usb 1-1: SerialNumber: 0123456789AB"
        self.assertEqual(evidence.SERIAL.sub(r"\1<redacted>", line),
                         "[ 9.3] usb 1-1: SerialNumber: <redacted>")


class KernelLogTests(unittest.TestCase):
    def test_oops_patterns(self):
        bad = lambda l: bool(evidence.KLOG_OOPS.search(l) or evidence.KLOG_DRIVER.search(l))
        # Real line from the Pi Zero W boot log: must not count as a problem.
        self.assertFalse(bad("[    5.327491] mmc-bcm2835 20300000.mmcnr: mmc_debug:0 mmc_debug2:0"))
        self.assertTrue(bad("[ 12.0] BUG: unable to handle page fault"))
        self.assertTrue(bad("[ 12.0] WARNING: CPU: 0 PID: 1 at drivers/usb/dwc2/hcd.c:100"))
        self.assertTrue(bad("[ 12.0] dwc2 20980000.usb: Channel 3 halt timeout"))


class H8Tests(unittest.TestCase):
    def _an(self, lat, n, dev=4):
        return {"urbs": [{"xfer": "ctrl", "dev": dev, "status": 0, "usbmon_latency_ns": lat}] * n,
                "channel_events": {"total": 0, "channel_numbers_seen": []}, "consistency": {}}

    def _eval(self, an_p, an_b, exit_p=0):
        cmd = ["hostlab-ctl", "get-descriptor", "1:4"]
        sessions = {"device": {"analysis": an_p, "session": {"command": cmd, "kprobes": {},
                                                             "command_exit": exit_p}},
                    "noprobe": {"analysis": an_b, "session": {"command": cmd, "command_exit": 0}}}
        hyps = evidence.evaluate_hypotheses(PREFLIGHT, sessions, "device")
        return next(h for h in hyps if h["id"] == "H8")

    def test_pass_and_fail(self):
        self.assertEqual(self._eval(self._an(110, 50), self._an(100, 50))["status"], PASS)
        self.assertEqual(self._eval(self._an(150, 50), self._an(100, 50))["status"], "FAIL")

    def test_root_hub_failed_command_or_few_samples_are_inconclusive(self):
        self.assertEqual(self._eval(self._an(150, 50, dev=1), self._an(100, 50, dev=1))["status"],
                         "INCONCLUSIVE")
        self.assertEqual(self._eval(self._an(150, 5), self._an(100, 5))["status"], "INCONCLUSIVE")
        self.assertEqual(self._eval(self._an(150, 50), self._an(100, 50), exit_p=1)["status"],
                         "INCONCLUSIVE")


class BundleTests(unittest.TestCase):
    def test_bundle_without_sessions_is_explicit(self):
        with tempfile.TemporaryDirectory() as d:
            pf = os.path.join(d, "pf.json")
            with open(pf, "w") as f:
                json.dump(PREFLIGHT, f)
            out = os.path.join(d, "bundle")
            args = argparse.Namespace(out=out, preflight=pf, session=None, state=None,
                                      kernel_log="none", kernel_config="none")
            man = evidence.build(args)
            names = {f["path"] for f in man["files"]}
            for fn in ("hardware.json", "kernel-config.txt", "usb-topology.txt",
                       "probe-capabilities.json", "usbmon.jsonl", "dwc2-events.jsonl",
                       "analysis.json", "hypothesis-results.json", "test-summary.md"):
                self.assertIn(fn, names)
            self.assertTrue(os.path.exists(os.path.join(out, "manifest.json")))
            with open(os.path.join(out, "analysis.json")) as f:
                self.assertEqual(json.load(f)["available"], False)
            with open(os.path.join(out, "hypothesis-results.json")) as f:
                res = json.load(f)
        self.assertFalse(man["m1_complete"])
        unavailable = {u["file"] for u in man["unavailable"]}
        self.assertTrue({"usbmon.jsonl", "analysis.json", "kernel-log.txt"} <= unavailable)
        st = {h["id"]: h["status"] for h in res["hypotheses"]}
        self.assertEqual((st["H1"], st["H2"], st["H9"]), (PASS, PASS, PASS))
        for hid in ("H3", "H4", "H5", "H6", "H7", "H10"):
            self.assertNotEqual(st[hid], PASS, hid)
        m1 = {c["id"]: c["status"] for c in res["m1_checks"]}
        self.assertEqual(m1["recovery"], BLOCKED)
        self.assertEqual(m1["stability"], BLOCKED)

    def test_refuses_missing_session_dir(self):
        with tempfile.TemporaryDirectory() as d:
            pf = os.path.join(d, "pf.json")
            with open(pf, "w") as f:
                json.dump(PREFLIGHT, f)
            args = argparse.Namespace(out=os.path.join(d, "b"), preflight=pf,
                                      session=["device=/nonexistent/run"], state=None,
                                      kernel_log="none", kernel_config="none")
            with self.assertRaises(SystemExit):
                evidence.build(args)

    def test_refuses_non_empty_output(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "x"), "w") as f:
                f.write("x")
            args = argparse.Namespace(out=d, preflight="/nonexistent", session=None,
                                      state=None, kernel_log="none", kernel_config="none")
            with self.assertRaises(SystemExit):
                evidence.build(args)


if __name__ == "__main__":
    unittest.main()
