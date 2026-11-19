"""Write a trace in each supported input format (for fixtures, demos and round-trip tests).

The ina219 writers produce what ina219-battery-monitor would have sent for these samples: the
device clock in ms, integer mV/uA/uW, a running charge and energy counter and a state of charge
from a nominal capacity. They are written from the format documentation, not by running the
firmware.
"""

from __future__ import annotations

import csv
import math
import random
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from .bm1 import Frame
from .loaders import LOGGER_HEADER
from .sslog import KIND_INFO, KIND_RX, SslogWriter
from .trace import Trace

DEFAULT_START = 1_790_000_000.0  # host clock of synthetic logs: 2026-09-21T14:13:20Z
WRITE_FORMATS = ("csv", "ina219", "logger-csv", "sslog", "sslog-csv")


def frames(
    trace: Trace, boot_ms: int = 1000, capacity_mah: float = 2000.0, voltage_v: float = 3.3
) -> Iterator[Frame]:
    """``$BM1`` frames for the samples of ``trace`` (device clock starts at ``boot_ms``)."""
    charge_uah = 0.0
    energy_uwh = 0.0
    prev_t: float | None = None
    for seq, (t, i) in enumerate(zip(trace.t, trace.current_ma, strict=False)):
        v = trace.voltage_v[seq] if trace.voltage_v is not None else voltage_v
        bus_mv = round(v * 1000)
        current_ua = round(i * 1000)
        power_uw = round(bus_mv * current_ua / 1000)
        if prev_t is not None:
            dt_h = (t - prev_t) / 3600.0
            charge_uah += current_ua * dt_h
            energy_uwh += power_uw * dt_h
        prev_t = t
        soc = max(0, min(1000, round(1000 * (1 - charge_uah / 1000 / capacity_mah))))
        yield Frame(
            seq=seq,
            t_ms=boot_ms + round(t * 1000),
            bus_mv=bus_mv,
            current_ua=current_ua,
            power_uw=power_uw,
            charge_uah=round(charge_uah),
            energy_uwh=round(energy_uwh),
            soc_permille=soc,
            flags=0,
        )


def write_csv(trace: Trace, path: str | Path) -> None:
    with Path(path).open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        if trace.voltage_v is None:
            w.writerow(["time_s", "current_ma"])
            for t, i in zip(trace.t, trace.current_ma, strict=False):
                w.writerow([f"{t:.6f}", f"{i:.6g}"])
        else:
            w.writerow(["time_s", "current_ma", "voltage_v"])
            for t, i, v in zip(trace.t, trace.current_ma, trace.voltage_v, strict=False):
                w.writerow([f"{t:.6f}", f"{i:.6g}", f"{v:.4g}"])


def write_ina219_log(trace: Trace, path: str | Path, corrupt_every: int = 0) -> None:
    """Raw serial output, CR LF terminated, with the firmware's banner and rate lines.

    ``corrupt_every`` > 0 flips one digit in every n-th frame so the CRC check must catch it.
    """
    with Path(path).open("w", newline="", encoding="ascii") as f:
        f.write("#ina219-battery-monitor 0.1.0\r\n")
        f.write("#OK RATE 20\r\n")
        for frame in frames(trace):
            line = frame.encode()
            if corrupt_every and frame.seq % corrupt_every == corrupt_every - 1:
                line = line.replace(",", ";", 1)  # still well-formed text, wrong CRC
            f.write(line + "\r\n")


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds")


def write_logger_csv(trace: Trace, path: str | Path, start: float = DEFAULT_START) -> None:
    """The CSV of ina219-battery-monitor's ``tools/logger.py record``."""
    with Path(path).open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(LOGGER_HEADER)
        for frame, t in zip(frames(trace), trace.t, strict=False):
            w.writerow(
                [
                    _iso(start + t),
                    str(frame.seq),
                    str(frame.t_ms),
                    f"{frame.bus_mv / 1000:.3f}",
                    f"{frame.current_ua / 1000:.3f}",
                    f"{frame.power_uw / 1000:.3f}",
                    f"{frame.charge_uah / 1000:.3f}",
                    f"{frame.energy_uwh / 1000:.3f}",
                    f"{frame.soc_permille / 10:.1f}",
                    str(frame.flags),
                ]
            )


def write_sslog(
    trace: Trace, path: str | Path, start: float = DEFAULT_START, seed: int = 0
) -> None:
    """A serial-scope raw log: the frame bytes split into irregular receive chunks."""
    rng = random.Random(seed)
    meta = {"source": "synthetic", "decoder": "ina219", "synthetic": True, "baud": 115200}
    with SslogWriter(path, start, meta) as w:
        w.write(KIND_INFO, start, b"connected")
        w.write(KIND_RX, start, b"#ina219-battery-monitor 0.1.0\r\n")
        pending = b""
        for frame, t in zip(frames(trace), trace.t, strict=False):
            pending += (frame.encode() + "\r\n").encode("ascii")
            # A USB-serial adapter delivers whatever arrived since its last poll.
            cut = rng.randint(1, len(pending))
            w.write(KIND_RX, start + t + 0.002, pending[:cut])
            pending = pending[cut:]
        if pending:
            w.write(KIND_RX, start + (trace.t[-1] if trace.t else 0.0) + 0.004, pending)


def _fmt(value: float) -> str:
    """serial-scope's ``format_value``."""
    if not math.isfinite(value):
        return ""
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return repr(value)


def write_sslog_csv(trace: Trace, path: str | Path, start: float = DEFAULT_START) -> None:
    """serial-scope's decoded CSV export of an ina219 session."""
    with Path(path).open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(
            [
                "host_time",
                "device_time",
                "source",
                "bus_v",
                "current_ma",
                "power_mw",
                "charge_mah",
                "energy_mwh",
                "soc_pct",
                "flags",
            ]
        )
        for frame, t in zip(frames(trace), trace.t, strict=False):
            w.writerow(
                [
                    f"{start + t + 0.002:.6f}",
                    f"{frame.t_ms / 1000.0:.6f}",
                    "BM1",
                    _fmt(frame.bus_mv * 1e-3),
                    _fmt(frame.current_ua * 1e-3),
                    _fmt(frame.power_uw * 1e-3),
                    _fmt(frame.charge_uah * 1e-3),
                    _fmt(frame.energy_uwh * 1e-3),
                    _fmt(frame.soc_permille * 0.1),
                    _fmt(float(frame.flags)),
                ]
            )


def write(trace: Trace, path: str | Path, fmt: str) -> None:
    if fmt == "csv":
        write_csv(trace, path)
    elif fmt == "ina219":
        write_ina219_log(trace, path)
    elif fmt == "logger-csv":
        write_logger_csv(trace, path)
    elif fmt == "sslog":
        write_sslog(trace, path)
    elif fmt == "sslog-csv":
        write_sslog_csv(trace, path)
    else:
        raise ValueError(f"unknown output format {fmt!r} (known: {', '.join(WRITE_FORMATS)})")
