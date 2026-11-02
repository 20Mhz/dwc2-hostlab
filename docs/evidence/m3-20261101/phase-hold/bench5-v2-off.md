# Instrumentation benchmark

- device 1:2, 3 rounds x 200 transfers per profile, seed 3, hostlab 062017b720ea2905e24cc67a8ed6c6f35031cdde-dirty
- kernel 6.12.47+rpt-rpi-v6, 2026-11-02T03:09:31Z .. 2026-11-02T03:11:17Z

Latency: usbmon submit -> complete of successful device control URBs.

| Profile | Hits | URBs | Median us | P95 us | Overhead (median) | Overhead (P95) | Run-median spread us | Above noise | Phases exact | Join |
|---|---|---|---|---|---|---|---|---|---|---|
| none | 0 | 600 | 173.0 | 206.0 | +0.0% | +0.0% | 3.0 | - | - | - |
| fast | 6 | 600 | 195.0 | 232.0 | +12.7% | +12.6% | 2.0 | yes | 600/600 | - |
| state | 11 | 600 | 256.0 | 298.0 | +48.0% | +44.7% | 2.0 | yes | 600/600 | 600/600 |
