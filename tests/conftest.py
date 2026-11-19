from __future__ import annotations

from functools import cache
from pathlib import Path

import pytest

from embedded_power_profiler.analysis import Result, analyze
from embedded_power_profiler.synth import Truth, generate
from embedded_power_profiler.trace import Trace

FIXTURES = Path(__file__).parent / "fixtures"


@cache
def esp32() -> tuple[Trace, Truth, Result]:
    """30 minutes of the esp32-wifi profile at 50 Hz (30 wakes, 4 long reconnects)."""
    trace, truth = generate("esp32-wifi", seed=1)
    return trace, truth, analyze(trace)


@cache
def atmega() -> tuple[Trace, Truth, Result]:
    trace, truth = generate("atmega-nrf24", seed=1, duration_s=80.0)
    return trace, truth, analyze(trace)


@pytest.fixture
def fixtures() -> Path:
    return FIXTURES
