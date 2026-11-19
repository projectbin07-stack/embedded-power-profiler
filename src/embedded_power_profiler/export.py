"""Exports: solar-node-sizer load profile / config, and a CSV of detected events.

solar-node-sizer (v0.1.1, ``src/core/config.js`` and ``src/core/load.js``) describes the load as
a list of states. One state has ``"base": true`` and fills whatever time the others leave; the
others are scheduled with ``every`` (seconds between events) and ``duration`` (seconds per
event); ``mA`` is the current at the supply. The exporter chooses

    mA       = measured charge of the state / time in the state
    every    = covered log time / number of events
    duration = time in the state / number of events

so that ``mA * duration / every`` equals the measured mean contribution of the state exactly:
solar-node-sizer's daily budget then reproduces the measured mAh/day (long outlier events are
included in the mean duration, not dropped). Hour-of-day windows (``from``/``to``) are not
inferred.

solar-node-sizer has no per-state voltage. When the current was measured at the regulated
supply (the default) the full config uses its ``ldo-lowiq`` regulator (3.3 V output) or a
custom LDO at the measured voltage; when it was measured at the battery terminals, the
regulator is ``none`` because its losses are already in the measured current.
"""

from __future__ import annotations

import copy
import csv
import math
from pathlib import Path
from typing import Any

from . import __version__
from .analysis import Result

SNS_VERSION = "0.1.1"

# solar-node-sizer v0.1.1 defaultConfig() (src/core/config.js), less the load, which is ours.
SNS_DEFAULTS: dict[str, Any] = {
    "location": {
        "lat": 12.97,
        "lon": 77.59,
        "tz": 5.5,
        "climate": "bengaluru",
        "seed": 1,
        "cloudScale": 1,
    },
    "panel": {
        "wp": 1,
        "tilt": 15,
        "azimuth": 180,
        "albedo": 0.2,
        "tempCoeff": -0.004,
        "noct": 48,
        "soiling": 0.05,
        "otherLosses": 0.1,
        "vmp": 6,
    },
    "charger": {"type": "linear", "efficiency": 0.97, "maxCurrentMa": 500},
    "battery": {
        "chemistry": "liion",
        "cells": 1,
        "capacityMah": 2500,
        "dod": 0.8,
        "cutoffV": 3.0,
        "selfDischargePerMonth": 0.03,
        "initialSoc": 1,
    },
    "regulatorPreset": "ldo-lowiq",
    "simulation": {"warmupYears": 1, "restartFraction": 0.05},
    "sizer": {
        "panelWp": [],
        "batteryMah": [],
        "maxDowntimeHours": 0,
        "costPerWp": 1,
        "costPerAh": 1,
    },
}
# The ldo-lowiq preset, used as the template of a custom LDO at another output voltage.
LDO_LOWIQ = {
    "type": "ldo",
    "vout": 3.3,
    "iqUa": 1.6,
    "dropout": 0.18,
    "minVin": 0,
    "nodeMinV": 3.0,
    "efficiency": [],
}
SNS_TOP_LEVEL = (
    "name",
    "note",
    "location",
    "panel",
    "charger",
    "battery",
    "regulatorPreset",
    "regulator",
    "load",
    "simulation",
    "sizer",
)


class ExportError(ValueError):
    pass


def _sig(x: float, digits: int = 6) -> float:
    return float(f"{x:.{digits}g}")


def load_states(result: Result) -> list[dict[str, Any]]:
    """The ``load`` array: base state first, then the scheduled states by current."""
    out: list[dict[str, Any]] = []
    base = result.base_state
    out.append({"name": base.name, "mA": _sig(base.level_ma), "base": True})
    for s in result.states:
        if s.base or s.events == 0:
            continue
        out.append(
            {
                "name": s.name,
                "mA": _sig(s.level_ma),
                "every": _sig(result.covered_s / s.events),
                "duration": _sig(s.time_s / s.events),
            }
        )
    return out


def _note(result: Result, measured_at: str) -> str:
    v = (
        f"{result.mean_voltage_v:.3g} V ({result.voltage_source})"
        if result.mean_voltage_v is not None
        else "not recorded"
    )
    return (
        f"Load profile measured by embedded-power-profiler {__version__} from "
        f"{result.trace.name} ({result.trace.source}, {result.covered_s:.0f} s analysed, "
        f"{result.mah_per_day:.4g} mAh/day). Current measured at the {measured_at}; "
        f"mean voltage {v}. 'every' and 'duration' are means, so long outlier events are "
        "included."
    )


def solar_node_sizer_profile(
    result: Result, name: str | None = None, measured_at: str = "supply"
) -> dict[str, Any]:
    """A partial solar-node-sizer config: name, note and load (it fills in the rest)."""
    if measured_at not in ("supply", "battery"):
        raise ExportError("measured_at must be 'supply' or 'battery'")
    cfg: dict[str, Any] = {
        "name": name or f"{result.trace.name} (measured)",
        "note": _note(result, measured_at),
    }
    if measured_at == "battery":
        cfg["regulatorPreset"] = "none"
    cfg["load"] = load_states(result)
    validate_sns_config(cfg)
    return cfg


def solar_node_sizer_config(
    result: Result, name: str | None = None, measured_at: str = "supply"
) -> dict[str, Any]:
    """A full solar-node-sizer config: its defaults with the measured load and battery."""
    profile = solar_node_sizer_profile(result, name, measured_at)
    cfg: dict[str, Any] = {"name": profile["name"], "note": profile["note"]}
    cfg.update(copy.deepcopy(SNS_DEFAULTS))
    opts = result.options
    if opts.capacity_mah is not None:
        cfg["battery"]["capacityMah"] = _sig(opts.capacity_mah)
    cfg["battery"]["dod"] = opts.depth_of_discharge
    cfg["battery"]["selfDischargePerMonth"] = opts.self_discharge_per_month
    if measured_at == "battery":
        cfg["regulatorPreset"] = "none"
    elif result.mean_voltage_v is not None and abs(result.mean_voltage_v - 3.3) > 0.05:
        cfg["regulatorPreset"] = "custom"
        vout = round(result.mean_voltage_v, 2)
        cfg["regulator"] = {**LDO_LOWIQ, "vout": vout, "nodeMinV": min(3.0, vout)}
    cfg["load"] = profile["load"]
    validate_sns_config(cfg)
    return cfg


def validate_sns_config(cfg: Any) -> None:
    """Re-implementation of the structural checks solar-node-sizer v0.1.1 applies on import.

    Mirrors ``normalizeConfig`` (unknown top-level fields are rejected) and ``normalizeLoad``
    (non-empty array; name string; ``mA >= 0``; optional ``every``/``duration``/``from``/``to``
    finite and ``>= 0``; a non-base state needs ``every > 0`` and ``duration``; hours <= 24; at
    most one base state). The CI job additionally runs solar-node-sizer itself.
    """
    if not isinstance(cfg, dict):
        raise ExportError("configuration must be a JSON object")
    for key in cfg:
        if key not in SNS_TOP_LEVEL:
            raise ExportError(f"unknown field {key}")
    load = cfg.get("load")
    if not isinstance(load, list) or not load:
        raise ExportError("load must be a non-empty array of states")
    bases = 0
    for i, s in enumerate(load):
        if not isinstance(s, dict):
            raise ExportError(f"load[{i}] must be an object")
        if not isinstance(s.get("name"), str):
            raise ExportError(f"load[{i}].name must be a string")
        ma = s.get("mA")
        if isinstance(ma, bool) or not isinstance(ma, (int, float)) or not ma >= 0:
            raise ExportError(f"load[{i}].mA must be a number >= 0")
        for k in ("every", "duration", "from", "to"):
            v = s.get(k)
            if v is None:
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                raise ExportError(f"load[{i}].{k} must be a number >= 0")
            if v < 0:
                raise ExportError(f"load[{i}].{k} must be a number >= 0")
        base = s.get("base") is True
        bases += base
        every = s.get("every")
        if not base and not (
            isinstance(every, (int, float)) and every > 0 and s.get("duration") is not None
        ):
            raise ExportError(f'load[{i}] needs "every" and "duration" (seconds), or "base": true')
        if s.get("from", 0) > 24 or s.get("to", 24) > 24:
            raise ExportError(f"load[{i}] hours must be 0..24")
    if bases > 1:
        raise ExportError("at most one load state can be the base state")


def write_events_csv(result: Result, path: str | Path) -> int:
    with Path(path).open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(
            [
                "index",
                "state",
                "start_s",
                "end_s",
                "duration_s",
                "mean_ma",
                "charge_uah",
                "energy_uwh",
                "clipped",
                "outlier",
            ]
        )
        for e in result.events:
            w.writerow(
                [
                    e.index,
                    e.state,
                    f"{e.start:.6f}",
                    f"{e.end:.6f}",
                    f"{e.duration:.6f}",
                    f"{e.mean_ma:.6g}",
                    f"{e.charge_mah * 1000:.6g}",
                    "" if e.energy_mwh is None else f"{e.energy_mwh * 1000:.6g}",
                    int(e.clipped),
                    e.outlier or "",
                ]
            )
    return len(result.events)
