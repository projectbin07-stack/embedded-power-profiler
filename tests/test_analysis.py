"""End-to-end analysis of synthetic traces, compared with the generator's ground truth."""

from __future__ import annotations

import math

import pytest

from conftest import atmega, esp32
from embedded_power_profiler.analysis import Options, analyze
from embedded_power_profiler.states import Band, StateSpec
from embedded_power_profiler.synth import PROFILES, generate
from embedded_power_profiler.trace import Trace


def _truth_level(truth_name: str, truth: object) -> float:
    from embedded_power_profiler.synth import Truth

    assert isinstance(truth, Truth)
    return truth.charge_mah(truth_name) * 3600.0 / truth.time_s(truth_name)


def test_esp32_states_match_the_ground_truth() -> None:
    _, truth, r = esp32()
    profile = PROFILES["esp32-wifi"]
    assert len(r.states) == 5
    # States come out in order of current: sleep 25 uA, sensor 18 mA, boot 42 mA, Wi-Fi 95 mA,
    # MQTT 190 mA. Edge samples (the ADC averages across a transition) pull short bursts
    # slightly towards their neighbours, hence 3 % on the levels.
    order = ["deep sleep", "sensor read", "boot", "Wi-Fi connect", "MQTT publish"]
    for state, name in zip(r.states, order, strict=False):
        assert state.level_ma == pytest.approx(_truth_level(name, truth), rel=0.03), name
        assert state.events == truth.events(name)
    assert r.states[0].base and r.states[0].name == "sleep"
    # Typical durations: within one 20 ms sample of the profile's values.
    durations = {s.name: s.duration_s for s in profile.wake}
    for state, name in zip(r.states[1:], order[1:], strict=False):
        assert state.median_duration_s == pytest.approx(durations[name], abs=0.021), name
        assert state.median_period_s == pytest.approx(60.0, abs=0.05)
    # Charge is conserved by construction: within 0.05 % of the true daily charge.
    assert r.mah_per_day == pytest.approx(truth.mah_per_day(), rel=5e-4)
    assert sum(s.mah_per_day for s in r.states) == pytest.approx(r.mah_per_day)
    assert sum(s.time_share for s in r.states) == pytest.approx(1.0)
    assert sum(s.charge_share for s in r.states) == pytest.approx(1.0)
    # Per-state daily charge within 2.5 % (the edge samples move a little charge between states).
    for state, name in zip(r.states, order, strict=False):
        assert state.mah_per_day == pytest.approx(truth.mah_per_day(name), rel=0.025), name


def test_esp32_cycle_and_outliers() -> None:
    _, truth, r = esp32()
    assert r.cycle.acf_period_s == pytest.approx(60.0, abs=0.1)
    assert r.cycle.median_wake_interval_s == pytest.approx(60.0, abs=0.05)
    assert r.cycle.wakes == 30
    # Every long reconnect (4 x 2 s Wi-Fi connect) is flagged, and nothing else.
    outlier_wakes = [w for w in r.wakes if w.outlier]
    assert len(outlier_wakes) == len(truth.outlier_starts) == 4
    for w, t in zip(outlier_wakes, truth.outlier_starts, strict=False):
        assert w.start <= t <= w.end
    outlier_events = [e for e in r.events if e.outlier]
    assert {e.state for e in outlier_events} == {"radio 94.9 mA"}
    assert [round(e.duration) for e in outlier_events] == [8, 8, 8, 8]


def test_esp32_budget_voltage_and_warnings() -> None:
    _, truth, r = esp32()
    assert r.voltage_source == "measured"
    assert r.mean_voltage_v == pytest.approx(3.3, abs=0.01)
    assert r.mwh_per_day is not None
    # V = 3.3 - 0.2 ohm * I: energy is a little under charge x 3.3 V.
    assert r.mwh_per_day == pytest.approx(r.mah_per_day * 3.3, rel=0.01)
    assert r.mwh_per_day < r.mah_per_day * 3.3
    codes = [w.code for w in r.warnings]
    assert codes == ["quantisation"]  # 25 uA on a 10 uA ADC; nothing else at 50 Hz
    js = r.to_json()
    assert js["budget"]["mah_per_day"] == pytest.approx(truth.mah_per_day(), rel=5e-4)
    assert js["input"]["adc_step_ma"] == pytest.approx(0.01)
    assert len(js["outliers"]["wakes"]) == 4


def test_atmega_profile_and_short_burst_warning() -> None:
    _, truth, r = atmega()
    assert [s.name for s in r.states] == ["sleep", "low-power", "active"]
    assert r.states[0].level_ma == pytest.approx(0.0045, rel=0.01)
    assert r.states[1].level_ma == pytest.approx(1.6, rel=0.02)
    # 6 ms TX at 1 kHz = 6 samples: level within 10 %, and a sample-rate warning.
    assert r.states[2].level_ma == pytest.approx(13.5, rel=0.1)
    assert r.mah_per_day == pytest.approx(truth.mah_per_day(), rel=2e-3)
    warn = [w for w in r.warnings if w.code == "sample-rate"]
    assert len(warn) == 1 and "'active'" in warn[0].message and "6 samples" in warn[0].message
    assert r.cycle.acf_period_s == pytest.approx(8.0, abs=0.05)


def test_undersampled_log_is_flagged() -> None:
    # The same ESP32 node logged at the firmware's default RATE 500 (2 Hz).
    trace, _truth = generate("esp32-wifi", seed=1, duration_s=900.0, rate_hz=2.0)
    r = analyze(trace)
    sample_rate = [w for w in r.warnings if w.code == "sample-rate"]
    assert sample_rate, "a 2 Hz log of 0.3-2 s bursts must be flagged"
    assert any("f_s >= 10/d" in w.message or "f_s >= 2/d" in w.message for w in sample_rate)
    # Fewer states are resolved than exist.
    assert len(r.states) < 5


def test_min_burst_option() -> None:
    trace, _ = generate("esp32-wifi", seed=3, duration_s=200.0)
    r = analyze(trace, Options(min_burst_s=0.005))  # 5 ms bursts at 50 Hz: hopeless
    msgs = [w.message for w in r.warnings if w.code == "sample-rate"]
    assert any("at most 2.5 ms" in m for m in msgs)
    r = analyze(trace, Options(min_burst_s=0.1))  # 100 ms: 5 samples, marginal
    msgs = [w.message for w in r.warnings if w.code == "sample-rate"]
    assert any("only 5 samples" in m for m in msgs)
    r = analyze(trace, Options(min_burst_s=1.0))
    assert not [w for w in r.warnings if w.code == "sample-rate"]


def test_gaps_are_excluded_and_scaled() -> None:
    trace, truth = generate("esp32-wifi", seed=2, duration_s=600.0, gaps=[(130.0, 250.0)])
    r = analyze(trace)
    assert [w.code for w in r.warnings][:1] == ["gaps"]
    assert r.covered_s == pytest.approx(480.0, abs=0.1)
    # Two wakes fell into the gap; the analysed 480 s hold 8 complete ones.
    assert r.cycle.wakes == 8
    covered = sum(
        (min(iv.end, b) - max(iv.start, a)) * iv.ma / 3600.0
        for iv in truth.intervals
        for a, b in ((0.0, 130.0), (250.0, 600.0))
        if iv.end > a and iv.start < b
    )
    assert r.charge_mah == pytest.approx(covered, rel=2e-3)
    assert r.mah_per_day == pytest.approx(covered * 86400 / 480.0, rel=2e-3)
    assert any(e.clipped for e in r.events)


def test_states_spec_names_and_merges() -> None:
    trace, truth = generate("esp32-wifi", seed=4, duration_s=300.0)
    spec = StateSpec(
        bands=[
            Band("asleep", 0.0, 1.0),
            Band("radio", 60.0, math.inf),  # Wi-Fi and MQTT merged into one state
        ],
        penalty=3.0,
    )
    opts = Options(spec=spec)
    r = analyze(trace, opts)
    assert opts.penalty == 3.0
    names = [s.name for s in r.states]
    assert names[0] == "asleep" and names[3] == "radio" and len(names) == 4
    assert names[1].startswith("active 18") and names[2].startswith("active 4")
    radio = r.state("radio")
    expected = truth.charge_mah("Wi-Fi connect") + truth.charge_mah("MQTT publish")
    assert radio.charge_mah == pytest.approx(expected, rel=0.02)
    with pytest.raises(KeyError):
        r.state("nope")


def test_auto_names_do_not_collide_with_bands() -> None:
    trace, _ = generate("atmega-nrf24", seed=2, duration_s=40.0)
    r = analyze(trace, Options(spec=StateSpec(bands=[Band("active", 100.0, 200.0)])))
    assert "active (auto)" in [s.name for s in r.states]


def test_voltage_option_and_missing_voltage() -> None:
    trace, _ = generate("atmega-nrf24", seed=1, duration_s=40.0)
    bare = Trace(trace.t, trace.current_ma, None, "csv", "bare")
    r = analyze(bare)
    assert r.mwh_per_day is None and r.voltage_source == "none"
    assert all(s.energy_mwh is None for s in r.states)
    r = analyze(bare, Options(voltage_v=3.0))
    assert r.voltage_source == "given"
    assert r.mwh_per_day == pytest.approx(r.mah_per_day * 3.0)
    assert r.base_state.voltage_v == 3.0


def test_device_counter_mismatch_and_short_log() -> None:
    trace, _ = generate("esp32-wifi", seed=5, duration_s=50.0)
    trace.device_charge_mah = 10.0  # far from the integral
    r = analyze(trace, Options(capacity_mah=1000.0))
    codes = [w.code for w in r.warnings]
    assert "device-counter" in codes and "short-log" in codes
    assert r.battery is not None
    assert r.battery.days_without_self_discharge == pytest.approx(800.0 / r.mah_per_day)
