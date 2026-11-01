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
