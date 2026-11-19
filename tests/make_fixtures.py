"""Regenerate the fixtures in tests/fixtures/ with the project's own writers.

    python tests/make_fixtures.py

Every file holds the same synthetic trace (profile ``esp32-wifi``, seed 7, 125 s at 25 Hz, so
two complete wakes at 60 s spacing) in one of the supported input formats, plus a few small
hand-written files for edge cases. The values asserted in the tests come from the generator's
ground truth, not from running the analysis on these files.
"""

from __future__ import annotations

from pathlib import Path

from embedded_power_profiler.synth import generate
from embedded_power_profiler.writers import (
    write_csv,
    write_ina219_log,
    write_logger_csv,
    write_sslog,
    write_sslog_csv,
)

HERE = Path(__file__).parent / "fixtures"
SEED = 7
DURATION_S = 125.0
RATE_HZ = 25.0


def main() -> None:
    HERE.mkdir(exist_ok=True)
    trace, _ = generate("esp32-wifi", seed=SEED, duration_s=DURATION_S, rate_hz=RATE_HZ)
    write_csv(trace, HERE / "esp32_generic.csv")
    write_ina219_log(trace, HERE / "esp32_ina219.log", corrupt_every=500)
    write_logger_csv(trace, HERE / "esp32_logger.csv")
    write_sslog(trace, HERE / "esp32.sslog", seed=SEED)
    write_sslog_csv(trace, HERE / "esp32_sslog.csv")
    # Units in the header, semicolons, a comment line and a non-numeric row.
    (HERE / "units_semicolon.csv").write_text(
        "# exported by a bench supply\n"
        "Time [ms];Current (uA);Voltage [mV]\n"
        "0;20;3300\n"
        "100;20;3300\n"
        "200;40000;3290\n"
        "300;40000;3290\n"
        "n/a;n/a;n/a\n"
        "400;20;3300\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
