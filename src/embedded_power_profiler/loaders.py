"""Load current/voltage logs from the supported formats into a :class:`Trace`.

Formats (``--format``; ``auto`` detects them from the content):

``ina219``     raw ina219-battery-monitor serial output: ``$BM1,...*HH`` frames and ``#`` lines.
``logger-csv`` the CSV written by ina219-battery-monitor's ``tools/logger.py``.
``sslog``      a serial-scope ``.sslog`` raw log; the ``$BM1`` frames inside it are decoded.
``sslog-csv``  serial-scope's decoded CSV export (``host_time,device_time,source,...``) of an
               ina219 session.
``csv``        any CSV with a time column, a current column and optionally a voltage column.
               Units come from the header (``time_ms``, ``current (uA)``, ``V [mV]``...) or
               from :class:`CsvOptions`.

For the ina219 formats the device clock (``t_ms``) is used, unwrapped at 2^32 ms. A device
restart (sequence number or clock going backwards) starts a new run; when host timestamps exist
the run is placed by them, otherwise it is placed after an artificial gap and a note says so.
Samples flagged overflow or sensor-error carry no valid current and are dropped, and so are
samples flagged not-ready (no new conversion: repeated values, or zeros right after boot).
"""

from __future__ import annotations

import contextlib
import csv
import io
import itertools
import math
import re
from array import array
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .bm1 import FLAG_NOT_READY, INVALID_FLAGS, FrameError, decode_frame
from .sslog import SslogInfo, is_sslog, read_records, received_lines
from .trace import Trace, TraceError, median, sort_and_dedupe

FORMATS = ("auto", "ina219", "logger-csv", "sslog", "sslog-csv", "csv")

LOGGER_HEADER = [
    "host_time",
    "seq",
    "t_ms",
    "bus_v",
    "current_ma",
    "power_mw",
    "charge_mah",
    "energy_mwh",
    "soc_pct",
    "flags",
]
SSLOG_CSV_FIXED = ["host_time", "device_time", "source"]

WRAP_S = 2**32 / 1000.0  # the device's millisecond clock wraps after 49.7 days
RESTART_GAP_S = 60.0  # placeholder gap when a restart cannot be placed by host time

TIME_UNITS = {"s": 1.0, "sec": 1.0, "ms": 1e-3, "us": 1e-6, "ns": 1e-9, "min": 60.0, "h": 3600.0}
CURRENT_UNITS = {"a": 1000.0, "ma": 1.0, "ua": 1e-3, "na": 1e-6}
VOLTAGE_UNITS = {"v": 1.0, "mv": 1e-3}
# Column names that are just a unit.
BARE_UNITS = {
    "a": ("current", "a"),
    "ma": ("current", "ma"),
    "ua": ("current", "ua"),
    "na": ("current", "na"),
    "mv": ("voltage", "mv"),
    "ms": ("time", "ms"),
    "us": ("time", "us"),
}


@dataclass
class CsvOptions:
    """Column names and units for the generic CSV loader (``None`` = from the header)."""

    time_col: str | None = None
    current_col: str | None = None
    voltage_col: str | None = None
    time_unit: str | None = None
    current_unit: str | None = None
    voltage_unit: str | None = None
    # Block-average inputs sampled faster than this period, s (None = AUTO_BLOCK_S, 0 = never).
    block_s: float | None = None


@dataclass
class _Sample:
    host_t: float | None
    seq: int | None
    t_s: float
    bus_v: float
    current_ma: float
    charge_mah: float | None
    flags: int


def _bm1_trace(
    samples: Iterable[_Sample], source: str, name: str, notes: list[str] | None = None
) -> Trace:
    t_out: list[float] = []
    i_out: list[float] = []
    v_out: list[float] = []
    notes = list(notes or [])
    offset = 0.0
    prev: _Sample | None = None
    prev_abs = 0.0
    invalid = stale = restarts = wraps = unplaced = 0
    charge_runs: list[list[float]] = []
    for s in samples:
        if s.flags & INVALID_FLAGS:
            invalid += 1
            continue
        if s.flags & FLAG_NOT_READY:
            # No new conversion since the previous read: the values are a repeat (or, right
            # after boot, zeros), not a measurement.
            stale += 1
            continue
        new_run = prev is None
        if prev is not None:
            restart = wrap = False
            if s.seq is not None and prev.seq is not None and s.seq <= prev.seq:
                restart = True
            elif s.t_s < prev.t_s:
                if s.seq is not None or (prev.t_s > WRAP_S - 3600.0 and s.t_s < 3600.0):
                    wrap = True
                else:
                    restart = True
            if wrap:
                offset += WRAP_S
                wraps += 1
            if restart:
                restarts += 1
                new_run = True
                if s.host_t is not None and prev.host_t is not None:
                    target = prev_abs + max(s.host_t - prev.host_t, 0.0)
                else:
                    target = prev_abs + RESTART_GAP_S
                    unplaced += 1
                offset = target - s.t_s
        abs_t = s.t_s + offset
        t_out.append(abs_t)
        i_out.append(s.current_ma)
        v_out.append(s.bus_v)
        if s.charge_mah is not None:
            if new_run or not charge_runs:
                charge_runs.append([s.charge_mah, s.charge_mah])
            else:
                charge_runs[-1][1] = s.charge_mah
        prev = s
        prev_abs = abs_t
    if not t_out:
        raise TraceError(f"{name}: no valid ina219 frames found")
    if invalid:
        notes.append(f"{invalid} frames flagged overflow/sensor-error were dropped")
    if stale:
        notes.append(f"{stale} frames flagged not-ready (no new conversion) were dropped")
    if wraps:
        notes.append(f"device clock wrapped {wraps} time(s); unwrapped")
    if restarts:
        how = (
            f"{unplaced} without host time placed after a {RESTART_GAP_S:g} s gap"
            if unplaced
            else "runs placed by host time"
        )
        notes.append(f"device restarted {restarts} time(s); {how}")
    device_charge = sum(b - a for a, b in charge_runs) if charge_runs else None
    trace = Trace(t_out, i_out, v_out, source, name, notes, device_charge)
    return sort_and_dedupe(trace)


def _frames_from_lines(
    lines: Iterable[tuple[float | None, str]], notes: list[str]
) -> list[_Sample]:
    out: list[_Sample] = []
    bad = 0
    for host_t, line in lines:
        text = line.strip()
        if not text:
            continue
        if text.startswith("#"):
            continue  # firmware status line
        try:
            f = decode_frame(text)
        except FrameError:
            bad += 1
            continue
        out.append(
            _Sample(
                host_t,
                f.seq,
                f.t_ms / 1000.0,
                f.bus_mv / 1000.0,
                f.current_ua / 1000.0,
                f.charge_uah / 1000.0,
                f.flags,
            )
        )
    if bad:
        notes.append(f"{bad} lines failed CRC or format checks and were skipped")
    return out


def load_ina219_log(path: str | Path) -> Trace:
    p = Path(path)
    notes: list[str] = []
    with p.open(encoding="ascii", errors="replace") as f:
        frames = _frames_from_lines(((None, line) for line in f), notes)
    return _bm1_trace(frames, "ina219", p.name, notes)


def load_sslog(path: str | Path) -> Trace:
    p = Path(path)
    info = SslogInfo(paths=[p])
    notes: list[str] = []
    lines = list(received_lines(read_records(p, info)))
    if info.truncated:
        notes.append("log is truncated; read up to the last complete record")
    if len(info.paths) > 1:
        notes.append(f"read {len(info.paths)} rotated parts")
    frames = _frames_from_lines(lines, notes)
    if not frames:
        raise TraceError(f"{p.name}: no ina219 '$BM1' frames in the received data")
    return _bm1_trace(frames, "sslog", p.name, notes)


def _iso_seconds(text: str) -> float | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig", errors="replace")


def _csv_rows(text: str) -> tuple[list[str], list[list[str]]]:
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if not lines:
        raise TraceError("file has no data")
    try:
        dialect: type[csv.Dialect] | csv.Dialect = csv.Sniffer().sniff(lines[0], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO("\n".join(lines)), dialect))
    header = [h.strip() for h in rows[0]]
    return header, rows[1:]


def _float(text: str) -> float:
    return float(text.strip()) if text.strip() else math.nan


def load_logger_csv(path: str | Path) -> Trace:
    p = Path(path)
    header, rows = _csv_rows(_read_text(p))
    if header != LOGGER_HEADER:
        raise TraceError(f"{p.name}: not an ina219-battery-monitor logger CSV")
    col = {name: k for k, name in enumerate(header)}
    samples: list[_Sample] = []
    bad = 0
    for row in rows:
        try:
            samples.append(
                _Sample(
                    _iso_seconds(row[col["host_time"]].strip()),
                    int(row[col["seq"]]),
                    int(row[col["t_ms"]]) / 1000.0,
                    float(row[col["bus_v"]]),
                    float(row[col["current_ma"]]),
                    float(row[col["charge_mah"]]),
                    int(row[col["flags"]]),
                )
            )
        except (ValueError, IndexError):
            bad += 1
    notes = [f"{bad} malformed rows skipped"] if bad else []
    return _bm1_trace(samples, "logger-csv", p.name, notes)


def load_sslog_csv(path: str | Path) -> Trace:
    p = Path(path)
    header, rows = _csv_rows(_read_text(p))
    if header[:3] != SSLOG_CSV_FIXED:
        raise TraceError(f"{p.name}: not a serial-scope CSV export")
    if "current_ma" not in header:
        raise TraceError(
            f"{p.name}: serial-scope CSV has no current_ma channel; "
            "use --format csv with --time-col/--current-col"
        )
    col = {name: k for k, name in enumerate(header)}
    samples: list[_Sample] = []
    bad = 0
    skipped = 0
    for row in rows:
        try:
            if "source" in col and row[col["source"]] not in ("BM1", ""):
                skipped += 1
                continue
            cur = _float(row[col["current_ma"]])
            if math.isnan(cur):
                skipped += 1
                continue
            host_t = _float(row[col["host_time"]])
            dev = _float(row[col["device_time"]])
            volt = _float(row[col["bus_v"]]) if "bus_v" in col else math.nan
            charge = _float(row[col["charge_mah"]]) if "charge_mah" in col else math.nan
            flags = int(_float(row[col["flags"]])) if "flags" in col else 0
        except (ValueError, IndexError):
            bad += 1
            continue
        samples.append(
            _Sample(
                host_t,
                None,
                host_t if math.isnan(dev) else dev,
                volt,
                cur,
                None if math.isnan(charge) else charge,
                flags,
            )
        )
    notes: list[str] = []
    if bad:
        notes.append(f"{bad} malformed rows skipped")
    if skipped:
        notes.append(f"{skipped} rows without a current value skipped")
    trace = _bm1_trace(samples, "sslog-csv", p.name, notes)
    if trace.voltage_v is not None and all(math.isnan(v) for v in trace.voltage_v):
        trace.voltage_v = None
    return trace


_UNIT_RE = re.compile(r"^(?P<base>.*?)\s*(?:[\[(](?P<br>[^\])]+)[\])]|_(?P<suf>[a-z]+))?$")


def split_unit(name: str) -> tuple[str, str | None]:
    """``"current (uA)"`` -> ``("current", "ua")``; ``"time_ms"`` -> ``("time", "ms")``."""
    text = name.strip().lower().replace("µ", "u").replace("μ", "u")
    m = _UNIT_RE.match(text)
    if m is None:  # pragma: no cover - the pattern matches every string
        return text, None
    unit = m.group("br") or m.group("suf")
    base = m.group("base").strip()
    known = set(TIME_UNITS) | set(CURRENT_UNITS) | set(VOLTAGE_UNITS)
    if unit is not None and unit.strip() not in known:
        return text, None  # e.g. "host_time": "_time" is not a unit
    return base, unit.strip() if unit else None


def _role(base: str) -> str | None:
    if base in ("t", "time", "timestamp", "elapsed", "seconds", "sec", "host_time") or (
        base.startswith("time")
    ):
        return "time"
    if "current" in base or base in ("i", "ibat", "iload", "i_load", "idd", "i_out"):
        return "current"
    if "volt" in base or base in ("v", "vbus", "bus", "vbat", "vdd", "u", "bus_v"):
        return "voltage"
    return None


def _pick(
    header: list[str], role: str, explicit: str | None
) -> tuple[int, str | None] | tuple[None, None]:
    if explicit is not None:
        if explicit not in header:
            raise TraceError(f"column {explicit!r} not found (columns: {', '.join(header)})")
        k = header.index(explicit)
        return k, split_unit(explicit)[1]
    for k, name in enumerate(header):
        base, unit = split_unit(name)
        if _role(base) == role:
            return k, unit
    for k, name in enumerate(header):  # a column named only by its unit: "mA", "uA", "mV"
        base, unit = split_unit(name)
        if unit is None and BARE_UNITS.get(base, ("", ""))[0] == role:
            return k, BARE_UNITS[base][1]
    return None, None


def _scale(unit: str | None, table: dict[str, float], what: str) -> float:
    if unit is None:
        raise TraceError(f"no {what} unit")
    key = unit.lower().replace("µ", "u")
    if key not in table:
        raise TraceError(f"unknown {what} unit {unit!r} (known: {', '.join(table)})")
    return table[key]


AUTO_BLOCK_S = 1e-3  # inputs sampled faster than this are block-averaged to it by default
MAX_BINS = 2_000_000  # beyond this many bins, adjacent bins are merged (memory and PELT time)
_PROBE_ROWS = 2000  # rows read before deciding whether to block-average


class _Binner:
    """Block averages of (t, i, v) over fixed time bins, in O(number of bins) memory.

    Every sample adds to bin ``floor((t - t0) / width)``. A bin's time is the mean time of its
    samples, so a partly filled bin at an edge or a gap is not shifted, and its current is the
    mean of its samples, so the charge of the samples is preserved.
    """

    def __init__(self, t0: float, width: float, voltage: bool) -> None:
        self.t0 = t0
        self.width = width
        self.voltage = voltage
        self.st: array[float] = array("d")
        self.si: array[float] = array("d")
        self.sv: array[float] = array("d")
        self.nv: array[int] = array("q")
        self.n: array[int] = array("q")
        self.early: dict[int, list[float]] = {}  # bins before t0 (rows out of time order)
        self.samples = 0

    def add(self, t: float, i: float, v: float) -> None:
        k = math.floor((t - self.t0) / self.width)
        self.samples += 1
        has_v = self.voltage and not math.isnan(v)
        if k < 0:
            acc = self.early.setdefault(k, [0.0, 0.0, 0.0, 0.0, 0.0])
            acc[0] += t
            acc[1] += i
            acc[2] += v if has_v else 0.0
            acc[3] += 1.0 if has_v else 0.0
            acc[4] += 1.0
            return
        if k >= len(self.n):
            grow = k + 1 - len(self.n)
            self.st.extend(itertools.repeat(0.0, grow))
            self.si.extend(itertools.repeat(0.0, grow))
            self.n.extend(itertools.repeat(0, grow))
            if self.voltage:
                self.sv.extend(itertools.repeat(0.0, grow))
                self.nv.extend(itertools.repeat(0, grow))
        self.st[k] += t
        self.si[k] += i
        self.n[k] += 1
        if has_v:
            self.sv[k] += v
            self.nv[k] += 1

    def merge(self, factor: int) -> None:
        """Merge every ``factor`` adjacent bins into one."""
        if factor <= 1:
            return

        def fold(a: array[Any]) -> array[Any]:
            out = array(a.typecode)
            for k in range(0, len(a), factor):
                out.append(sum(a[k : k + factor]))
            return out

        self.st, self.si, self.n = fold(self.st), fold(self.si), fold(self.n)
        if self.voltage:
            self.sv, self.nv = fold(self.sv), fold(self.nv)
        early: dict[int, list[float]] = {}
        for k, acc in self.early.items():
            dst = early.setdefault(k // factor, [0.0] * 5)
            for j in range(5):
                dst[j] += acc[j]
        self.early = early
        self.width *= factor

    def result(self) -> tuple[list[float], list[float], list[float]]:
        t: list[float] = []
        i: list[float] = []
        v: list[float] = []
        for k in sorted(self.early):
            acc = self.early[k]
            t.append(acc[0] / acc[4])
            i.append(acc[1] / acc[4])
            v.append(acc[2] / acc[3] if acc[3] else math.nan)
        for k, cnt in enumerate(self.n):
            if cnt == 0:
                continue
            t.append(self.st[k] / cnt)
            i.append(self.si[k] / cnt)
            if self.voltage:
                nv = self.nv[k]
                v.append(self.sv[k] / nv if nv else math.nan)
            else:
                v.append(math.nan)
        return t, i, v


def _fmt_period(seconds: float) -> str:
    return f"{seconds * 1000:.3g} ms" if seconds >= 1e-3 else f"{seconds * 1e6:.3g} us"


def load_generic_csv(path: str | Path, options: CsvOptions | None = None) -> Trace:
    """Generic CSV, read as a stream.

    A file sampled faster than ``options.block_s`` (default 1 ms, i.e. faster than 1 kHz) is
    block-averaged to that period while it is read, so memory and analysis time depend on the
    length of the log, not on its number of rows. Charge is preserved; events shorter than about
    two bins are not resolved as states of their own. ``block_s = 0`` keeps every sample.
    """
    p = Path(path)
    o = options or CsvOptions()
    with p.open(encoding="utf-8-sig", errors="replace", newline="") as f:
        lines = (ln for ln in f if ln.strip() and not ln.lstrip().startswith("#"))
        first = next(lines, None)
        if first is None:
            raise TraceError("file has no data")
        try:
            dialect: type[csv.Dialect] | csv.Dialect = csv.Sniffer().sniff(first, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        header = [h.strip() for h in next(csv.reader([first], dialect))]
        return _generic_rows(p, o, header, csv.reader(lines, dialect))


def _generic_rows(p: Path, o: CsvOptions, header: list[str], rows: Iterator[list[str]]) -> Trace:
    notes: list[str] = []
    kt, ut = _pick(header, "time", o.time_col)
    ki, ui = _pick(header, "current", o.current_col)
    kv, uv = _pick(header, "voltage", o.voltage_col)
    if kt is None:
        raise TraceError(f"{p.name}: no time column found; use --time-col")
    if ki is None:
        raise TraceError(f"{p.name}: no current column found; use --current-col")
    ut = o.time_unit or ut
    ui = o.current_unit or ui
    uv = o.voltage_unit or uv
    if ut is None:
        ut = "s"
        notes.append("time unit not in the header; assumed seconds (use --time-unit)")
    assumed_v = kv is not None and uv is None
    if assumed_v:
        uv = "v"
        notes.append("voltage unit not in the header; assumed V (use --voltage-unit)")
    st = _scale(ut, TIME_UNITS, "time")
    sv = _scale(uv, VOLTAGE_UNITS, "voltage") if kv is not None else 1.0
    if ui is None:
        # Assuming mA here made a log in amps read 1000x low with exit 0: refuse instead.
        raw: list[float] = []
        for row in itertools.islice(rows, _PROBE_ROWS):
            with contextlib.suppress(ValueError, IndexError):
                raw.append(float(row[ki]))
        seen = f" (first values {min(raw):g} to {max(raw):g})" if raw else ""
        raise TraceError(
            f"{p.name}: the current column {header[ki]!r} has no unit{seen}; pass "
            "--current-unit A, mA, uA or nA, or name the column like 'current_ma' or "
            "'current (A)'"
        )
    si = _scale(ui, CURRENT_UNITS, "current")

    bad = 0
    iso = False

    def parse(row: list[str]) -> tuple[float, float, float] | None:
        nonlocal bad, iso
        try:
            raw_t = row[kt].strip()
            try:
                tv = float(raw_t) * st
            except ValueError:
                parsed = _iso_seconds(raw_t)
                if parsed is None:
                    raise
                tv = parsed
                iso = True
            iv = float(row[ki]) * si
            vv = float(row[kv]) * sv if kv is not None else math.nan
        except (ValueError, IndexError):
            bad += 1
            return None
        if not (math.isfinite(tv) and math.isfinite(iv)):
            bad += 1
            return None
        return tv, iv, vv

    probe: list[tuple[float, float, float]] = []
    for row in rows:
        x = parse(row)
        if x is not None:
            probe.append(x)
            if len(probe) >= _PROBE_ROWS:
                break
    if len(probe) < 2:
        raise TraceError(f"{p.name}: fewer than two usable rows")
    if assumed_v and kv is not None:
        volts = sorted(abs(x[2]) for x in probe if math.isfinite(x[2]))
        if volts and volts[len(volts) // 2] >= 100.0:
            raise TraceError(
                f"{p.name}: the voltage column {header[kv]!r} has no unit and reads about "
                f"{volts[len(volts) // 2]:g}; pass --voltage-unit mV or V"
            )
    probe_t = sorted(x[0] for x in probe)
    steps = [b - a for a, b in itertools.pairwise(probe_t) if b > a]
    native = median(steps) if steps else 0.0
    block = AUTO_BLOCK_S if o.block_s is None else o.block_s

    if block > 0 and 0 < native < block * 0.999:
        binner = _Binner(probe[0][0], block, kv is not None)
        for x in probe:
            binner.add(*x)
        for row in rows:
            x = parse(row)
            if x is not None:
                binner.add(*x)
        factor = 1
        while len(binner.n) / factor > MAX_BINS:
            factor *= 2
        binner.merge(factor)
        t, i, v = binner.result()
        notes.append(
            f"{binner.samples} samples at {1 / native:.3g} Hz were block-averaged to "
            f"{_fmt_period(binner.width)} ({len(t)} points) while reading: charge is preserved, "
            f"events shorter than about {_fmt_period(2 * binner.width)} are not resolved as "
            "states of their own (--resample-ms sets the period)"
        )
    else:
        t = [x[0] for x in probe]
        i = [x[1] for x in probe]
        v = [x[2] for x in probe]
        for row in rows:
            x = parse(row)
            if x is not None:
                t.append(x[0])
                i.append(x[1])
                v.append(x[2])
    if bad:
        notes.append(f"{bad} rows without a numeric time and current were skipped")
    if iso:
        notes.append("ISO 8601 timestamps converted to seconds")
    if len(t) < 2:
        raise TraceError(f"{p.name}: fewer than two usable rows")
    trace = Trace(t, i, v if kv is not None else None, "csv", p.name, notes)
    return sort_and_dedupe(trace)


def detect_format(path: str | Path) -> str:
    p = Path(path)
    if is_sslog(p):
        return "sslog"
    with p.open(encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            if text.startswith("$"):
                return "ina219"
            fields = [x.strip() for x in re.split(r"[,;\t]", text)]
            if fields == LOGGER_HEADER:
                return "logger-csv"
            if fields[:3] == SSLOG_CSV_FIXED and "current_ma" in fields:
                return "sslog-csv"
            return "csv"
    raise TraceError(f"{p.name}: file is empty")


def load(path: str | Path, fmt: str = "auto", csv_options: CsvOptions | None = None) -> Trace:
    """Load any supported log. Raises :class:`TraceError` (or ``OSError``) on failure."""
    p = Path(path)
    if fmt not in FORMATS:
        raise TraceError(f"unknown format {fmt!r} (known: {', '.join(FORMATS)})")
    kind = detect_format(p) if fmt == "auto" else fmt
    if kind == "ina219":
        trace = load_ina219_log(p)
    elif kind == "logger-csv":
        trace = load_logger_csv(p)
    elif kind == "sslog":
        trace = load_sslog(p)
    elif kind == "sslog-csv":
        trace = load_sslog_csv(p)
    else:
        trace = load_generic_csv(p, csv_options)
    if len(trace) < 2:
        raise TraceError(f"{p.name}: fewer than two valid samples")
    return trace
