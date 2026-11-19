# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-11-19

### Added

- Loaders for ina219-battery-monitor raw logs (`$BM1` frames, CRC-8) and `tools/logger.py` CSV,
  serial-scope `.sslog` logs (rotated sets, truncated files) and decoded CSV exports, and generic
  CSV with units from the header or the command line. Device clock unwrapping, restart handling,
  flag handling, gap detection and uniform resampling.
- State segmentation: PELT on log10 current with a mean-and-variance cost and a quantisation
  floor, restricted to candidate positions; short-segment folding; 1-D Gaussian-mixture level
  clustering sized by BIC; TOML/JSON states files.
- Duty-cycle analysis per state, wake period by autocorrelation (numpy optional), outlier events
  and wakes, daily charge and energy, battery life with solar-node-sizer v0.1.1's self-discharge
  convention, sample-rate, aliasing, quantisation and device-counter warnings.
- Exports: solar-node-sizer load profile and full config (checked against solar-node-sizer
  v0.1.1 in CI), events CSV, JSON, self-contained HTML report with linked zoom panels.
- `epp analyze | export | synth` CLI; seeded synthetic profiles (`esp32-wifi`, `atmega-nrf24`)
  with ground truth and writers for every input format.

[0.1.0]: https://github.com/projectbin07-stack/embedded-power-profiler/releases/tag/v0.1.0
