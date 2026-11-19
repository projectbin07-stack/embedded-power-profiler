"""User state definitions (TOML or JSON) and the default naming of detected states.

A states file maps current bands to names and can tune the detector::

    [analysis]            # all optional
    penalty = 2.0         # change-point penalty multiplier (beta = 2 * penalty * ln n)
    min_samples = 2       # shorter segments are folded into a neighbour
    max_states = 8        # upper limit for the level clustering

    [[state]]
    name = "deep sleep"
    max_ma = 1.0          # band is [min_ma, max_ma), defaults 0 and infinity

    [[state]]
    name = "Wi-Fi"
    min_ma = 60

Every detected segment whose mean current falls in a band gets that band's name (first matching
band wins); segments in no band are clustered automatically. JSON files use the same keys
(``{"analysis": {...}, "state": [{...}, ...]}``; ``"states"`` is accepted too).
"""

from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib as _toml
else:  # pragma: no cover - exercised on Python 3.10 only
    import tomli as _toml


class StatesFileError(ValueError):
    pass


@dataclass(frozen=True)
class Band:
    name: str
    min_ma: float = 0.0
    max_ma: float = math.inf

    def contains(self, ma: float) -> bool:
        return self.min_ma <= ma < self.max_ma


@dataclass
class StateSpec:
    bands: list[Band] = field(default_factory=list)
    penalty: float | None = None
    min_samples: int | None = None
    max_states: int | None = None

    def match(self, ma: float) -> str | None:
        for band in self.bands:
            if band.contains(ma):
                return band.name
        return None


# Heuristic names for automatically found states, by mean current. They describe a current
# range, not what the firmware is doing; give real names with a states file.
DEFAULT_BANDS = (
    Band("sleep", 0.0, 1.0),
    Band("low-power", 1.0, 10.0),
    Band("active", 10.0, 80.0),
    Band("radio", 80.0, math.inf),
)


def _number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StatesFileError(f"{where} must be a number")
    return float(value)


def parse_spec(data: Any) -> StateSpec:
    if not isinstance(data, dict):
        raise StatesFileError("states file must contain a table/object")
    unknown = set(data) - {"analysis", "state", "states"}
    if unknown:
        raise StatesFileError(f"unknown key(s): {', '.join(sorted(unknown))}")
    spec = StateSpec()
    analysis = data.get("analysis", {})
    if not isinstance(analysis, dict):
        raise StatesFileError("[analysis] must be a table")
    for key, value in analysis.items():
        if key == "penalty":
            spec.penalty = _number(value, "analysis.penalty")
            if spec.penalty <= 0:
                raise StatesFileError("analysis.penalty must be positive")
        elif key in ("min_samples", "max_states"):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise StatesFileError(f"analysis.{key} must be a positive integer")
            setattr(spec, key, value)
        else:
            raise StatesFileError(f"unknown key analysis.{key}")
    raw_states = data.get("state", data.get("states", []))
    if not isinstance(raw_states, list):
        raise StatesFileError("state must be an array of tables")
    for k, item in enumerate(raw_states):
        where = f"state[{k}]"
        if not isinstance(item, dict):
            raise StatesFileError(f"{where} must be a table")
        extra = set(item) - {"name", "min_ma", "max_ma"}
        if extra:
            raise StatesFileError(f"{where}: unknown key(s) {', '.join(sorted(extra))}")
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            raise StatesFileError(f"{where}.name must be a non-empty string")
        lo = _number(item.get("min_ma", 0.0), f"{where}.min_ma")
        hi = _number(item.get("max_ma", math.inf), f"{where}.max_ma")
        if not lo < hi:
            raise StatesFileError(f"{where}: min_ma must be below max_ma")
        spec.bands.append(Band(name.strip(), lo, hi))
    return spec


def load_spec(path: str | Path) -> StateSpec:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    try:
        data = json.loads(text) if p.suffix.lower() == ".json" else _toml.loads(text)
    except (ValueError, _toml.TOMLDecodeError) as exc:
        raise StatesFileError(f"{p.name}: {exc}") from exc
    return parse_spec(data)


def default_names(levels_ma: list[float]) -> list[str]:
    """Names for clusters (given by mean current) from :data:`DEFAULT_BANDS`.

    When several clusters share a band, each gets its level appended, e.g. ``active 18 mA``.
    """
    bands = []
    for ma in levels_ma:
        band = next((b.name for b in DEFAULT_BANDS if b.contains(ma)), "radio")
        bands.append(band)
    names = []
    for ma, band in zip(levels_ma, bands, strict=False):
        if bands.count(band) > 1:
            names.append(f"{band} {format_ma(ma)}")
        else:
            names.append(band)
    return names


def format_ma(ma: float) -> str:
    if ma >= 1.0:
        return f"{ma:.3g} mA"
    return f"{ma * 1000:.3g} uA"
