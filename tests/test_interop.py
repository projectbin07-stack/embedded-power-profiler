"""Cross-checks against the other projects' own code, when it is available.

* serial-scope: ``pip install "git+https://github.com/projectbin07-stack/serial-scope@v0.3.0"``
  (CI does this) or put its ``src`` on ``PYTHONPATH``.
* ina219-battery-monitor: set ``EPP_INA219_LOGGER`` to the path of its ``tools/logger.py``.

The tests are skipped otherwise; CI's interop job runs them with pinned tags.
"""

from __future__ import annotations

import importlib.util
import io
import os
import sys
from pathlib import Path
from types import ModuleType

import pytest

from embedded_power_profiler.analysis import analyze
from embedded_power_profiler.loaders import load
from embedded_power_profiler.synth import generate
from embedded_power_profiler.writers import frames, write_ina219_log, write_sslog


def test_serial_scope_reads_our_sslog(tmp_path: Path) -> None:
    cli = pytest.importorskip("serial_scope.cli")
    trace, _ = generate("esp32-wifi", seed=11, duration_s=30.0)
    path = tmp_path / "ours.sslog"
    write_sslog(trace, path)
    stats = cli.log_stats(str(path))
    assert "ina219" in stats.decoder  # "auto (ina219)": detected from the content
    assert stats.samples == len(trace) == len(load(path))
    assert stats.decode_errors == 0 and not stats.truncated


def test_we_read_serial_scope_demo_log(tmp_path: Path) -> None:
    demo = pytest.importorskip("serial_scope.demo")
    cli = pytest.importorskip("serial_scope.cli")
    path = tmp_path / "theirs.sslog"
    demo.write_demo_log(path, "ina219")
    stats = cli.log_stats(str(path))
    trace = load(path)
    assert len(trace) == stats.samples
    assert any("failed CRC" in n for n in trace.notes) == (stats.decode_errors > 0)
    current = next(c for c in stats.channels if c.name == "current_ma")
    assert sum(trace.current_ma) / len(trace) == pytest.approx(current.mean, rel=1e-6)


def _logger() -> ModuleType:
    path = os.environ.get("EPP_INA219_LOGGER")
    if not path or not Path(path).is_file():
        pytest.skip("EPP_INA219_LOGGER not set to ina219-battery-monitor's tools/logger.py")
    spec = importlib.util.spec_from_file_location("ina219_logger", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["ina219_logger"] = module  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


def test_ina219_logger_decodes_our_frames_and_we_read_its_csv(tmp_path: Path) -> None:
    logger = _logger()
    trace, _ = generate("esp32-wifi", seed=12, duration_s=130.0)
    for frame in list(frames(trace))[:200]:
        theirs = logger.decode_frame(frame.encode())
        assert theirs.current_ua == frame.current_ua and theirs.t_ms == frame.t_ms
    raw = tmp_path / "raw.log"
    write_ina219_log(trace, raw, corrupt_every=1000)
    stats = logger.LogStats()
    out = tmp_path / "logger.csv"
    with raw.open(encoding="ascii") as src, out.open("w", newline="", encoding="utf-8") as dst:
        rows = logger.write_csv(logger.iter_frames(src, stats), dst)
    assert stats.bad_lines == 6 and rows == len(trace) - 6
    ours = analyze(load(raw))
    via_logger = analyze(load(out))
    assert via_logger.trace.source == "logger-csv"
    assert via_logger.mah_per_day == pytest.approx(ours.mah_per_day, rel=1e-9)
    assert [s.name for s in via_logger.states] == [s.name for s in ours.states]
    buf = io.StringIO()
    logger.write_csv([], buf)
    assert buf.getvalue().strip().split(",")[0] == "host_time"
