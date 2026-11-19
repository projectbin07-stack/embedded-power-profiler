"""Change-point detection on the current trace with PELT.

The current of a sleeping-then-waking device spans four or more decades (microamps asleep,
hundreds of milliamps transmitting), so the detector works on ``y = log10(i + i_floor)``:
a step from 20 uA to 40 uA and one from 60 mA to 120 mA are the same size there.
``i_floor`` is the ADC step of the readings (or 1 uA), so zero readings stay finite.

The noise is not the same at every level in that domain (a 25 uA sleep current read with 10 uA
steps is very noisy in log10 terms, a 190 mA burst is not), so the default cost is the Gaussian
negative log-likelihood with a change in mean *and* variance (twice the log-likelihood, constant
terms dropped)::

    C(a, b) = n * ln(max(var(y[a:b]), v_floor))          n = b - a

``v_floor`` is the variance that quantisation alone causes at the segment's level,
``(q / (I * ln 10))^2`` with ``q = ADC step / sqrt(12)`` and ``I`` the segment's current,
and never less than ``SIGMA_MIN^2``; without it a run of identical readings would have zero
variance and an infinitely good fit. ``cost="mean"`` selects the classic change-in-mean cost
``(sum y^2 - (sum y)^2 / n) / sigma^2`` with a fixed noise level instead. Both are O(1) per
segment from prefix sums.

PELT (Killick, Fearnhead & Eckley 2012) minimises ``sum C(segments) + beta * changes`` exactly,
discarding a candidate start ``tau`` once ``F(tau) + C(tau, t) > F(t)``. The penalty is
``beta = 2 * penalty * ln(n)`` (default penalty 2, i.e. a conservative BIC).

In a long homogeneous stretch nothing is pruned, which makes plain PELT quadratic in the length
of a sleep period. The search is therefore restricted to candidate positions: every sample where
``y`` moves by more than ``jump`` local noise levels within one or two samples (an averaging ADC
spreads an edge over two samples), plus every ``grid``-th sample for slow drifts. The result is
the exact optimum among partitions whose boundaries lie on those positions.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass

DEFAULT_PENALTY = 2.0
DEFAULT_MIN_SAMPLES = 2
SIGMA_MIN = 0.003  # decades: 0.7 % relative
_LN10 = math.log(10.0)


@dataclass(frozen=True)
class Segment:
    start: int  # grid index, inclusive
    end: int  # grid index, exclusive
    level: float  # mean of y (log10 domain)

    @property
    def n(self) -> int:
        return self.end - self.start


def log_current(values: list[float], floor_ma: float) -> list[float]:
    return [math.log10(max(x, 0.0) + floor_ma) for x in values]


def _quantile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    pos = q * (len(sorted_values) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (pos - lo) * (sorted_values[hi] - sorted_values[lo])


def _sigma_from_diffs(diffs: list[float]) -> float:
    """Robust noise level from sorted absolute first differences.

    ``max(1.4826 * median / sqrt 2, q75 / (1.1503 * sqrt 2), SIGMA_MIN)``: the second term keeps
    it sensible when more than half of the differences are zero (readings on one ADC code).
    """
    if not diffs:
        return SIGMA_MIN
    s_mad = 1.4826 * _quantile(diffs, 0.5) / math.sqrt(2.0)
    s_q75 = _quantile(diffs, 0.75) / (1.1503 * math.sqrt(2.0))
    return max(s_mad, s_q75, SIGMA_MIN)


def noise_sigma(runs: list[list[float]]) -> float:
    """Global robust noise level of ``y`` over several runs."""
    return _sigma_from_diffs(sorted(abs(b - a) for y in runs for a, b in itertools.pairwise(y)))


def candidate_positions(
    y: list[float], jump: float = 3.0, grid: int = 256, block: int = 32
) -> list[int]:
    """Positions where a change point may be placed (see the module docstring).

    The local noise level is the smallest of the robust estimates of a block of ``block``
    samples and its two neighbours, so a quiet radio burst next to a noisy, quantised sleep
    current still gets its own (low) threshold. With white noise a few per cent of the samples
    become positions by chance, which only costs time.
    """
    n = len(y)
    if n < 2:
        return []
    diffs = [abs(b - a) for a, b in itertools.pairwise(y)]
    n_blocks = (len(diffs) + block - 1) // block
    sig = [_sigma_from_diffs(sorted(diffs[k * block : (k + 1) * block])) for k in range(n_blocks)]
    pos: set[int] = set(range(grid, n, grid))
    for k in range(1, n):
        b = (k - 1) // block
        local = min(sig[max(0, b - 1) : b + 2])  # the quietest neighbour: edges stay visible
        limit = jump * math.sqrt(2.0) * local  # a difference of two samples has sqrt(2) sigma
        if diffs[k - 1] > limit:
            pos.add(k)
        if k >= 2 and abs(y[k] - y[k - 2]) > limit:
            pos.add(k - 1)
            pos.add(k)
    return sorted(pos)


def pelt(
    y: list[float],
    beta: float,
    min_size: int = 2,
    positions: list[int] | None = None,
    cost: str = "meanvar",
    sigma: float = 1.0,
    quant_ma: float = 0.0,
    floor_ma: float = 0.0,
) -> list[int]:
    """Optimal change points of ``y`` (indices where a new segment starts, excluding 0).

    ``positions`` restricts where change points may go (all indices if ``None``: exact PELT).
    ``cost`` is ``"meanvar"`` (default; ``quant_ma`` = ADC step / sqrt 12 and ``floor_ma`` the
    offset used for ``y`` set the variance floor) or ``"mean"`` (fixed ``sigma``).
    """
    if cost not in ("mean", "meanvar"):
        raise ValueError(f"unknown cost {cost!r}")
    n = len(y)
    if n < 2 * min_size:
        return []
    s1 = [0.0] * (n + 1)
    s2 = [0.0] * (n + 1)
    for k, v in enumerate(y):
        s1[k + 1] = s1[k] + v
        s2[k + 1] = s2[k] + v * v
    inv = 1.0 / (sigma * sigma)
    vmin = SIGMA_MIN * SIGMA_MIN

    def seg_cost(a: int, b: int) -> float:
        m = b - a
        d = s1[b] - s1[a]
        ss = s2[b] - s2[a] - d * d / m
        if cost == "mean":
            return ss * inv
        var = ss / m
        floor = vmin
        if quant_ma > 0:
            level = 10.0 ** (d / m)  # = current + floor_ma
            floor = max(floor, (quant_ma / (max(level, floor_ma, 1e-12) * _LN10)) ** 2)
        return m * math.log(max(var, floor))

    allowed = sorted(
        {p for p in (positions if positions is not None else range(1, n)) if 0 < p < n}
    )
    f: dict[int, float] = {0: -beta}
    last: dict[int, int] = {0: -1}
    candidates = [0]
    waiting: list[int] = []  # computed positions not yet min_size behind t
    for t in [*allowed, n]:
        while waiting and waiting[0] <= t - min_size:
            candidates.append(waiting.pop(0))
        best = math.inf
        best_tau = -1
        values: list[float] = []
        for tau in candidates:
            v = f[tau] + seg_cost(tau, t) if t - tau >= min_size else math.inf
            values.append(v)
            if v + beta < best:
                best = v + beta
                best_tau = tau
        f[t] = best
        last[t] = best_tau
        candidates = [tau for tau, v in zip(candidates, values, strict=False) if v <= best]
        if t < n and best < math.inf:
            waiting.append(t)
    cps: list[int] = []
    t = n
    while True:
        tau = last.get(t, -1)
        if tau <= 0:
            break
        cps.append(tau)
        t = tau
    return sorted(cps)


def segments_from_changes(y: list[float], changes: list[int], offset: int = 0) -> list[Segment]:
    bounds = [0, *changes, len(y)]
    return [
        Segment(offset + a, offset + b, sum(y[a:b]) / (b - a))
        for a, b in itertools.pairwise(bounds)
    ]


def merge_short(
    segments: list[Segment], y: list[float], min_samples: int
) -> tuple[list[Segment], int, int]:
    """Fold segments shorter than ``min_samples`` into the neighbour with the closest level.

    ``y`` is the log-current indexed like the segments. Returns the new list, the number of
    segments folded and how many of those were excursions (above or below *both* neighbours,
    by more than 0.05 decades). Charge is preserved: samples only change segment. A short
    segment between its neighbours' levels is usually a sample that straddles a transition
    (the ADC averaged across the edge); an excursion is a burst shorter than the sample
    period, seen by one sample, or a glitch.
    """
    segs = list(segments)
    folded = 0
    excursions = 0
    while len(segs) > 1:
        short = [k for k, s in enumerate(segs) if s.n < min_samples]
        if not short:
            break
        # Fold the shortest first; ties left to right. One at a time keeps neighbours exact.
        k = min(short, key=lambda j: (segs[j].n, j))
        s = segs[k]
        left = segs[k - 1] if k > 0 else None
        right = segs[k + 1] if k + 1 < len(segs) else None
        if left is not None and right is not None:
            lo, hi = sorted((left.level, right.level))
            if s.level > hi + 0.05 or s.level < lo - 0.05:
                excursions += 1
        use_right = left is None or (
            right is not None and abs(right.level - s.level) < abs(left.level - s.level)
        )
        if use_right:
            assert right is not None
            segs[k : k + 2] = [_span(s.start, right.end, y)]
        else:
            assert left is not None
            segs[k - 1 : k + 1] = [_span(left.start, s.end, y)]
        folded += 1
    return segs, folded, excursions


def _span(a: int, b: int, y: list[float]) -> Segment:
    return Segment(a, b, sum(y[a:b]) / (b - a))
