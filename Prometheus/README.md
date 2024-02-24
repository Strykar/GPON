# Breakdown of the graphs:
It is split into three sections:

## 1) Common sources of attenuation
Reading the links listed here will help you troubleshoot GPON issues better.

Refer to the [HSGQ specification sheet](https://github.com/Strykar/GPON/blob/main/PON%20stick%20Spec.pdf) to understand the factory thresholds used in Grafana.

## 2) GPON / SFP metrics
_GPON Tx power_ (`-10 dBm` to `8 dBm`) / _Rx power_ (`-28 dBm` to `-8 dBm`) Error: `±2 dBm`

_SFP voltage_ (`3.015 V` to `3.56 V`)

Take the CPU and RAM usage with a grain of salt, their accuracy is not entirely clear.

## 3) Temperature and GPON signal metrics
_Commercial SoC temps_ `0°C` to `70°C`
_Industrial SoC temps_ `-40°C` to `85°C` (Add additional temperature thresholds in Grafana as appropriate if you own one)
_Bias Current_ `0 mA` to `130 mA`

_Signal Tx / Rx Power_
These two charts are (not-to-scale) representations of the daily fluctuation in signal power.

_PON Tx / Rx Power_
This is a poor attempt at a smokeping style graph to easily show the min and max values and visualize "jitter".
A Tx range showing `2 dBm` and `2.5 dBm` indicates less than `0.5 dBm` of jitter.
An Rx range (which is what your ISP tech usually concerns himself with the power meter) between `19 dBm` and `19.2 dBm` indicates less than `0.2 dBm` of jitter.
The screenshot shows an astonishingly stable connection with very low swings.

Your results or ideal ranges may not be as wide as this depending on distance to OLT / other sources of attenuation.
So ask your ISP for your location's ideal range and update the thresholds if required.

The charts are best seen at 24 hour views refreshing every five minutes.
