# SPDX-License-Identifier: MIT
"""TraceFS._write on a regular file: append adds, non-append replaces.

The real constraint (tracefs rejects Python's append-mode lseek with EINVAL)
cannot be reproduced on a regular file; it was found and verified on a Pi
Zero W running 6.12.47.
"""
import os
import tempfile
import unittest

from hostlab.tracefs import TraceFS


class WriteTests(unittest.TestCase):
    def test_append_and_replace(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "kprobe_events")
            with open(path, "w") as f:
                f.write("p:other/keep do_sys_open\n")
            t = TraceFS(d)
            t._write("kprobe_events", "p:hostlab/hl_x usb_kill_urb", append=True)
            with open(path) as f:
                self.assertEqual(f.read(), "p:other/keep do_sys_open\np:hostlab/hl_x usb_kill_urb\n")
            t._write("kprobe_events", "replaced")
            with open(path) as f:
                self.assertEqual(f.read(), "replaced\n")


if __name__ == "__main__":
    unittest.main()
