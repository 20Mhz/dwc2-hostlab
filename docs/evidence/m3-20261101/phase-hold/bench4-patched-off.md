# Instrumentation benchmark

- device 1:2, 3 rounds x 200 transfers per profile, seed 3, hostlab 062017b720ea2905e24cc67a8ed6c6f35031cdde-dirty
- kernel 6.12.47+rpt-rpi-v6, 2026-11-02T02:47:42Z .. 2026-11-02T02:49:30Z

Latency: usbmon submit -> complete of successful device control URBs.

| Profile | Hits | URBs | Median us | P95 us | Overhead (median) | Overhead (P95) | Run-median spread us | Above noise | Phases exact | Join |
|---|---|---|---|---|---|---|---|---|---|---|
| none | 0 | 600 | 171.0 | 207.0 | +0.0% | +0.0% | 1.0 | - | - | - |
| fast | 6 | 600 | 194.0 | 239.0 | +13.5% | +15.5% | 6.0 | yes | 600/600 | - |
| state | 11 | 600 | 257.0 | 317.0 | +50.3% | +53.1% | 7.0 | yes | 600/600 | 600/600 |
