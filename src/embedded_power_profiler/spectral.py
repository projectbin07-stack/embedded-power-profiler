"""Autocorrelation by FFT, used to find the device's wake-cycle period.

``numpy.fft`` is used when numpy is installed; otherwise an iterative radix-2 Cooley-Tukey FFT
in pure Python (O(n log n)) gives the same result. Set ``EPP_NO_NUMPY=1`` to force the pure
path. The autocorrelation is the biased estimate

    r(L) = sum_k x_k x_{k+L} / sum_k x_k^2        (x = signal minus its mean, zero in gaps)

computed as IFFT(|FFT(x)|^2) with zero padding to at least twice the length (no wrap-around).
"""

from __future__ import annotations

import cmath
import math
import os
from typing import Any


def _numpy() -> Any:
    if os.environ.get("EPP_NO_NUMPY"):
        return None
    try:
        import numpy
    except ImportError:  # pragma: no cover - numpy is installed in the dev environment
        return None
    return numpy


def fft(values: list[complex], inverse: bool = False) -> list[complex]:
    """Radix-2 FFT; ``len(values)`` must be a power of two. The inverse is scaled by 1/n."""
    n = len(values)
    if n == 0 or n & (n - 1):
        raise ValueError("length must be a power of two")
    a = list(values)
    j = 0
    for i in range(1, n):  # bit-reversal permutation
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j |= bit
        if i < j:
            a[i], a[j] = a[j], a[i]
    sign = 1.0 if inverse else -1.0
    size = 2
    while size <= n:
        w_step = cmath.exp(sign * 2j * math.pi / size)
        half = size // 2
        twiddles = [1 + 0j] * half
        for k in range(1, half):
            twiddles[k] = twiddles[k - 1] * w_step
        for start in range(0, n, size):
            for k in range(half):
                u = a[start + k]
                v = a[start + k + half] * twiddles[k]
                a[start + k] = u + v
                a[start + k + half] = u - v
        size *= 2
    if inverse:
        a = [x / n for x in a]
    return a


def autocorrelation(x: list[float], max_lag: int | None = None) -> list[float]:
    """Normalised autocorrelation ``r[0..max_lag]`` (``r[0] = 1``) of ``x`` (mean removed here)."""
    n = len(x)
    if n == 0:
        return []
    mean = sum(x) / n
    centred = [v - mean for v in x]
    max_lag = n - 1 if max_lag is None else min(max_lag, n - 1)
    size = 1
    while size < 2 * n:
        size *= 2
    np = _numpy()
    if np is not None:
        arr = np.zeros(size)
        arr[:n] = centred
        spec = np.fft.rfft(arr)
        acf = np.fft.irfft(spec * np.conj(spec), size)[: max_lag + 1]
        raw = [float(v) for v in acf]
    else:
        spec = fft([complex(v) for v in centred] + [0j] * (size - n))
        power = [complex(abs(z) ** 2) for z in spec]
        raw = [z.real for z in fft(power, inverse=True)[: max_lag + 1]]
    if raw[0] <= 0:
        return [1.0] + [0.0] * max_lag
    return [v / raw[0] for v in raw]


def dominant_period(
    acf: list[float], min_lag: int, threshold: float = 0.2
) -> tuple[int, float] | None:
    """Lag of the repetition period: the first local maximum reaching 80 % of the highest one.

    Taking the first strong peak rather than the highest avoids picking a multiple of the period
    (peaks at 2T, 3T... are about as high as the one at T). Returns ``(lag, r)`` or ``None``.
    """
    peaks = [
        (lag, acf[lag])
        for lag in range(max(1, min_lag), len(acf) - 1)
        if acf[lag] >= acf[lag - 1] and acf[lag] > acf[lag + 1] and acf[lag] >= threshold
    ]
    if not peaks:
        return None
    top = max(r for _, r in peaks)
    for lag, r in peaks:
        if r >= 0.8 * top:
            return lag, r
    return None  # pragma: no cover - the highest peak always qualifies


def parabolic_peak(acf: list[float], lag: int) -> float:
    """Sub-sample peak position from a parabola through the three points around ``lag``."""
    if lag <= 0 or lag >= len(acf) - 1:
        return float(lag)
    a, b, c = acf[lag - 1], acf[lag], acf[lag + 1]
    denom = a - 2 * b + c
    if denom == 0:
        return float(lag)
    return lag + 0.5 * (a - c) / denom
