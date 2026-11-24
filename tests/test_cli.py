from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from embedded_power_profiler import __version__
from embedded_power_profiler.cli import EXIT_ERROR, EXIT_OK, EXIT_WARN, main
from embedded_power_profiler.loaders import load
from embedded_power_profiler.synth import generate
from embedded_power_profiler.writers import WRITE_FORMATS


def run(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    code = main(list(argv), out)
    return code, out.getvalue()


@pytest.fixture(scope="module")
def trace_csv(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("cli") / "esp32.csv"
    code, text = run("synth", "--profile", "esp32-wifi", "-o", str(path), "--duration", "400")
    assert code == EXIT_OK and "true charge" in text
    return path


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"epp {__version__}"


def test_analyze_text_report_and_events(trace_csv: Path, tmp_path: Path) -> None:
    report = tmp_path / "r.html"
    events = tmp_path / "e.csv"
    code, text = run(
        "analyze", str(trace_csv), "--report", str(report), "--events-csv", str(events),
        "--capacity-mah", "2500",
    )  # fmt: skip
    assert code == EXIT_OK
    assert "charge/day" in text and "battery       2500 mAh" in text
    assert "sleep *" in text and "wake period 60 s" in text
    assert report.read_text(encoding="utf-8").startswith("<!doctype html>")
    assert events.read_text(encoding="utf-8").startswith("index,state,start_s")


def test_analyze_json_and_strict(trace_csv: Path) -> None:
    code, text = run("analyze", str(trace_csv), "--json", "--strict")
    data = json.loads(text)
    assert data["tool"] == "embedded-power-profiler"
    assert len(data["states"]) == 5
    assert code == EXIT_WARN  # the quantisation warning
    code, _ = run("analyze", str(trace_csv), "--json")
    assert code == EXIT_OK


def test_analyze_options_reach_the_analysis(trace_csv: Path, tmp_path: Path) -> None:
    states = tmp_path / "s.toml"
    states.write_text('[analysis]\npenalty = 50\n[[state]]\nname = "zz"\nmax_ma = 1\n', "utf-8")
    code, text = run(
        "analyze", str(trace_csv), "--json", "--states", str(states), "--penalty", "2",
        "--min-samples", "3", "--max-states", "6", "--resample-ms", "20", "--min-burst-ms", "100",
    )  # fmt: skip
    data = json.loads(text)
    assert code == EXIT_OK
    assert data["segmentation"]["penalty"] == 2.0  # the command line wins over the file
    assert data["states"][0]["name"] == "zz"
    assert any("100 ms" in w["message"] for w in data["warnings"])


def test_export_partial_and_full(trace_csv: Path, tmp_path: Path) -> None:
    out = tmp_path / "sns.json"
    code, text = run("export", str(trace_csv), "--solar-node-sizer", str(out), "--name", "n")
    assert code == EXIT_OK and "load states" in text and "base" in text
    cfg = json.loads(out.read_text(encoding="utf-8"))
    assert cfg["name"] == "n" and cfg["load"][0]["base"] is True
    full = tmp_path / "full.json"
    events = tmp_path / "ev.csv"
    code, _ = run(
        "export", str(trace_csv), "--solar-node-sizer", str(full), "--full-config",
        "--measured-at", "battery", "--capacity-mah", "1800", "--events-csv", str(events),
        "--strict",
    )  # fmt: skip
    # --strict with warnings: exit 1 and nothing written (audit #41)
    assert code == EXIT_WARN
    assert not full.exists() and not events.exists()
    code, text = run(
        "export", str(trace_csv), "--solar-node-sizer", str(full), "--full-config",
        "--measured-at", "battery", "--capacity-mah", "1800", "--events-csv", str(events),
    )  # fmt: skip
    assert code == EXIT_OK and "also in the exported note" in text
    cfg = json.loads(full.read_text(encoding="utf-8"))
    assert cfg["regulatorPreset"] == "none" and cfg["battery"]["capacityMah"] == 1800
    assert "warning(s) (quantisation" in cfg["note"] and "[quantisation]" in cfg["note"]
    assert events.exists()


def test_export_measured_at_default_follows_the_source(trace_csv: Path, tmp_path: Path) -> None:
    """Audit #30: ina219 logs are measured on the battery side; generic CSVs at the supply."""
    log = tmp_path / "batt.log"
    code, _ = run("synth", "-o", str(log), "--format", "ina219", "--duration", "300")
    assert code == EXIT_OK
    out = tmp_path / "b.json"
    code, text = run("export", str(log), "--solar-node-sizer", str(out), "--full-config")
    assert code == EXIT_OK and "measured at the battery" in text
    cfg = json.loads(out.read_text(encoding="utf-8"))
    assert cfg["regulatorPreset"] == "none" and "regulator" not in cfg
    code, text = run("export", str(trace_csv), "--solar-node-sizer", str(out))
    assert "measured at the supply (generic CSV" in text


def test_amps_without_unit_exit_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Audit #40: was 0.1384 mAh/day with exit 0 for a log whose true figure is 138.4."""
    path = tmp_path / "si.csv"
    rows = [f"{k * 0.02:.2f},{0.00002 if k % 50 else 0.19},3.3" for k in range(500)]
    path.write_text("time,current,voltage\n" + "\n".join(rows) + "\n", encoding="utf-8")
    code, _ = run("analyze", str(path))
    assert code == EXIT_ERROR
    assert "--current-unit" in capsys.readouterr().err
    code, text = run("analyze", str(path), "--current-unit", "A", "--json")
    assert code == EXIT_OK
    data = json.loads(text)
    mean_ma = (0.19 * 10 + 0.00002 * 490) / 500 * 1000
    assert data["budget"]["mean_current_ma"] == pytest.approx(mean_ma, rel=0.02)


def test_short_log_warns_about_the_daily_extrapolation(tmp_path: Path) -> None:
    """Audit #48: 2.25 s extrapolated to a day with no warning."""
    path = tmp_path / "short.csv"
    path.write_text(
        "time_s,current_ma\n" + "".join(f"{k * 0.5},{152 if k % 3 else 20}\n" for k in range(6)),
        encoding="utf-8",
    )
    code, text = run("analyze", str(path), "--strict")
    assert code == EXIT_WARN and "extrapolated x" in text


@pytest.mark.parametrize("fmt", WRITE_FORMATS)
def test_synth_writes_every_format(tmp_path: Path, fmt: str) -> None:
    path = tmp_path / f"t.{fmt}"
    code, _ = run(
        "synth", "--profile", "atmega-nrf24", "-o", str(path), "--format", fmt,
        "--duration", "10", "--rate", "200", "--seed", "3",
    )  # fmt: skip
    assert code == EXIT_OK
    trace = load(path)
    expected, _ = generate("atmega-nrf24", seed=3, duration_s=10.0, rate_hz=200.0)
    assert len(trace) == len(expected) == 2000
    # The ina219 formats store integer microamps: 0.1 uA steps round to 1 uA.
    tol = 1e-6 if fmt == "csv" else 5e-4
    assert trace.current_ma[:50] == pytest.approx(expected.current_ma[:50], abs=tol)


def test_synth_states_out(tmp_path: Path) -> None:
    states = tmp_path / "st.toml"
    code, _text = run(
        "synth", "-o", str(tmp_path / "x.csv"), "--duration", "5", "--states-out", str(states)
    )
    assert code == EXIT_OK and "MQTT publish" in states.read_text(encoding="utf-8")


def test_errors_exit_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, _ = run("analyze", str(tmp_path / "missing.csv"))
    assert code == EXIT_ERROR
    assert "epp: error" in capsys.readouterr().err
    bad = tmp_path / "bad.csv"
    bad.write_text("a,b\n1,2\n", encoding="utf-8")
    code, _ = run("analyze", str(bad))
    assert code == EXIT_ERROR
    states = tmp_path / "s.toml"
    states.write_text("[[state]]\nname = 5\n", encoding="utf-8")
    good = tmp_path / "g.csv"
    good.write_text("time_s,current_ma\n0,1\n1,1\n2,1\n", encoding="utf-8")
    code, _ = run("analyze", str(good), "--states", str(states))
    assert code == EXIT_ERROR
    with pytest.raises(SystemExit):
        main(["analyze", str(good), "--dod", "1.5"])
    with pytest.raises(SystemExit):
        main(["analyze", str(good), "--self-discharge", "x"])
    with pytest.raises(SystemExit):
        main(["analyze", str(good), "--min-samples", "0"])
    with pytest.raises(SystemExit):
        main(["synth", "-o", "x", "--rate", "-1"])
