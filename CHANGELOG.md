# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.2.0] - 2026-11-24

### Fixed

- High-rate captures were not feasible. A generic CSV was read whole into memory (every row as a
  list of strings) and the change-point search ran on every sample: a generated 3,000,000-row
  PPK2-style CSV (30 s at 100 kHz) was stopped after 734 s at about 877 MB. Generic CSVs are now
  read as a stream, and files sampled faster than 1 kHz are block-averaged to 1 ms bins (or to
  `--resample-ms`) while they are read; the bins preserve the charge. The same file now analyses
  in 10-14 s with a 43 MB peak working set and gives 96.57 mAh/day (hand value 96.57). On a
  300,000-row cut 0.1.0 took 147 s and 0.2.0 takes 1.2 s with an identical result table.
  Bursts shorter than about 2 ms are no longer resolved as states of their own by default.
- A generic CSV whose current column has no unit in its header was assumed to be in mA, so a log
  in amps read 1000x low with exit 0 (0.1384 instead of 138.4 mAh/day). It is now refused with
  exit 2 and a message naming the column, its first values and `--current-unit`. Columns named
  only by their unit (`mA`, `uA`, `A`, `mV`, `ms`) are recognised; before, `t_s,mA` failed with
  "no current column found". A voltage column without a unit reading 100 or more is refused
  instead of being taken as volts.
- ina219 frames flagged not-ready (no new conversion: a repeat, or zeros right after boot) were
  kept as samples; they are now dropped and counted in a note.
- `export --strict` returned 1 on warnings but still wrote the JSON; it now writes nothing.
  Analysis warnings are appended to the exported `note`, so they travel with the profile.
- `export --measured-at` defaulted to `supply` for every input, so a battery-side ina219 log
  became a "custom LDO" at the battery voltage in `--full-config`. The default is now `battery`
  for ina219-battery-monitor logs (raw, logger CSV, `.sslog`, serial-scope CSV) and `supply` for
  generic CSVs, and the export prints which it used.
- Daily figures extrapolated from less than 60 s of log now get a `short-log` warning
  (ina219's 2.25 s `sample_session.log` was extrapolated to 3650 mAh/day without one).

### Changed

- Interop CI pins: solar-node-sizer v0.1.1 -> v0.1.2 (also checks its new browser JSON import on
  the export), ina219-battery-monitor v0.1.0 -> v0.2.0, serial-scope v0.1.0 -> v0.3.0.

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
