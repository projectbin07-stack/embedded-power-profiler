from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import pytest

from conftest import esp32
from embedded_power_profiler.analysis import Options, analyze
from embedded_power_profiler.export import (
    ExportError,
    load_states,
    solar_node_sizer_config,
    solar_node_sizer_profile,
    validate_sns_config,
    write_events_csv,
)
from embedded_power_profiler.synth import generate
from embedded_power_profiler.trace import Trace


def _sns_supply_mah_per_day(load: list[dict[str, Any]]) -> float:
    """solar-node-sizer's dailyBudget at the supply side, for states active all day.

    hourlyShares: a scheduled state takes min(1, duration / every) of every hour, the base state
    the rest; mAhPerDay = sum over 24 hours of share x mA.
    """
    busy = sum(min(1.0, s["duration"] / s["every"]) for s in load if not s.get("base"))
    total = 0.0
    for s in load:
        share = (1.0 - busy) if s.get("base") else min(1.0, s["duration"] / s["every"])
        total += 24.0 * share * s["mA"]
    return total


def test_profile_reproduces_the_measured_daily_charge() -> None:
    _, _, r = esp32()
    load = load_states(r)
    assert load[0]["base"] is True and load[0]["name"] == "sleep"
    assert all(s["every"] == pytest.approx(60.0, abs=0.01) for s in load[1:])
    assert _sns_supply_mah_per_day(load) == pytest.approx(r.mah_per_day, rel=1e-5)
    # Long reconnects are kept in the mean: Wi-Fi duration is above the 2 s median.
    wifi = next(s for s in load if s["name"].startswith("radio 94"))
    assert wifi["duration"] == pytest.approx((26 * 2.0 + 4 * 8.0) / 30, abs=0.03)


def test_partial_and_full_config() -> None:
    _, _, r = esp32()
    prof = solar_node_sizer_profile(r, "node A")
    assert list(prof) == ["name", "note", "load"]
    assert prof["name"] == "node A" and "measured at the supply" in prof["note"]
    full = solar_node_sizer_config(r, measured_at="supply")
    assert full["regulatorPreset"] == "ldo-lowiq"  # measured 3.3 V
    assert full["battery"]["dod"] == 0.8 and full["battery"]["capacityMah"] == 2500
    assert full["load"] == prof["load"]
    batt = solar_node_sizer_config(r, measured_at="battery")
    assert batt["regulatorPreset"] == "none" and "regulator" not in batt
    with pytest.raises(ExportError):
        solar_node_sizer_profile(r, measured_at="elsewhere")


def test_custom_regulator_at_other_voltage() -> None:
    trace, _ = generate("atmega-nrf24", seed=1, duration_s=40.0)  # 3.0 V supply
    r = analyze(trace, Options(capacity_mah=1200))
    cfg = solar_node_sizer_config(r)
    assert cfg["regulatorPreset"] == "custom"
    assert cfg["regulator"]["vout"] == pytest.approx(3.0, abs=0.02)
    assert cfg["battery"]["capacityMah"] == 1200
    validate_sns_config(json.loads(json.dumps(cfg)))


def test_no_voltage_partial_note() -> None:
    trace, _ = generate("atmega-nrf24", seed=1, duration_s=40.0)
    r = analyze(Trace(trace.t, trace.current_ma, None, "csv", "x.csv"))
    prof = solar_node_sizer_profile(r, measured_at="battery")
    assert prof["regulatorPreset"] == "none"
    assert "not recorded" in prof["note"]


@pytest.mark.parametrize(
    ("cfg", "message"),
    [
        ([], "JSON object"),
        ({"load": [], "extra": 1}, "unknown field extra"),
        ({"load": []}, "non-empty"),
        ({"load": ["x"]}, r"load\[0\] must be an object"),
        ({"load": [{"mA": 1, "base": True}]}, "name must be a string"),
        ({"load": [{"name": "a", "mA": -1, "base": True}]}, "mA must be a number"),
        ({"load": [{"name": "a", "mA": True, "base": True}]}, "mA must be a number"),
        ({"load": [{"name": "a", "mA": 1, "every": -5, "duration": 1}]}, "every must be"),
        ({"load": [{"name": "a", "mA": 1, "every": "5", "duration": 1}]}, "every must be"),
        ({"load": [{"name": "a", "mA": 1, "every": 5}]}, "needs"),
        ({"load": [{"name": "a", "mA": 1, "base": True, "to": 25}]}, "hours"),
        (
            {"load": [{"name": "a", "mA": 1, "base": True}, {"name": "b", "mA": 1, "base": True}]},
            "at most one",
        ),
    ],
)
def test_validator_mirrors_solar_node_sizer(cfg: Any, message: str) -> None:
    with pytest.raises(ExportError, match=message):
        validate_sns_config(cfg)


def test_bundled_solar_node_sizer_example_passes_the_validator() -> None:
    # The load section of solar-node-sizer v0.1.1 examples/esp32-wifi-10min.json.
    validate_sns_config(
        {
            "name": "x",
            "load": [
                {"name": "Deep sleep (RTC timer)", "mA": 0.015, "base": True},
                {"name": "Boot + BME280 read", "mA": 40, "every": 600, "duration": 0.3},
                {"name": "Wi-Fi connect + MQTT", "mA": 120, "every": 600, "duration": 3},
            ],
        }
    )


def test_events_csv(tmp_path: Path) -> None:
    _, _, r = esp32()
    path = tmp_path / "events.csv"
    n = write_events_csv(r, path)
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    assert n == len(rows) == len(r.events)
    assert rows[0]["clipped"] == "1"  # the log starts asleep part-way through a sleep
    total_uah = sum(float(x["charge_uah"]) for x in rows)
    assert total_uah / 1000 == pytest.approx(r.charge_mah, rel=1e-5)
    assert sum(1 for x in rows if x["outlier"]) == 4
