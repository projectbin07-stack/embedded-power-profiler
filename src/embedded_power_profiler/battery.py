"""Battery-life estimate with the self-discharge convention of solar-node-sizer v0.1.1.

From solar-node-sizer's ``docs/models.md`` (section 10, "Days of autonomy") and
``src/core/simulate.js`` (``autonomyHours``): self-discharge removes a fraction ``s`` of the
*stored* energy each hour, with

    s = 1 - (1 - m)^(1/730)          (m = fraction lost per month, a month taken as 730 h)

and each hour applies self-discharge first, then the load ``p``. After ``k`` whole hours from a
full battery ``E0``:

    E_k = (E0 + p/s)(1 - s)^k - p/s       (s > 0)
    E_k = E0 - k p                        (s = 0)

Life is the last whole hour ``N`` with ``E_N >= floor`` plus the fraction
``((1 - s) E_N - floor) / p`` of the next hour. Here the battery is counted in mAh at the
measured current (the same proportionality holds for Wh at a fixed voltage), the floor is
``(1 - depth_of_discharge) * capacity``, and ``p`` is the measured mean charge per hour.
With ``s = 0`` the result is simply ``usable capacity / daily charge``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

DEFAULT_DOD = 0.8  # solar-node-sizer's Li-ion preset
DEFAULT_SELF_DISCHARGE = 0.03  # per month, solar-node-sizer's Li-ion preset


def self_discharge_per_hour(per_month: float) -> float:
    if per_month <= 0:
        return 0.0
    return float(1.0 - (1.0 - min(per_month, 0.99)) ** (1.0 / 730.0))


def autonomy_hours(e0: float, floor: float, load_per_hour: float, sdh: float) -> float:
    """Hours until ``e0`` falls to ``floor`` (a port of solar-node-sizer's ``autonomyHours``)."""
    if not load_per_hour > 0:
        return math.inf
    if e0 <= floor:
        return 0.0
    s = max(0.0, sdh)
    q = load_per_hour / s if s > 0 else 0.0

    def after(k: int) -> float:
        return (e0 + q) * (1 - s) ** k - q if s > 0 else e0 - k * load_per_hour

    if s > 0:
        k_real = math.log((floor + q) / (e0 + q)) / math.log(1 - s)
    else:
        k_real = (e0 - floor) / load_per_hour
    n = max(0, math.floor(k_real))
    while n > 0 and after(n) < floor:
        n -= 1
    while after(n + 1) >= floor:
        n += 1
    frac = ((1 - s) * after(n) - floor) / load_per_hour
    return n + min(1.0, max(0.0, frac))


@dataclass(frozen=True)
class BatteryLife:
    capacity_mah: float
    depth_of_discharge: float
    self_discharge_per_month: float
    mah_per_day: float
    days_without_self_discharge: float
    days_with_self_discharge: float

    def to_json(self) -> dict[str, float | None]:
        def finite(x: float) -> float | None:
            return x if math.isfinite(x) else None

        return {
            "capacity_mah": self.capacity_mah,
            "depth_of_discharge": self.depth_of_discharge,
            "self_discharge_per_month": self.self_discharge_per_month,
            "mah_per_day": self.mah_per_day,
            "days_without_self_discharge": finite(self.days_without_self_discharge),
            "days_with_self_discharge": finite(self.days_with_self_discharge),
        }


def battery_life(
    capacity_mah: float,
    mah_per_day: float,
    depth_of_discharge: float = DEFAULT_DOD,
    self_discharge_per_month: float = DEFAULT_SELF_DISCHARGE,
) -> BatteryLife:
    if capacity_mah <= 0:
        raise ValueError("capacity must be positive")
    if not 0 < depth_of_discharge <= 1:
        raise ValueError("depth of discharge must be in (0, 1]")
    if not 0 <= self_discharge_per_month < 1:
        raise ValueError("self-discharge must be in [0, 1)")
    floor = (1 - depth_of_discharge) * capacity_mah
    p = mah_per_day / 24.0
    plain = autonomy_hours(capacity_mah, floor, p, 0.0) / 24.0
    leaky = (
        autonomy_hours(capacity_mah, floor, p, self_discharge_per_hour(self_discharge_per_month))
        / 24.0
    )
    return BatteryLife(
        capacity_mah, depth_of_discharge, self_discharge_per_month, mah_per_day, plain, leaky
    )
