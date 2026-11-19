from __future__ import annotations

import math

import pytest

from embedded_power_profiler.battery import (
    autonomy_hours,
    battery_life,
    self_discharge_per_hour,
)


def _hour_by_hour(e0: float, floor: float, p: float, s: float) -> float:
    """Reference: step the solar-node-sizer order (self-discharge, then load) hour by hour."""
    e = e0
    hours = 0
    while True:
        after = e * (1 - s) - p
        if after < floor:
            return hours + max(0.0, min(1.0, ((1 - s) * e - floor) / p))
        e = after
        hours += 1


def test_self_discharge_per_hour() -> None:
    # (1 - s)^730 = 1 - m: 3 % a month -> 1 - 0.97^(1/730)
    assert self_discharge_per_hour(0.03) == pytest.approx(1 - 0.97 ** (1 / 730))
    assert (1 - self_discharge_per_hour(0.2)) ** 730 == pytest.approx(0.8)
    assert self_discharge_per_hour(0.0) == 0.0


def test_without_self_discharge_is_usable_over_daily() -> None:
    # 1000 mAh x 0.8 usable / 10 mAh per day = 80 days exactly.
    life = battery_life(1000, 10, 0.8, 0.0)
    assert life.days_without_self_discharge == pytest.approx(80.0)
    assert life.days_with_self_discharge == pytest.approx(80.0)


@pytest.mark.parametrize(
    ("e0", "floor", "p", "m"),
    [(2500, 500, 138.4 / 24, 0.03), (1000, 200, 0.5 / 24, 0.03), (2000, 0, 1.0, 0.2)],
)
def test_closed_form_matches_hour_by_hour(e0: float, floor: float, p: float, m: float) -> None:
    s = self_discharge_per_hour(m)
    assert autonomy_hours(e0, floor, p, s) == pytest.approx(_hour_by_hour(e0, floor, p, s))


def test_self_discharge_shortens_life() -> None:
    # 1000 mAh, 0.5 mAh/day: 800 / 0.5 = 1600 days without leakage. With 3 %/month:
    # s = 1 - 0.97^(1/730) = 4.1725e-5 per hour, q = p/s = (0.5/24)/s = 499.3 mAh and
    # k = ln((200 + q) / (1000 + q)) / ln(1 - s) = 0.76275 / 4.1726e-5 = 18280 h = 761.7 days.
    life = battery_life(1000, 0.5, 0.8, 0.03)
    assert life.days_without_self_discharge == pytest.approx(1600.0)
    assert life.days_with_self_discharge == pytest.approx(761.6, abs=0.2)
    js = life.to_json()
    assert js["capacity_mah"] == 1000 and js["days_with_self_discharge"] is not None


def test_edge_cases() -> None:
    assert autonomy_hours(100, 10, 0.0, 0.001) == math.inf
    assert autonomy_hours(10, 10, 1.0, 0.0) == 0.0
    assert battery_life(100, 0.0).to_json()["days_without_self_discharge"] is None
    with pytest.raises(ValueError):
        battery_life(0, 1)
    with pytest.raises(ValueError):
        battery_life(100, 1, depth_of_discharge=0)
    with pytest.raises(ValueError):
        battery_life(100, 1, self_discharge_per_month=1.0)
