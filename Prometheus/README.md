# Breakdown of the graphs

The dashboard is split into three sections.

## 1) Common sources of attenuation

Reading the links listed here will help you troubleshoot GPON issues better.

Refer to the [HSGQ specification sheet](https://github.com/Strykar/GPON/blob/main/docs/PON%20stick%20Spec.pdf) to understand the factory thresholds used in Grafana.

## 2) GPON / SFP metrics

_GPON Tx power_ (`-10 dBm` to `8 dBm`) / _Rx power_ (`-28 dBm` to `-8 dBm`) Error: `±2 dBm`

_SFP voltage_ (`3.015 V` to `3.56 V`)

Take the CPU and RAM usage with a grain of salt, their accuracy is not entirely clear.

## 3) Temperature and GPON signal metrics

_Commercial SoC temps_ `0°C` to `70°C`

_Industrial SoC temps_ `-40°C` to `85°C` (Add additional temperature thresholds in Grafana as appropriate if you own one)

_Bias Current_ `0 mA` to `130 mA`

_Signal Tx / Rx Power_: these two charts plot the daily fluctuation in signal power against threshold-coloured bands. Green = inside factory range with margin, orange = at the edge, red = out of spec.

Your results may not match these defaults depending on distance to OLT and other sources of attenuation. Ask your ISP for your location's ideal range and adjust the thresholds in Grafana accordingly.

The charts are best seen at 24-hour views refreshing every minute.
