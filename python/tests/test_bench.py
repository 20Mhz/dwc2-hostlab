# SPDX-License-Identifier: MIT
"""Benchmark summary tests on synthetic runs (not hardware data)."""
import unittest

from hostlab.bench import percentile, summarize


def run(profile, meds, n=10, exact=None):
    return [{"profile": profile, "latencies": [m] * n, "median": m, "user_latencies": [m * 2],
             "ctrl_urbs_ok": n, "ctrl_urbs_phases_exact": n if exact is None else exact,
             "with_dwc2_enqueue": None if profile in ("none", "fphase") else n,
             "device_urbs": n, "ambiguous": 0, "exit": 0} for m in meds]


class BenchTests(unittest.TestCase):
    def test_percentile_nearest_rank(self):
        xs = list(range(1, 101))
        self.assertEqual(percentile(xs, 50), 50)
        self.assertEqual(percentile(xs, 95), 95)
        self.assertEqual(percentile([7], 95), 7)
        self.assertIsNone(percentile([], 50))

    def test_overhead_noise_and_cost_per_hit(self):
        runs = run("none", [100_000, 104_000]) + run("phase", [125_000, 126_000]) \
            + run("minimal", [102_000, 103_000])
        s = summarize(runs, ["none", "phase", "minimal"])
        self.assertEqual(s["none"]["median_ns"], 100_000)
        self.assertEqual(s["none"]["run_median_spread_ns"], 4_000)
        self.assertAlmostEqual(s["phase"]["overhead_median"], 0.25)
        self.assertTrue(s["phase"]["above_noise"])
        self.assertEqual(s["phase"]["cost_per_hit_ns"], 5_000)  # 25 us over 5 hits
        self.assertFalse(s["minimal"]["above_noise"])  # inside the baseline spread
        self.assertEqual((s["phase"]["join_ok"], s["phase"]["join_total"]), (20, 20))
        self.assertIsNone(s["none"]["phases_exact"])


if __name__ == "__main__":
    unittest.main()
