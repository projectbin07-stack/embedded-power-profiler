from __future__ import annotations

import random

import pytest

from embedded_power_profiler.cluster import best_mixture, cluster_levels, fit_gmm


def _group(centre: float, n: int, spread: float, rng: random.Random) -> list[float]:
    return [centre + rng.gauss(0.0, spread) for _ in range(n)]


def test_three_separated_levels() -> None:
    rng = random.Random(1)
    xs = _group(-1.6, 30, 0.01, rng) + _group(1.26, 30, 0.01, rng) + _group(2.0, 30, 0.01, rng)
    labels, means = cluster_levels(xs)
    assert means == pytest.approx([-1.6, 1.26, 2.0], abs=0.01)
    assert labels == [0] * 30 + [1] * 30 + [2] * 30


def test_two_wakes_are_enough_to_separate_levels() -> None:
    # Only two segments per level (a log with two wakes), 95 and 190 mA (log10 1.98 and 2.28):
    # more than the 0.15-decade gap apart, so they are fitted as separate groups.
    xs = [1.977, 1.978, 2.278, 2.279, 1.976]
    labels, means = cluster_levels(xs)
    assert labels == [0, 0, 1, 1, 0]
    assert means == pytest.approx([1.977, 2.2785], abs=1e-3)


def test_close_levels_are_split_on_evidence() -> None:
    # 0.1 decades apart (26 %), tight groups of 40: BIC prefers two components.
    rng = random.Random(2)
    xs = _group(0.0, 40, 0.005, rng) + _group(0.1, 40, 0.005, rng)
    _labels, means = cluster_levels(xs)
    assert len(means) == 2
    assert means == pytest.approx([0.0, 0.1], abs=0.01)


def test_single_level() -> None:
    rng = random.Random(3)
    labels, means = cluster_levels(_group(1.0, 50, 0.01, rng))
    assert len(means) == 1 and set(labels) == {0}


def test_max_k_joins_the_closest_groups() -> None:
    xs = [0.0, 0.01, 1.0, 1.01, 1.3, 1.31, 3.0, 3.01]
    labels, means = cluster_levels(xs, max_k=3)
    assert len(means) == 3
    assert labels[2] == labels[4]  # 1.0 and 1.3 were the closest pair of groups


def test_fit_gmm_errors_and_order() -> None:
    with pytest.raises(ValueError):
        fit_gmm([1.0], 2)
    with pytest.raises(ValueError):
        best_mixture([])
    with pytest.raises(ValueError):
        cluster_levels([])
    m = fit_gmm([5.0, 5.01, 0.0, 0.01], 2)
    assert m.means == sorted(m.means)
    assert sum(m.weights) == pytest.approx(1.0)
