# Instrumentation benchmark

- device 1:2, 5 rounds x 200 transfers per profile, seed 1, hostlab cd285cb7f5a2803e399c36a18607db09a08131e8
- kernel 6.12.47+rpt-rpi-v6, 2026-11-02T01:51:52Z .. 2026-11-02T01:58:49Z

Latency: usbmon submit -> complete of successful device control URBs.

| Profile | Hits | URBs | Median us | P95 us | Overhead (median) | Overhead (P95) | Run-median spread us | Above noise | Phases exact | Join |
|---|---|---|---|---|---|---|---|---|---|---|
| channel | 8 | 1000 | 236.0 | 278.0 | +37.2% | +28.1% | 7.0 | yes | 1000/1000 | 1000/1000 |
| fphase | 3 | 1000 | 195.0 | 234.0 | +13.4% | +7.8% | 4.0 | yes | 1000/1000 | - |
| full | 17 | 1000 | 262.0 | 307.0 | +52.3% | +41.5% | 8.0 | yes | 1000/1000 | 1000/1000 |
| idle | 0 | 1000 | 172.0 | 209.0 | +0.0% | -3.7% | 5.0 | no | 0/1000 | - |
| minimal | 2 | 1000 | 203.0 | 244.0 | +18.0% | +12.4% | 5.0 | yes | 0/1000 | 1000/1000 |
| none | 0 | 1000 | 172.0 | 217.0 | +0.0% | +0.0% | 5.0 | - | - | - |
| phase | 5 | 1000 | 224.0 | 271.0 | +30.2% | +24.9% | 11.0 | yes | 1000/1000 | 1000/1000 |
