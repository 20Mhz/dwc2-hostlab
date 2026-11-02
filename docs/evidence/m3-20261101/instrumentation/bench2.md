# Instrumentation benchmark

- device 1:2, 4 rounds x 200 transfers per profile, seed 2, hostlab 0d9f3705f908f7ebabec7d81f3c1dc02443b6d0f
- kernel 6.12.47+rpt-rpi-v6, 2026-11-02T02:02:12Z .. 2026-11-02T02:07:59Z

Latency: usbmon submit -> complete of successful device control URBs.

| Profile | Hits | URBs | Median us | P95 us | Overhead (median) | Overhead (P95) | Run-median spread us | Above noise | Phases exact | Join |
|---|---|---|---|---|---|---|---|---|---|---|
| none | 0 | 800 | 172.0 | 211.0 | +0.0% | +0.0% | 1.0 | - | - | - |
| k:urb_enqueue | ? | 800 | 194.0 | 250.0 | +12.8% | +18.5% | 11.0 | yes | 0/800 | 800/800 |
| k:giveback | ? | 800 | 189.0 | 225.0 | +9.9% | +6.6% | 3.0 | yes | 0/800 | - |
| k:hc_start | ? | 800 | 213.0 | 378.0 | +23.8% | +79.1% | 21.0 | yes | 800/800 | - |
| f:dwc2_hc_start_transfer | ? | 800 | 195.0 | 231.0 | +13.4% | +9.5% | 4.0 | yes | 800/800 | - |
| f:dwc2_hc_start_transfer+dwc2_release_channel | ? | 800 | 194.0 | 235.0 | +12.8% | +11.4% | 7.0 | yes | 800/800 | - |
| k:urb_enqueue/f:dwc2_hc_start_transfer | ? | 800 | 208.0 | 257.0 | +20.9% | +21.8% | 10.0 | yes | 800/800 | 800/800 |
