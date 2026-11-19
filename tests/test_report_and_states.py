from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from conftest import esp32
from embedded_power_profiler.analysis import Options, analyze
from embedded_power_profiler.html_report import render_html
from embedded_power_profiler.loaders import load
from embedded_power_profiler.states import (
    StatesFileError,
    default_names,
    format_ma,
    load_spec,
    parse_spec,
)
from embedded_power_profiler.synth import PROFILES, generate, states_toml
from embedded_power_profiler.trace import Trace


def test_report_is_self_contained() -> None:
    _, _, r = esp32()
    page = render_html(r)
    assert page.startswith("<!doctype html>")
    assert "<script" not in page.lower()
    assert not re.search(r"""(?:src|href)=["']https?:""", page)  # no CDN, no remote assets
    assert "@import" not in page
    assert "prefers-color-scheme:dark" in page
    # Linked static zoom panels: overview boxes link to panels with matching ids.
    for wid in ("a", "b", "c"):
        assert f'href="#zoom-{wid}"' in page and f'id="zoom-{wid}"' in page
    assert "largest outlier wake" in page
    assert page.count("<svg") == 5  # overview, 3 zooms, energy bar
    assert "Charge per day by state" in page
    assert "quantisation" in page


def test_report_escapes_names_and_handles_gaps() -> None:
    trace, _ = generate("esp32-wifi", seed=2, duration_s=300.0, gaps=[(100.0, 150.0)])
    trace.name = "<b>evil</b>.csv"
    r = analyze(trace)
    page = render_html(r, event_limit=5)
    assert "<b>evil</b>" not in page and "&lt;b&gt;evil&lt;/b&gt;" in page
    assert 'class="gapbox"' in page
    assert "Showing the first 5 of" in page


def test_report_without_voltage_or_outliers() -> None:
    trace, _ = generate("atmega-nrf24", seed=1, duration_s=40.0)
    r = analyze(
        Trace(trace.t, trace.current_ma, None, "csv", "bare.csv"), Options(capacity_mah=200)
    )
    page = render_html(r)
    assert "(no voltage)" in page and "No outlier wakes." in page
    assert "Battery life" in page and 'id="zoom-b"' not in page


def test_report_on_a_tiny_log(fixtures: Path) -> None:
    r = analyze(load(fixtures / "units_semicolon.csv"))
    page = render_html(r)
    assert "</html>" in page


def test_states_file_toml_and_json(tmp_path: Path) -> None:
    toml = tmp_path / "s.toml"
    toml.write_text(
        "[analysis]\npenalty = 3\nmin_samples = 3\nmax_states = 4\n\n"
        '[[state]]\nname = "sleep"\nmax_ma = 1\n\n[[state]]\nname = "tx"\nmin_ma = 100\n',
        encoding="utf-8",
    )
    spec = load_spec(toml)
    assert (spec.penalty, spec.min_samples, spec.max_states) == (3.0, 3, 4)
    assert spec.match(0.02) == "sleep" and spec.match(150) == "tx" and spec.match(50) is None
    js = tmp_path / "s.json"
    js.write_text(json.dumps({"states": [{"name": "x", "min_ma": 1, "max_ma": 2}]}), "utf-8")
    assert load_spec(js).match(1.5) == "x"
    bad = tmp_path / "bad.toml"
    bad.write_text("[[state]\n", encoding="utf-8")
    with pytest.raises(StatesFileError):
        load_spec(bad)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ([], "table/object"),
        ({"foo": 1}, "unknown key"),
        ({"analysis": 3}, "must be a table"),
        ({"analysis": {"penalty": -1}}, "positive"),
        ({"analysis": {"penalty": "x"}}, "must be a number"),
        ({"analysis": {"max_states": 0}}, "positive integer"),
        ({"analysis": {"other": 1}}, "unknown key analysis.other"),
        ({"state": {}}, "array of tables"),
        ({"state": [1]}, "must be a table"),
        ({"state": [{"name": ""}]}, "non-empty string"),
        ({"state": [{"name": "a", "colour": "red"}]}, "unknown key"),
        ({"state": [{"name": "a", "min_ma": 5, "max_ma": 1}]}, "below max_ma"),
    ],
)
def test_states_file_errors(data: object, message: str) -> None:
    with pytest.raises(StatesFileError, match=message):
        parse_spec(data)


def test_default_names_and_format() -> None:
    assert default_names([0.02, 18.0, 41.0, 95.0]) == [
        "sleep",
        "active 18 mA",
        "active 41 mA",
        "radio",
    ]
    assert default_names([3.0]) == ["low-power"]
    assert format_ma(0.0251) == "25.1 uA" and format_ma(186.0) == "186 mA"


def test_generated_states_file_names_every_state(tmp_path: Path) -> None:
    path = tmp_path / "esp.toml"
    path.write_text(states_toml("esp32-wifi"), encoding="utf-8")
    spec = load_spec(path)
    p = PROFILES["esp32-wifi"]
    for s in (p.sleep, *p.wake):
        assert spec.match(s.ma) == s.name
    _, _, r = esp32()
    named = analyze(r.trace, Options(spec=spec))
    assert sorted(s.name for s in named.states) == sorted(
        ["deep sleep", "boot", "sensor read", "Wi-Fi connect", "MQTT publish"]
    )
