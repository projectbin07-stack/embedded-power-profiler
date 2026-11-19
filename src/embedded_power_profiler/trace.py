"""Measured traces, gap detection, uniform resampling and quantisation estimate.

A :class:`Trace` is what every loader returns: sample times in seconds (strictly increasing),
current in mA (positive = drawn from the supply) and optionally the voltage in V.

Resampling puts the samples on a uniform grid with period ``dt`` (by default the median sample
interval) by linear interpolation. Wherever two consecutive input samples are further apart than
``gap_factor * dt`` the grid points between them are marked invalid: nothing is interpolated
across a gap, and the time inside gaps is excluded from every duration and from the daily
scaling of charge and energy.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field

DEFAULT_GAP_FACTOR = 3.0


class TraceError(ValueError):
    """The input cannot be turned into a usable trace."""


@dataclass
class Trace:
    t: list[float]
    current_ma: list[float]
    voltage_v: list[float] | None = None
    source: str = ""
    name: str = ""
    notes: list[str] = field(default_factory=list)
    # Net charge the device's own coulomb counter reported over the log (ina219 sources).
    device_charge_mah: float | None = None

    def __post_init__(self) -> None:
        if len(self.t) != len(self.current_ma):
            raise TraceError("time and current columns have different lengths")
        if self.voltage_v is not None and len(self.voltage_v) != len(self.t):
            raise TraceError("time and voltage columns have different lengths")

    def __len__(self) -> int:
        return len(self.t)

    @property
    def span_s(self) -> float:
        return self.t[-1] - self.t[0] if len(self.t) > 1 else 0.0


def sort_and_dedupe(trace: Trace) -> Trace:
    """Sort by time and drop samples whose time repeats an earlier one (kept: the first)."""
    order = sorted(range(len(trace.t)), key=lambda k: trace.t[k])
    t: list[float] = []
    i: list[float] = []
    v: list[float] | None = [] if trace.voltage_v is not None else None
    dropped = 0
    for k in order:
        if t and trace.t[k] <= t[-1]:
            dropped += 1
            continue
        t.append(trace.t[k])
        i.append(trace.current_ma[k])
        if v is not None and trace.voltage_v is not None:
            v.append(trace.voltage_v[k])
    out = Trace(t, i, v, trace.source, trace.name, list(trace.notes), trace.device_charge_mah)
    if dropped:
        out.notes.append(f"{dropped} samples with a repeated timestamp were dropped")
    if any(b < a for a, b in zip(trace.t, trace.t[1:], strict=False)):
        out.notes.append("samples were not in time order and have been sorted")
    return out


def median(values: list[float]) -> float:
    if not values:
        raise ValueError("median of an empty list")
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else 0.5 * (s[mid - 1] + s[mid])


def median_interval(t: list[float]) -> float:
    if len(t) < 2:
        raise TraceError("need at least two samples")
    return median([b - a for a, b in itertools.pairwise(t)])


@dataclass(frozen=True)
class Gap:
    start: float  # time of the last sample before the gap
    end: float  # time of the first sample after it

    @property
    def length(self) -> float:
        return self.end - self.start


def find_gaps(t: list[float], dt: float, gap_factor: float = DEFAULT_GAP_FACTOR) -> list[Gap]:
    limit = gap_factor * dt
    return [Gap(a, b) for a, b in itertools.pairwise(t) if b - a > limit]


@dataclass
class Uniform:
    """A trace on a uniform grid ``t0 + k * dt``. ``nan`` marks grid points inside gaps."""

    t0: float
    dt: float
    current_ma: list[float]
    voltage_v: list[float] | None
    gaps: list[Gap]

    def __len__(self) -> int:
        return len(self.current_ma)

    def time(self, k: float) -> float:
        return self.t0 + k * self.dt

    def valid_runs(self) -> list[tuple[int, int]]:
        """Half-open index ranges ``[a, b)`` of consecutive valid grid points."""
        runs: list[tuple[int, int]] = []
        start: int | None = None
        for k, x in enumerate(self.current_ma):
            if math.isnan(x):
                if start is not None:
                    runs.append((start, k))
                    start = None
            elif start is None:
                start = k
        if start is not None:
            runs.append((start, len(self.current_ma)))
        return runs

    @property
    def covered_s(self) -> float:
        return sum(b - a for a, b in self.valid_runs()) * self.dt


def resample(
    trace: Trace, dt: float | None = None, gap_factor: float = DEFAULT_GAP_FACTOR
) -> Uniform:
    """Linear interpolation onto a uniform grid, leaving gaps as ``nan``."""
    if len(trace) < 2:
        raise TraceError("need at least two samples")
    native = median_interval(trace.t)
    if native <= 0:
        raise TraceError("sample times do not increase")
    step = native if dt is None else dt
    if step <= 0:
        raise TraceError("resampling period must be positive")
    gaps = find_gaps(trace.t, native, gap_factor)
    t, x = trace.t, trace.current_ma
    v = trace.voltage_v
    n = math.floor((t[-1] - t[0]) / step + 1e-9) + 1
    out_i: list[float] = [math.nan] * n
    out_v: list[float] | None = [math.nan] * n if v is not None else None
    limit = gap_factor * native
    j = 0
    for k in range(n):
        tk = t[0] + k * step
        while j < len(t) - 2 and t[j + 1] < tk:
            j += 1
        ta, tb = t[j], t[j + 1]
        if tb - ta > limit and ta < tk < tb:
            continue  # inside a gap
        if tk <= ta:
            f = 0.0
        elif tk >= tb:
            f = 1.0
        else:
            f = (tk - ta) / (tb - ta)
        out_i[k] = x[j] + f * (x[j + 1] - x[j])
        if out_v is not None and v is not None:
            out_v[k] = v[j] + f * (v[j + 1] - v[j])
    return Uniform(t[0], step, out_i, out_v, gaps)


def estimate_lsb(values: list[float], max_values: int = 50_000) -> float | None:
    """Quantisation step of the current readings, or ``None`` if they do not look quantised.

    The candidate is the smallest positive difference between distinct values; the data count as
    quantised when at least 99 % of the samples are integer multiples of it (to 1 % of a step).
    """
    sample = values[:max_values]
    distinct = sorted({round(x, 9) for x in sample if math.isfinite(x)})
    if len(distinct) < 3:
        return None
    step = min((b - a for a, b in itertools.pairwise(distinct) if b - a > 1e-9), default=None)
    if step is None:
        return None
    on_grid = sum(1 for x in sample if abs(x / step - round(x / step)) < 0.01)
    if on_grid < 0.99 * len(sample):
        return None
    return step
