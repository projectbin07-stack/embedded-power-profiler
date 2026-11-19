from __future__ import annotations

import math
import random

import pytest

from embedded_power_profiler.segment import (
    SIGMA_MIN,
    Segment,
    candidate_positions,
    log_current,
    merge_short,
    noise_sigma,
    pelt,
    segments_from_changes,
)


def _steps(levels: list[tuple[float, int]], noise: float, seed: int = 1) -> list[float]:
    rng = random.Random(seed)
    return [lv + rng.gauss(0.0, noise) for lv, n in levels for _ in range(n)]


def test_mean_cost_finds_exact_steps_without_noise() -> None:
    # A 5-sigma step held for 10 samples: splitting gains 10 * 20 * 25 / 30 > beta = 10 at each
    # edge, so both edges are change points; nothing else is.
    y = [0.0] * 20 + [5.0] * 10 + [0.0] * 20
    assert pelt(y, beta=10.0, min_size=1, cost="mean", sigma=1.0) == [20, 30]


def test_mean_cost_respects_the_penalty() -> None:
    # One sample 2 sigma high: the best split gains about 2^2 = 4 < beta, so no change point.
    y = [0.0] * 20 + [2.0] + [0.0] * 20
    assert pelt(y, beta=10.0, min_size=1, cost="mean", sigma=1.0) == []


def test_meanvar_cost_finds_level_and_variance_changes() -> None:
    y = _steps([(0.0, 60), (1.0, 30), (-1.0, 40)], 0.05)
    y += [-1.0 + random.Random(2).gauss(0.0, 0.5) for _ in range(60)]  # same mean, noisier
    cps = pelt(y, beta=4 * math.log(len(y)), min_size=2)
    assert cps[:2] == [60, 90]
    assert cps[2] == pytest.approx(130, abs=3)


def test_restricted_search_matches_full_search() -> None:
    y = _steps([(-1.6, 300), (1.6, 15), (1.25, 25), (2.0, 100), (-1.6, 300)], 0.02, seed=3)
    beta = 4 * math.log(len(y))
    full = pelt(y, beta, min_size=1)
    fast = pelt(y, beta, min_size=1, positions=candidate_positions(y))
    assert full == fast == [300, 315, 340, 440]


def test_restricted_search_uses_only_allowed_positions() -> None:
    y = [0.0] * 20 + [5.0] * 10 + [0.0] * 20
    assert pelt(y, 10.0, 1, positions=[18, 30], cost="mean") == [18, 30]


def test_pelt_edge_cases() -> None:
    assert pelt([1.0, 2.0, 3.0], 1.0, min_size=2) == []
    with pytest.raises(ValueError):
        pelt([1.0] * 10, 1.0, cost="l1")


def test_quantisation_floor_stops_splitting_identical_readings() -> None:
    # 25 uA on a 10 uA ADC: readings 20/30 uA. A stretch of identical readings would have zero
    # variance; the floor (q / (I ln 10))^2 keeps it from becoming its own segment.
    rng = random.Random(4)
    ma = [round((0.025 + rng.gauss(0, 0.004)) / 0.01) * 0.01 for _ in range(400)]
    ma[100:130] = [0.02] * 30
    y = log_current(ma, 0.01)
    beta = 4 * math.log(len(y))
    assert pelt(y, beta, 1, quant_ma=0.01 / math.sqrt(12), floor_ma=0.01) == []
    assert pelt(y, beta, 1) != []  # without the floor the flat stretch is split off


def test_noise_sigma() -> None:
    assert noise_sigma([]) == SIGMA_MIN
    assert noise_sigma([[1.0, 1.0, 1.0]]) == SIGMA_MIN
    y = _steps([(0.0, 20000)], 0.1, seed=5)
    assert noise_sigma([y]) == pytest.approx(0.1, rel=0.05)


def test_candidate_positions_find_two_sample_edges() -> None:
    # An edge smeared over two samples (0 -> 0.5 -> 1) is found by the lag-2 test.
    y = [0.0] * 40 + [0.5] + [1.0] * 40
    for k in range(len(y)):
        y[k] += 0.001 * ((k * 7919) % 13 - 6)  # small deterministic noise
    pos = candidate_positions(y, grid=1000)
    assert 40 in pos and 41 in pos
    assert len(pos) < 6


def test_segments_and_merge_short() -> None:
    y = [0.0] * 10 + [0.6] + [1.0] * 9 + [3.0] + [1.0] * 5
    segs = segments_from_changes(y, [10, 11, 20, 21])
    assert [(s.start, s.end) for s in segs] == [(0, 10), (10, 11), (11, 20), (20, 21), (21, 26)]
    merged, folded, excursions = merge_short(segs, y, 2)
    # 0.6 is closer to 1.0 than to 0.0: it joins the right (level now 0.96). The 3.0 spike is
    # above both neighbours (an excursion) and joins the closer one, the 1.0 run on its right.
    assert [(s.start, s.end) for s in merged] == [(0, 10), (10, 20), (20, 26)]
    assert folded == 2 and excursions == 1
    assert merged[1].level == pytest.approx((0.6 + 9 * 1.0) / 10)
    assert merged[2].level == pytest.approx((3.0 + 5 * 1.0) / 6)
    assert Segment(0, 5, 0.0).n == 5
