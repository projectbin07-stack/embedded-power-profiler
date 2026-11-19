# Contributing

Bug reports, logs that are analysed wrongly, and pull requests are welcome.

## Development setup

```console
git clone https://github.com/projectbin07-stack/embedded-power-profiler.git
cd embedded-power-profiler
python -m venv .venv
.venv/bin/pip install -e ".[dev]"      # Windows: .venv\Scripts\pip
```

## Checks

All four must pass; CI runs them on Ubuntu and Windows with Python 3.10 and 3.13.

```console
ruff check .
ruff format --check .
mypy
pytest --cov
```

The interop tests run only when serial-scope is importable and `EPP_INA219_LOGGER` points to
ina219-battery-monitor's `tools/logger.py`; CI's interop job sets both up from pinned tags and also
checks the solar-node-sizer export with `scripts/check_solar_node_sizer.mjs`.

## Guidelines

- Keep the runtime free of third-party dependencies (numpy stays optional, with a pure-Python path
  that is tested to give the same result).
- New behaviour needs a test whose expected value comes from the synthetic generator's ground truth
  or from a derivation written in the test. Do not assert against numbers copied from the tool's
  own output.
- Fixtures are generated: change `tests/make_fixtures.py`, run `python tests/make_fixtures.py` and
  commit the regenerated files. Do not commit logs you are not allowed to share.
- Do not state accuracy figures for real hardware unless you measured them, with a reference
  instrument, and say how.
- Keep commits small with imperative messages (`Add ...`, `Fix ...`).

## Reporting a wrong result

Include the command, the `--json` output, what you expected, how the log was recorded (instrument,
shunt, sample rate) and, if possible, the shortest stretch of the log that reproduces it.
