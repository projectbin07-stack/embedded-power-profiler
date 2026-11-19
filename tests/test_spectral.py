from __future__ import annotations

import cmath
import math
import random

import pytest

from embedded_power_profiler import spectral
from embedded_power_profiler.spectral import (
    autocorrelation,
    dominant_period,
    fft,
    parabolic_peak,
)


def _dft(x: list[complex], sign: float) -> list[complex]:
    n = len(x)
    return [
        sum(x[j] * cmath.exp(sign * 2j * math.pi * j * k / n) for j in range(n)) for k in range(n)
    ]


def test_fft_matches_naive_dft() -> None:
    rng = random.Random(1)
    x = [complex(rng.uniform(-1, 1), rng.uniform(-1, 1)) for _ in range(16)]
    assert fft(x) == pytest.approx(_dft(x, -1.0))
    assert fft(fft(x), inverse=True) == pytest.approx(x)
    with pytest.raises(ValueError):
        fft([1j] * 6)


def _pulses(n: int, period: int, width: int) -> list[float]:
    return [100.0 if k % period < width else 0.02 for k in range(n)]


@pytest.mark.parametrize("pure", [False, True])
def test_autocorrelation_finds_the_period(monkeypatch: pytest.MonkeyPatch, pure: bool) -> None:
    if pure:
        monkeypatch.setenv("EPP_NO_NUMPY", "1")
    acf = autocorrelation(_pulses(1000, 50, 5), 500)
    assert acf[0] == pytest.approx(1.0)
    found = dominant_period(acf, 2)
    assert found is not None
    assert found[0] == 50  # not 100 or 150, although those peaks are almost as high


def test_numpy_and_pure_paths_agree(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("numpy")
    rng = random.Random(5)
    x = [rng.random() for _ in range(300)]
    with_numpy = autocorrelation(x, 100)
    monkeypatch.setenv("EPP_NO_NUMPY", "1")
    assert spectral._numpy() is None
    assert autocorrelation(x, 100) == pytest.approx(with_numpy, abs=1e-12)


def test_autocorrelation_edge_cases() -> None:
    assert autocorrelation([]) == []
    assert autocorrelation([2.0, 2.0, 2.0]) == [1.0, 0.0, 0.0]
    assert dominant_period([1.0, 0.0, 0.1, 0.0], 1) is None  # peak below threshold


def test_parabolic_peak() -> None:
    assert parabolic_peak([0.0, 1.0, 0.0], 1) == 1.0
    # vertex of the parabola through (0, 0), (1, 1), (2, 0.5): 1 + 0.5 * (0 - 0.5) / -1.5
    assert parabolic_peak([0.0, 1.0, 0.5], 1) == pytest.approx(1 + 1 / 6)
    assert parabolic_peak([1.0, 1.0, 1.0], 1) == 1.0
    assert parabolic_peak([1.0, 0.5], 0) == 0.0
