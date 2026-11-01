# SPDX-License-Identifier: MIT
import tempfile
import unittest

from fakefs import make_dwc2_system, make_dwc_otg_system, write
from hostlab.probe import core_revision, parse_dwc2_params, parse_kconfig, probe


class ParserTests(unittest.TestCase):
    def test_params_parse_int_and_hex(self):
        p = parse_dwc2_params("host_dma                      : 1\n"
                              "snpsid                        : 0x4f54280a\n"
                              "garbage line\n")
        self.assertEqual(p, {"host_dma": 1, "snpsid": 0x4F54280A})

    def test_core_revision(self):
        self.assertEqual(core_revision(0x4F54280A), "2.80a")
        self.assertEqual(core_revision(0x4F54271A), "2.71a")
        self.assertIsNone(core_revision(None))

    def test_kconfig(self):
        cfg = parse_kconfig("CONFIG_A=y\n# CONFIG_B is not set\nCONFIG_C=\"x\"\n",
                            ["CONFIG_A", "CONFIG_B", "CONFIG_C", "CONFIG_D"])
        self.assertEqual(cfg, {"CONFIG_A": "y", "CONFIG_B": "n", "CONFIG_C": "x",
                               "CONFIG_D": None})


class ProbeTests(unittest.TestCase):
    def test_dwc2_system(self):
        with tempfile.TemporaryDirectory() as root:
            make_dwc2_system(root)
            r = probe(root)
        ctl = r["controller"]
        self.assertEqual(ctl["driver"], "dwc2")
        self.assertEqual(ctl["device"], "20980000.usb")
        self.assertEqual(ctl["compatible"], ["brcm,bcm2835-usb"])
        self.assertEqual(ctl["dr_mode"], "host")
        s = ctl["summary"]
        self.assertEqual(s["host_dma_mode"], "buffer_dma")
        self.assertEqual(s["core_revision"], "2.80a")
        self.assertEqual(s["arch"], "internal_dma")
        self.assertEqual(s["host_channels"], 8)
        fac = r["facilities"]
        self.assertTrue(fac["usbmon"]["module_loaded"])
        self.assertEqual(fac["usbmon"]["devices"], ["usbmon0"])
        self.assertTrue(fac["ftrace"]["mono_clock"])
        self.assertTrue(fac["ftrace"]["kprobe_events"])
        self.assertIn("hc_start", fac["ftrace"]["hostlab_probes_usable"])
        self.assertEqual(fac["ftrace"]["hostlab_probes_missing"], [])
        self.assertEqual(fac["kernel_config"]["CONFIG_DYNAMIC_DEBUG"], "n")
        self.assertEqual(fac["kernel_config"]["CONFIG_HZ"], "100")
        caps = {u["capability"] for u in r["unsupported"]}
        self.assertIn("control_phase_control", caps)
        self.assertIn("dwc2_dev_dbg", caps)
        self.assertNotIn("dwc2_channel_tracing", caps)

    def test_inlined_function_reported_missing(self):
        with tempfile.TemporaryDirectory() as root:
            make_dwc2_system(root, functions=["usb_hcd_submit_urb", "_dwc2_hcd_urb_enqueue"])
            r = probe(root)
        missing = {m["symbol"] for m in r["facilities"]["ftrace"]["hostlab_probes_missing"]}
        self.assertIn("dwc2_hc_start_transfer", missing)
        self.assertNotIn("_dwc2_hcd_urb_enqueue", missing)

    def test_dwc_otg_system(self):
        with tempfile.TemporaryDirectory() as root:
            make_dwc_otg_system(root)
            r = probe(root)
        self.assertEqual(r["controller"]["driver"], "dwc_otg")
        self.assertIn("dtoverlay=dwc2", r["controller"]["note"])
        caps = {u["capability"] for u in r["unsupported"]}
        self.assertIn("dwc2_channel_tracing", caps)
        self.assertIn("facilities.ftrace", r["unknown"])

    def test_boot_config_fallback(self):
        # Raspberry Pi OS ships /boot/config-RELEASE; /proc/config.gz needs 'configs'.
        with tempfile.TemporaryDirectory() as root:
            make_dwc_otg_system(root)
            write(root, "proc/sys/kernel/osrelease", "6.12.47+rpt-rpi-v6\n")
            write(root, "boot/config-6.12.47+rpt-rpi-v6", "CONFIG_USB_DWCOTG=y\n")
            fac = probe(root)["facilities"]
        self.assertEqual(fac["kernel_config_source"], "/boot/config-6.12.47+rpt-rpi-v6")
        self.assertEqual(fac["kernel_config"]["CONFIG_USB_DWCOTG"], "y")

    def test_empty_system_reports_unknowns_not_guesses(self):
        with tempfile.TemporaryDirectory() as root:
            r = probe(root)
        self.assertIsNone(r["controller"]["driver"])
        self.assertIsNone(r["model"])
        self.assertIn("controller.driver", r["unknown"])
        self.assertIn("facilities.kernel_config", r["unknown"])


if __name__ == "__main__":
    unittest.main()
