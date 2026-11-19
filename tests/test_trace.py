from __future__ import annotations

import math

import pytest

from embedded_power_profiler.trace import (
    Trace,
    TraceError,
    estimate_lsb,
    find_gaps,
    median,
    median_interval,
    resample,
    sort_and_dedupe,
)


def test_median() -> None:
    assert median([3.0, 1.0, 2.0]) == 2.0
    assert median([4.0, 1.0, 2.0, 3.0]) == 2.5
    with pytest.raises(ValueError):
        median([])


def test_trace_length_checks() -> None:
    with pytest.raises(TraceError):
        Trace([0.0, 1.0], [1.0])
    with pytest.raises(TraceError):
        Trace([0.0, 1.0], [1.0, 2.0], [3.3])
    with pytest.raises(TraceError):
        median_interval([0.0])


def test_resample_interpolates_linearly() -> None:
    # Samples at 0, 1, 2 s; a grid of 0.5 s puts points half-way: (0+10)/2 = 5, (10+20)/2 = 15.
    tr = Trace([0.0, 1.0, 2.0], [0.0, 10.0, 20.0], [3.0, 3.2, 3.4])
    u = resample(tr, 0.5)
    assert u.current_ma == pytest.approx([0.0, 5.0, 10.0, 15.0, 20.0])
    assert u.voltage_v == pytest.approx([3.0, 3.1, 3.2, 3.3, 3.4])
    assert u.covered_s == pytest.approx(2.5)  # 5 grid points x 0.5 s


def test_resample_leaves_gaps_empty() -> None:
    # Median interval 1 s; the 10 s hole is more than 3 periods, so 3..12 s is a gap.
    t = [0.0, 1.0, 2.0, 3.0, 13.0, 14.0, 15.0]
    tr = Trace(t, [1.0] * 7)
    gaps = find_gaps(t, 1.0)
    assert [(g.start, g.end, g.length) for g in gaps] == [(3.0, 13.0, 10.0)]
    u = resample(tr)
    assert u.dt == 1.0 and len(u) == 16
    nan = [k for k, x in enumerate(u.current_ma) if math.isnan(x)]
    assert nan == list(range(4, 13))
    assert u.valid_runs() == [(0, 4), (13, 16)]
    assert u.covered_s == 7.0


def test_resample_rejects_bad_input() -> None:
    with pytest.raises(TraceError):
        resample(Trace([0.0], [1.0]))
    with pytest.raises(TraceError):
        resample(Trace([0.0, 1.0], [1.0, 1.0]), -1.0)


def test_estimate_lsb() -> None:
    assert estimate_lsb([0.01 * k for k in (0, 3, 2, 5, 7, 2, 9)]) == pytest.approx(0.01)
    assert estimate_lsb([0.1234, 0.5678, 1.234567, 2.0001]) is None
    assert estimate_lsb([1.0, 1.0]) is None


def test_sort_and_dedupe_notes() -> None:
    tr = Trace([0.0, 2.0, 1.0, 2.0], [1.0, 3.0, 2.0, 9.0], [1.0, 1.0, 1.0, 1.0])
    out = sort_and_dedupe(tr)
    assert out.t == [0.0, 1.0, 2.0]
    assert out.current_ma == [1.0, 2.0, 3.0]
    assert any("repeated" in n for n in out.notes)
    assert any("sorted" in n for n in out.notes)
    assert out.span_s == 2.0
