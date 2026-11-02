# Instrumentation benchmark

- device 1:2, 3 rounds x 200 transfers per profile, seed 3, hostlab 6e0c015bd3af7baa0e19a1ba85abd8e1968f63e1
- kernel 6.12.47+rpt-rpi-v6, 2026-11-02T02:10:34Z .. 2026-11-02T02:12:19Z

Latency: usbmon submit -> complete of successful device control URBs.

| Profile | Hits | URBs | Median us | P95 us | Overhead (median) | Overhead (P95) | Run-median spread us | Above noise | Phases exact | Join |
|---|---|---|---|---|---|---|---|---|---|---|
| none | 0 | 600 | 172.0 | 219.0 | +0.0% | +0.0% | 4.0 | - | - | - |
| fast | 6 | 600 | 194.0 | 227.0 | +12.8% | +3.6% | 4.0 | yes | 600/600 | - |
| state | 11 | 600 | 255.0 | 300.0 | +48.3% | +37.0% | 2.0 | yes | 600/600 | 600/600 |
