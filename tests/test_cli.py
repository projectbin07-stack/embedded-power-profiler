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
    assert code == EXIT_WARN
    cfg = json.loads(full.read_text(encoding="utf-8"))
    assert cfg["regulatorPreset"] == "none" and cfg["battery"]["capacityMah"] == 1800
    assert events.exists()


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
