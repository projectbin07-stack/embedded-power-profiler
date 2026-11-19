"""Seeded synthetic current traces with a known duty cycle, for tests and demos.

A profile is a periodic wake sequence on top of a sleep current. The generator builds the true
piecewise-constant current, then imitates a measurement:

1. each sample is the *average* of the true current over the ADC conversion window that ends
   at the sample time (the INA219 in ``Avg16`` mode averages over about 8.5 ms per channel),
   so samples that straddle an edge take intermediate values;
2. Gaussian noise ``sigma = noise_rel * |i| + noise_abs`` is added;
3. the result is rounded to the ADC step (``lsb_ma``);
4. the voltage follows ``v0 - r_source * i`` with noise, rounded to 4 mV (the INA219 bus LSB).

Occasionally (``outlier_probability`` per cycle) one state lasts ``outlier_factor`` times
longer, standing in for a Wi-Fi reconnect. The true timeline is returned with the samples so
tests can compare the analysis with the ground truth. The numbers in the profiles are
illustrative orders of magnitude, not measurements of a particular board.
"""

from __future__ import annotations

import bisect
import math
import random
from dataclasses import dataclass, field, replace

from .trace import Trace


@dataclass(frozen=True)
class StateDef:
    name: str
    ma: float
    duration_s: float


@dataclass(frozen=True)
class Profile:
    name: str
    description: str
    sleep: StateDef  # duration ignored: sleep fills the rest of each cycle
    wake: tuple[StateDef, ...]
    period_s: float
    rate_hz: float
    duration_s: float
    lsb_ma: float
    conversion_s: float
    noise_rel: float
    noise_abs_ma: float
    v0: float = 3.3
    r_source_ohm: float = 0.2
    period_jitter_s: float = 0.02
    outlier_state: str | None = None
    outlier_probability: float = 0.0
    outlier_factor: float = 1.0


PROFILES: dict[str, Profile] = {
    "esp32-wifi": Profile(
        name="esp32-wifi",
        description=(
            "ESP32-class node: deep sleep, boot, sensor read, Wi-Fi connect, MQTT publish, "
            "every 60 s; logged like ina219-battery-monitor at RATE 20 (50 Hz) with a 1 ohm "
            "shunt (10 uA steps)"
        ),
        sleep=StateDef("deep sleep", 0.025, 0.0),
        wake=(
            StateDef("boot", 42.0, 0.30),
            StateDef("sensor read", 18.0, 0.50),
            StateDef("Wi-Fi connect", 95.0, 2.0),
            StateDef("MQTT publish", 190.0, 0.30),
        ),
        period_s=60.0,
        rate_hz=50.0,
        duration_s=1800.0,
        lsb_ma=0.01,
        conversion_s=0.0085,
        noise_rel=0.01,
        noise_abs_ma=0.004,
        outlier_state="Wi-Fi connect",
        outlier_probability=0.1,
        outlier_factor=4.0,
    ),
    "atmega-nrf24": Profile(
        name="atmega-nrf24",
        description=(
            "ATmega328P + nRF24L01+ node: power-down sleep, wake and measure, radio TX, every "
            "8 s; logged at 1 kHz by a bench instrument with 0.1 uA steps"
        ),
        sleep=StateDef("power-down", 0.0045, 0.0),
        wake=(
            StateDef("measure", 1.6, 0.040),
            StateDef("radio TX", 13.5, 0.006),
        ),
        period_s=8.0,
        rate_hz=1000.0,
        duration_s=120.0,
        lsb_ma=0.0001,
        conversion_s=0.0005,
        noise_rel=0.01,
        noise_abs_ma=0.0002,
        v0=3.0,
        r_source_ohm=1.0,
        period_jitter_s=0.01,
    ),
}


@dataclass(frozen=True)
class Interval:
    state: str
    start: float
    end: float
    ma: float


@dataclass
class Truth:
    profile: Profile
    intervals: list[Interval]
    duration_s: float
    outlier_starts: list[float] = field(default_factory=list)

    def state_names(self) -> list[str]:
        return [self.profile.sleep.name] + [s.name for s in self.profile.wake]

    def time_s(self, state: str) -> float:
        return sum(iv.end - iv.start for iv in self.intervals if iv.state == state)

    def charge_mah(self, state: str | None = None) -> float:
        return sum(
            (iv.end - iv.start) * iv.ma / 3600.0
            for iv in self.intervals
            if state is None or iv.state == state
        )

    def events(self, state: str) -> int:
        return sum(1 for iv in self.intervals if iv.state == state)

    def mah_per_day(self, state: str | None = None) -> float:
        return self.charge_mah(state) * 86_400.0 / self.duration_s

    def current_at(self, t: float) -> float:
        starts = [iv.start for iv in self.intervals]
        k = max(0, bisect.bisect_right(starts, t) - 1)
        return self.intervals[k].ma


def timeline(profile: Profile, duration_s: float, rng: random.Random) -> Truth:
    """True piecewise-constant current: ``[0, duration_s)`` covered by intervals."""
    out: list[Interval] = []
    outliers: list[float] = []
    sleep = profile.sleep
    t = 0.0
    # First wake after a random phase so the log starts asleep.
    k = 0
    phase = profile.period_s * (0.2 + 0.3 * rng.random())
    while t < duration_s:
        cycle_start = (
            phase
            + k * profile.period_s
            + rng.uniform(-profile.period_jitter_s, profile.period_jitter_s)
        )
        k += 1
        if cycle_start > t:
            out.append(Interval(sleep.name, t, min(cycle_start, duration_s), sleep.ma))
            t = cycle_start
        if t >= duration_s:
            break
        long_one = rng.random() < profile.outlier_probability
        for state in profile.wake:
            length = state.duration_s
            if long_one and state.name == profile.outlier_state:
                length *= profile.outlier_factor
                outliers.append(t)
            end = min(t + length, duration_s)
            out.append(Interval(state.name, t, end, state.ma))
            t = end
            if t >= duration_s:
                break
    return Truth(profile, out, duration_s, outliers)


def _window_average(truth: Truth, starts: list[float], a: float, b: float, hint: int) -> float:
    """Mean of the true current over ``[a, b]``; ``hint`` is an interval index near ``a``."""
    ivs = truth.intervals
    k = hint
    while k > 0 and ivs[k].start > a:
        k -= 1
    while k + 1 < len(ivs) and ivs[k + 1].start <= a:
        k += 1
    if b <= a:
        return ivs[k].ma
    total = 0.0
    t = a
    while t < b and k < len(ivs):
        end = min(ivs[k].end, b)
        total += (end - t) * ivs[k].ma
        t = end
        k += 1
    if t < b:  # past the last interval
        total += (b - t) * ivs[-1].ma
    return total / (b - a)


def generate(
    profile: Profile | str,
    seed: int = 1,
    duration_s: float | None = None,
    rate_hz: float | None = None,
    gaps: list[tuple[float, float]] | None = None,
) -> tuple[Trace, Truth]:
    """Synthetic measured trace and its ground truth."""
    p = PROFILES[profile] if isinstance(profile, str) else profile
    if rate_hz is not None:
        p = replace(p, rate_hz=rate_hz)
    total = p.duration_s if duration_s is None else duration_s
    if total <= 0 or p.rate_hz <= 0:
        raise ValueError("duration and rate must be positive")
    rng = random.Random(seed)
    truth = timeline(p, total, rng)
    starts = [iv.start for iv in truth.intervals]
    n = int(total * p.rate_hz)
    t_out: list[float] = []
    i_out: list[float] = []
    v_out: list[float] = []
    hint = 0
    for k in range(n):
        t = k / p.rate_hz
        if gaps and any(a <= t < b for a, b in gaps):
            continue
        a = max(0.0, t - p.conversion_s)
        while hint + 1 < len(starts) and starts[hint + 1] <= a:
            hint += 1
        true_i = _window_average(truth, starts, a, t, hint)
        noisy = true_i + rng.gauss(0.0, p.noise_rel * abs(true_i) + p.noise_abs_ma)
        measured = round(noisy / p.lsb_ma) * p.lsb_ma
        volt = p.v0 - p.r_source_ohm * true_i / 1000.0 + rng.gauss(0.0, 0.002)
        t_out.append(round(t, 6))
        i_out.append(round(measured, 6))
        v_out.append(round(round(volt / 0.004) * 0.004, 3))
    trace = Trace(t_out, i_out, v_out, "synthetic", f"synthetic:{p.name}")
    return trace, truth


def states_toml(profile: Profile | str) -> str:
    """A states file naming the profile's states, with band edges at geometric midpoints."""
    p = PROFILES[profile] if isinstance(profile, str) else profile
    levels = sorted([p.sleep, *p.wake], key=lambda s: s.ma)
    lines = [f"# States of the synthetic '{p.name}' profile (bands between geometric midpoints)."]
    for k, s in enumerate(levels):
        lo = math.sqrt(levels[k - 1].ma * s.ma) if k > 0 else None
        hi = math.sqrt(levels[k + 1].ma * s.ma) if k + 1 < len(levels) else None
        lines.append("")
        lines.append("[[state]]")
        lines.append(f'name = "{s.name}"')
        if lo is not None:
            lines.append(f"min_ma = {lo:.4g}")
        if hi is not None:
            lines.append(f"max_ma = {hi:.4g}")
    return "\n".join(lines) + "\n"
