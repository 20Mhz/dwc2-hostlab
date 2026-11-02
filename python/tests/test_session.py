# SPDX-License-Identifier: MIT
import argparse
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

import hostlab.session as session
from fakefs import FakeTraceFS, make_dwc2_system, make_tracefs, write
from hostlab.tracefs import ARM32_PROFILE, GROUP


class FtraceSessionTests(unittest.TestCase):
    def test_setup_registers_only_available_and_teardown_restores(self):
        with tempfile.TemporaryDirectory() as root:
            path = make_tracefs(root, functions=["usb_hcd_submit_urb", "dwc2_hc_halt"])
            tfs = FakeTraceFS(path)
            fs = session.FtraceSession(tfs, buffer_kb=2048)
            fs.setup()
            self.assertEqual(sorted(fs.registered), ["hl_hc_halt", "hl_submit"])
            self.assertIn("dwc2_hc_start_transfer", fs.missing)
            self.assertEqual(tfs.current_clock(), "mono")
            self.assertEqual(tfs.read("buffer_size_kb").strip(), "2048")
            self.assertEqual(tfs.read(f"events/{GROUP}/enable").strip(), "1")
            kp = tfs.read("kprobe_events")
            self.assertIn("p:hostlab/hl_hc_halt dwc2_hc_halt hc=+0(%r1):u8 reason=%r2:u32", kp)

            fs.start()
            fs.stop(os.path.join(root, "out.txt"))
            with open(os.path.join(root, "out.txt")) as f:
                self.assertIn("hostlab_stop mono_ns=", f.read())

            result = fs.teardown()
            self.assertEqual(sorted(result["removed"]), ["hl_hc_halt", "hl_submit"])
            self.assertEqual(result["errors"], [])
            self.assertEqual(tfs.read("kprobe_events").strip(), "")
            self.assertEqual(tfs.current_clock(), "local")
            self.assertEqual(tfs.read("buffer_size_kb").strip(), "1408")
            self.assertEqual(tfs.read("tracing_on").strip(), "1")

    def test_teardown_leaves_foreign_probes(self):
        with tempfile.TemporaryDirectory() as root:
            path = make_tracefs(root)
            write(root, "sys/kernel/tracing/kprobe_events", "p:other/mine do_sys_open\n")
            tfs = FakeTraceFS(path)
            tfs.add_kprobe(ARM32_PROFILE[0])
            tfs.teardown()
            self.assertEqual(tfs.read("kprobe_events"), "p:other/mine do_sys_open\n")

    def test_stale_probes_removed_before_setup(self):
        with tempfile.TemporaryDirectory() as root:
            path = make_tracefs(root, functions=["usb_hcd_submit_urb"])
            tfs = FakeTraceFS(path)
            tfs.add_kprobe(ARM32_PROFILE[0])  # left over from a crashed run
            session.FtraceSession(tfs).setup()
            self.assertEqual(tfs.read("kprobe_events").count("hostlab/hl_submit"), 1)


class RunTests(unittest.TestCase):
    def _args(self, root, out, **kw):
        a = argparse.Namespace(root=root, out=out, bus=0, snaplen=64, buffer_kb=1024,
                               bindir=None, no_ftrace=False, no_usbmon=True, profile="full",
                               kprobes=None, functions=None,
                               cmd=[sys.executable, "-c",
                                    "print('{\"src\":\"user\",\"ev\":\"meta\"}')"])
        for key, v in kw.items():
            setattr(a, key, v)
        return a

    def test_run_records_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as out:
            make_dwc2_system(root)
            with mock.patch.object(session, "TraceFS", FakeTraceFS):
                rc = session.run(self._args(root, out))
            self.assertEqual(rc, 0)
            for name in ("probe.json", "user.jsonl", "ftrace.txt", "session.json"):
                self.assertTrue(os.path.exists(os.path.join(out, name)), name)
            with open(os.path.join(out, "session.json")) as f:
                info = json.load(f)
            self.assertEqual(info["command_exit"], 0)
            self.assertEqual(len(info["kprobes"]["registered"]), len(ARM32_PROFILE))
            self.assertEqual(len(info["teardown"]["removed"]), len(ARM32_PROFILE))
            kp = os.path.join(root, "sys/kernel/tracing/kprobe_events")
            with open(kp) as f:
                self.assertEqual(f.read().strip(), "")
            self.assertEqual([m["tag"] for m in info["markers"]], ["hostlab_start", "hostlab_stop"])
            for m in info["markers"]:
                self.assertLessEqual(m["mono_before_ns"], m["mono_after_ns"])

    def test_profiles_register_subsets(self):
        from hostlab.tracefs import PROFILES
        for name, prof in PROFILES.items():
            with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as out:
                make_dwc2_system(root)
                with mock.patch.object(session, "TraceFS", FakeTraceFS):
                    session.run(self._args(root, out, profile=name))
                with open(os.path.join(out, "session.json")) as f:
                    info = json.load(f)
                self.assertEqual(info["profile"], name)
                self.assertEqual(sorted(info["kprobes"]["registered"]),
                                 sorted("hl_" + k for k in prof["kprobes"]), name)

    def test_custom_probe_set(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as out:
            make_dwc2_system(root)
            with mock.patch.object(session, "TraceFS", FakeTraceFS):
                session.run(self._args(root, out, kprobes="giveback", functions=""))
            with open(os.path.join(out, "session.json")) as f:
                info = json.load(f)
            self.assertEqual(info["profile"], "custom")
            self.assertEqual(info["kprobes"]["registered"], ["hl_giveback"])

    def test_function_tracer_profile_restores_state(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as out:
            make_dwc2_system(root, functions=[p.symbol for p in ARM32_PROFILE])
            tfs = os.path.join(root, "sys/kernel/tracing")
            seen = {}

            class Spy(FakeTraceFS):
                def set_tracer(self, name):
                    seen.setdefault("tracers", []).append(name)
                    super().set_tracer(name)

            with mock.patch.object(session, "TraceFS", Spy):
                session.run(self._args(root, out, profile="fphase"))
            with open(os.path.join(out, "session.json")) as f:
                info = json.load(f)
            self.assertEqual(info["functions"], ["dwc2_hc_start_transfer"])
            self.assertEqual(seen["tracers"], ["function", "nop"])
            with open(os.path.join(tfs, "current_tracer")) as f:
                self.assertEqual(f.read().strip(), "nop")
            with open(os.path.join(tfs, "set_ftrace_filter")) as f:
                self.assertEqual(f.read().strip(), "")

    def test_failed_registration_is_a_warning(self):
        # On 6.12.47 every write failed with EINVAL and the session still
        # looked clean; a failed registration must be reported.
        class RejectingTraceFS(FakeTraceFS):
            def add_kprobe(self, probe):
                raise OSError(22, "Invalid argument")

        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as out:
            make_dwc2_system(root)
            with mock.patch.object(session, "TraceFS", RejectingTraceFS):
                session.run(self._args(root, out))
            with open(os.path.join(out, "session.json")) as f:
                info = json.load(f)
            self.assertEqual(info["kprobes"]["registered"], [])
            self.assertTrue(any("failed to register" in w for w in info["warnings"]))

    def test_teardown_runs_when_command_fails_to_start(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as out:
            make_dwc2_system(root)
            args = self._args(root, out, cmd=["/nonexistent/hostlab-test-binary"])
            with mock.patch.object(session, "TraceFS", FakeTraceFS):
                with self.assertRaises(OSError):
                    session.run(args)
            with open(os.path.join(out, "session.json")) as f:
                info = json.load(f)
            self.assertEqual(len(info["teardown"]["removed"]), len(ARM32_PROFILE))

    def test_run_without_tracefs_warns(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as out:
            rc = session.run(self._args(root, out))
            self.assertEqual(rc, 0)
            with open(os.path.join(out, "session.json")) as f:
                info = json.load(f)
            self.assertTrue(any("tracefs" in w for w in info["warnings"]))


if __name__ == "__main__":
    unittest.main()
