from __future__ import annotations

from pathlib import Path

import pytest

from embedded_power_profiler.bm1 import Frame
from embedded_power_profiler.loaders import (
    CsvOptions,
    detect_format,
    load,
    load_generic_csv,
    load_logger_csv,
    load_sslog_csv,
    split_unit,
)
from embedded_power_profiler.synth import generate
from embedded_power_profiler.trace import TraceError

SOURCE, _ = generate("esp32-wifi", seed=7, duration_s=125.0, rate_hz=25.0)


@pytest.mark.parametrize(
    ("name", "fmt"),
    [
        ("esp32_generic.csv", "csv"),
        ("esp32_ina219.log", "ina219"),
        ("esp32_logger.csv", "logger-csv"),
        ("esp32.sslog", "sslog"),
        ("esp32_sslog.csv", "sslog-csv"),
    ],
)
def test_each_fixture_format_is_detected_and_loaded(fixtures: Path, name: str, fmt: str) -> None:
    path = fixtures / name
    assert detect_format(path) == fmt
    tr = load(path)
    assert tr.source == fmt
    # The ina219 fixture has every 500th frame corrupted (CRC must reject them): 6 of 3125.
    expected = 3125 - 6 if fmt == "ina219" else 3125
    assert len(tr) == expected
    assert tr.voltage_v is not None
    # Same samples as the generator produced (device clock starts 1 s after boot).
    offset = tr.t[0] - SOURCE.t[0]
    assert offset in (0.0, pytest.approx(1.0))
    lookup = dict(zip(SOURCE.t, SOURCE.current_ma, strict=False))
    for t, i in list(zip(tr.t, tr.current_ma, strict=False))[:400]:
        assert i == pytest.approx(lookup[round(t - offset, 6)], abs=5e-4)


def test_ina219_log_counts_rejected_lines(fixtures: Path) -> None:
    tr = load(fixtures / "esp32_ina219.log")
    assert "6 lines failed CRC or format checks and were skipped" in tr.notes
    # Device coulomb counter: frames carry the running charge; 125 s of the trace.
    assert tr.device_charge_mah is not None and tr.device_charge_mah > 0.1


def _frames_log(path: Path, frames: list[Frame], extra: list[str] | None = None) -> None:
    lines = ["#ina219-battery-monitor 0.1.0", *(extra or []), *(f.encode() for f in frames)]
    path.write_text("\r\n".join(lines) + "\r\n", encoding="ascii")


def test_ina219_restart_wrap_and_flags(tmp_path: Path) -> None:
    f = [
        Frame(0, 1000, 3300, 1000, 3300, 0, 0, 1000, 0),
        Frame(1, 1500, 3300, 1000, 3300, 1, 1, 1000, 2),  # not-ready: kept
        Frame(2, 2000, 3300, 0, 0, 0, 0, 1000, 4),  # sensor error: dropped
        Frame(3, 4294967000, 3300, 2000, 6600, 2, 2, 1000, 0),
        Frame(4, 200, 3300, 2000, 6600, 3, 3, 1000, 0),  # clock wrapped: seq continues
        Frame(0, 500, 3300, 3000, 9900, 0, 0, 1000, 1),  # restart, but overflow: dropped
        Frame(1, 1000, 3300, 3000, 9900, 5, 0, 1000, 0),  # restart
    ]
    path = tmp_path / "r.log"
    _frames_log(path, f, ["$BM1,garbage*00"])
    tr = load(path)
    assert tr.current_ma == [1.0, 1.0, 2.0, 2.0, 3.0]
    # Times: 1.0, 1.5, 4294967.0, (2^32 + 200) ms, then the restart run 60 s after the last one.
    wrapped = (2**32 + 200) / 1000.0
    assert tr.t[:4] == pytest.approx([1.0, 1.5, 4294967.0, wrapped])
    assert tr.t[4] == pytest.approx(wrapped + 60.0)
    notes = " | ".join(tr.notes)
    assert "1 lines failed" in notes
    assert "flagged overflow/sensor-error" in notes
    assert "not-ready" in notes
    assert "wrapped 1 time" in notes
    assert "restarted 1 time(s); 1 without host time" in notes
    # Counter: run 1 goes 0 -> 3 uAh, run 2 contributes 0 (one sample).
    assert tr.device_charge_mah == pytest.approx(0.003)


def test_logger_csv_restart_placed_by_host_time(tmp_path: Path) -> None:
    path = tmp_path / "l.csv"
    path.write_text(
        "host_time,seq,t_ms,bus_v,current_ma,power_mw,charge_mah,energy_mwh,soc_pct,flags\n"
        "2026-11-19T10:00:00.000+00:00,0,1000,3.300,1.000,3.300,0.000,0.000,100.0,0\n"
        "2026-11-19T10:00:01.000+00:00,1,2000,3.300,1.000,3.300,0.000,0.000,100.0,0\n"
        "2026-11-19T10:00:31.000+00:00,0,500,3.300,2.000,6.600,0.000,0.000,100.0,0\n"
        "broken,row\n",
        encoding="utf-8",
    )
    tr = load_logger_csv(path)
    assert tr.t == pytest.approx([1.0, 2.0, 32.0])
    assert "1 malformed rows skipped" in tr.notes
    assert any("runs placed by host time" in n for n in tr.notes)


def test_sslog_csv_skips_other_sources(tmp_path: Path) -> None:
    path = tmp_path / "s.csv"
    path.write_text(
        "host_time,device_time,source,bus_v,current_ma,flags\n"
        "100.0,1.0,BM1,3.3,5,0\n"
        "100.5,,TEXT,,,\n"
        "101.0,2.0,BM1,3.3,6.5,0\n"
        "x,y,BM1,3.3,1,0\n",
        encoding="utf-8",
    )
    tr = load_sslog_csv(path)
    assert tr.t == [1.0, 2.0] and tr.current_ma == [5.0, 6.5]
    assert tr.device_charge_mah is None
    assert "1 rows without a current value skipped" in tr.notes


def test_sslog_csv_without_current_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "s.csv"
    path.write_text("host_time,device_time,source,speed\n1,1,X,3\n", encoding="utf-8")
    assert detect_format(path) == "csv"
    with pytest.raises(TraceError, match="no current_ma channel"):
        load_sslog_csv(path)


def test_split_unit() -> None:
    assert split_unit("time_ms") == ("time", "ms")
    assert split_unit("Current (uA)") == ("current", "ua")
    assert split_unit("I [µA]") == ("i", "ua")
    assert split_unit("Voltage [mV]") == ("voltage", "mv")
    assert split_unit("host_time") == ("host_time", None)
    assert split_unit("current") == ("current", None)


def test_generic_csv_units_from_header(fixtures: Path) -> None:
    tr = load(fixtures / "units_semicolon.csv")
    assert tr.t == pytest.approx([0.0, 0.1, 0.2, 0.3, 0.4])
    assert tr.current_ma == pytest.approx([0.02, 0.02, 40.0, 40.0, 0.02])
    assert tr.voltage_v == pytest.approx([3.3, 3.3, 3.29, 3.29, 3.3])
    assert any("1 rows without a numeric" in n for n in tr.notes)


def test_generic_csv_explicit_columns_and_defaults(tmp_path: Path) -> None:
    path = tmp_path / "g.csv"
    path.write_text("stamp,amps,other\n0,0.001,5\n1,0.002,5\n2,0.003,5\n", encoding="utf-8")
    with pytest.raises(TraceError, match="no time column"):
        load(path)
    opts = CsvOptions(time_col="stamp", current_col="amps", current_unit="a")
    tr = load_generic_csv(path, opts)
    assert tr.current_ma == pytest.approx([1.0, 2.0, 3.0])
    assert tr.voltage_v is None
    with pytest.raises(TraceError, match="not found"):
        load_generic_csv(path, CsvOptions(time_col="nope"))
    with pytest.raises(TraceError, match="unknown current unit"):
        load_generic_csv(path, CsvOptions(time_col="stamp", current_col="amps", current_unit="kA"))


def test_generic_csv_assumed_units_and_iso_time(tmp_path: Path) -> None:
    path = tmp_path / "iso.csv"
    path.write_text(
        "timestamp,current,v\n"
        "2026-11-19T10:00:00Z,5,3.3\n"
        "2026-11-19T10:00:00.500Z,6,3.3\n"
        "2026-11-19T10:00:01Z,7,3.3\n",
        encoding="utf-8",
    )
    tr = load(path)
    assert [t - tr.t[0] for t in tr.t] == pytest.approx([0.0, 0.5, 1.0])
    notes = " ".join(tr.notes)
    assert "assumed mA" in notes and "assumed V" in notes and "ISO 8601" in notes


def test_errors(tmp_path: Path) -> None:
    empty = tmp_path / "e.csv"
    empty.write_text("# nothing\n", encoding="utf-8")
    with pytest.raises(TraceError, match="empty"):
        load(empty)
    with pytest.raises(TraceError, match="unknown format"):
        load(empty, "xml")
    one = tmp_path / "one.csv"
    one.write_text("time_s,current_ma\n0,1\n", encoding="utf-8")
    with pytest.raises(TraceError, match="fewer than two"):
        load(one)
    junk = tmp_path / "j.log"
    junk.write_text("$hello*00\n", encoding="ascii")
    with pytest.raises(TraceError, match="no valid ina219 frames"):
        load(junk)
    with pytest.raises(TraceError, match="not an ina219-battery-monitor logger CSV"):
        load(one, "logger-csv")
    with pytest.raises(TraceError, match="not a serial-scope CSV"):
        load(one, "sslog-csv")
