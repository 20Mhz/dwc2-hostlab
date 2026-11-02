# M3 evidence, Pi Zero W + CH340 (2026-11-01/02)

Kernel `6.12.47+rpt-rpi-v6`, dwc2 buffer DMA, device 1:2 = CH340 `1a86:7523`.
Results and interpretation: [docs/m3-results.md](../../m3-results.md).
`summary.json` condenses the reports below.

| Path | What | hostlab revision deployed | Command |
|---|---|---|---|
| `instrumentation/bench1.*` | 7 profiles x 5 rounds x 200 | `cd285cb` (reanalysed with `0d9f370`: URB join fix) | `hostlab-bench --device 1:2 --rounds 5 --repeat 200 --seed 1` |
| `instrumentation/bench2.*` | single-probe breakdown, 4 x 200 | `0d9f370` | `hostlab-bench ... --seed 2 --profiles none,k:urb_enqueue,k:giveback,k:hc_start,f:...` |
| `instrumentation/bench3.*` | none / fast / state, 3 x 200; `*-state/model.json` inside the tarball | `6e0c015` (model reports `ab7f7f5`) | `hostlab-bench ... --seed 3 --profiles none,fast,state --offsets ...` |
| `dwc2-offsets-6.12.47.json` | DWARF member/bitfield offsets | `9c3e5f5` + the DWARF 5 fix committed as `6e0c015` | `scripts/pi/dwc2-offsets.sh` |
| `e3/` | cancel sweep 0-300 us step 5 x 5, profiles state / fast / none | `06dfbb9` (reports `ab7f7f5`) | `scripts/pi/m3-run.sh e3` |
| `h3/` | cancel sweep 20-50 us step 1 x 10, state | `d7c1ed0` (reports `ab7f7f5`) | `scripts/pi/m3-run.sh h3` |
| `misscheck/` | cancel sweep 30-45 us x 5 with `kprobe_profile` | `6b58f16` | `hostlab-session run --profile state ...` |
| `e6/` | 5 resets; 5 resets with 4 URBs pending | `ab7f7f5` (deploy script uncommitted) | `scripts/pi/m3-run.sh e6` |
| `h5/` | 5 resets with usbcore `old_scheme_first=Y` (restored to N) | `ab7f7f5` (same) | `scripts/pi/m3-run.sh h5` |

Two resets before `e6/` ran with a stale `hostlab-ctl` (one plain reset
each, no data kept); `deploy.sh` now refuses stale ARMv6 binaries.

Raw benchmark sessions are in `instrumentation/bench*-runs.tar.xz`; extract
and run `hostlab-bench --out benchN --reanalyze` to recompute.
