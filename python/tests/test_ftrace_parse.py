# SPDX-License-Identifier: MIT
import unittest

from hostlab.ftrace_parse import parse, parse_line, parse_ts


class FtraceParseTests(unittest.TestCase):
    def test_kprobe_line_with_module_and_flags(self):
        ev = parse_line("     hostlab-ctl-1234    [000] d..1.   1234.567891: "
                        "hl_urb_enqueue: (_dwc2_hcd_urb_enqueue+0x0/0x2c0 [dwc2]) "
                        "urb=0xc3a4b500")
        self.assertEqual(ev.event, "hl_urb_enqueue")
        self.assertEqual(ev.ts_ns, 1234_567_891_000)
        self.assertEqual(ev.task, "hostlab-ctl")
        self.assertEqual(ev.pid, 1234)
        self.assertEqual(ev.int_field("urb"), 0xC3A4B500)

    def test_documented_format(self):
        # Documentation/trace/kprobetrace.rst:286 (no irq-info flags)
        ev = parse_line("             <...>-1447  [001] 1038282.286875: myprobe: "
                        "(do_sys_open+0x0/0xd6) dfd=3 filename=7fffd1ec4440 flags=8000 mode=0")
        self.assertEqual(ev.event, "myprobe")
        self.assertEqual(ev.fields["filename"], "7fffd1ec4440")
        self.assertEqual(ev.ts_ns, 1038282_286875_000)

    def test_old_four_char_flags_and_tgid(self):
        ev = parse_line("  <idle>-0     (    0) [000] d.h1   99.000001: hc_start: "
                        "(dwc2_hc_start_transfer+0x0/0x3a0 [dwc2]) hc=3")
        self.assertEqual(ev.event, "hc_start")
        self.assertEqual(ev.int_field("hc"), 3)
        self.assertEqual(ev.task, "<idle>")

    def test_no_irq_info(self):
        ev = parse_line("kworker/0:1H-42 [000] 5.5: giveback_bh: "
                        "(__usb_hcd_giveback_urb+0x0/0x120) urb=0xc0ffee00")
        self.assertEqual(ev.ts_ns, 5_500_000_000)
        self.assertEqual(ev.task, "kworker/0:1H")

    def test_negative_status_and_marker(self):
        ev = parse_line(" a-1 [000] ..... 1.000002: urb_dequeue: (x+0x0/0x10) "
                        "urb=0x10 status=-2")
        self.assertEqual(ev.int_field("status"), -2)
        mk = parse_line(" python3-100 [000] ..... 1.5: tracing_mark_write: "
                        "hostlab_start mono_ns=1500000000 real_ns=17")
        self.assertEqual(mk.fields["mono_ns"], "1500000000")

    def test_comments_and_garbage_skipped(self):
        evs = list(parse(["# tracer: nop\n", "\n", "not a trace line\n",
                          " a-1 [000] ..... 1.0: e: x=1\n"]))
        self.assertEqual(len(evs), 1)

    def test_ts_precision(self):
        self.assertEqual(parse_ts("1", "5"), 1_500_000_000)
        self.assertEqual(parse_ts("0", "000000001"), 1)


if __name__ == "__main__":
    unittest.main()
