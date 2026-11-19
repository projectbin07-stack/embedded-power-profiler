"""Reader and writer for serial-scope's ``.sslog`` raw log format (independent implementation).

Layout, as documented in https://github.com/projectbin07-stack/serial-scope
(``src/serial_scope/logfile.py``), all integers little-endian::

    header  = b"SSLOG" version:u8 start_ns:u64 meta_len:u32 meta:JSON(meta_len bytes)
    record  = kind:u8 dt_us:uvarint length:uvarint payload[length]

A record's host time is ``start + sum(dt_us)``. Kinds: 1 received bytes, 2 sent bytes,
3 marker text, 4 info text. A file cut short is read up to the last complete record. Rotated
sets (``run.sslog``, ``run_002.sslog``, ...) are read as one stream.
"""

from __future__ import annotations

import json
import re
import struct
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

MAGIC = b"SSLOG"
VERSION = 1
_HEADER = struct.Struct("<5sBQI")
KIND_RX = 1
KIND_TX = 2
KIND_MARK = 3
KIND_INFO = 4


class SslogError(ValueError):
    """Not an ``.sslog`` file, or an unsupported version."""


def uvarint(value: int) -> bytes:
    if value < 0:
        raise ValueError("uvarint cannot encode a negative number")
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _read_uvarint(f: BinaryIO) -> int | None:
    result = 0
    shift = 0
    while True:
        b = f.read(1)
        if not b:
            return None
        result |= (b[0] & 0x7F) << shift
        if not b[0] & 0x80:
            return result
        shift += 7
        if shift > 63:
            raise SslogError("varint too long")


def is_sslog(path: str | Path) -> bool:
    with Path(path).open("rb") as f:
        return f.read(len(MAGIC)) == MAGIC


def log_parts(path: str | Path) -> list[Path]:
    """``path`` followed by any consecutive rotation parts ``<stem>_002<suffix>``..."""
    path = Path(path)
    parts = [path]
    pattern = re.compile(re.escape(path.stem) + r"_(\d{3,})" + re.escape(path.suffix) + "$")
    found = sorted(
        (int(m.group(1)), p)
        for p in path.parent.glob(f"{path.stem}_*{path.suffix}")
        if (m := pattern.match(p.name))
    )
    expected = 2
    for index, p in found:
        if index != expected:
            break
        parts.append(p)
        expected += 1
    return parts


@dataclass
class Record:
    kind: int
    t: float  # host time, Unix seconds
    payload: bytes


@dataclass
class SslogInfo:
    paths: list[Path]
    meta: dict[str, Any] = field(default_factory=dict)
    start_time: float = 0.0
    truncated: bool = False


def _read_header(f: BinaryIO, name: str) -> tuple[float, dict[str, Any]]:
    raw = f.read(_HEADER.size)
    if len(raw) < _HEADER.size:
        raise SslogError(f"{name}: too short to be an .sslog file")
    magic, version, start_ns, meta_len = _HEADER.unpack(raw)
    if magic != MAGIC:
        raise SslogError(f"{name}: not an .sslog file (bad magic)")
    if version != VERSION:
        raise SslogError(f"{name}: unsupported .sslog version {version}")
    meta_raw = f.read(meta_len)
    if len(meta_raw) < meta_len:
        raise SslogError(f"{name}: header truncated")
    try:
        meta = json.loads(meta_raw.decode("utf-8")) if meta_len else {}
    except ValueError as exc:
        raise SslogError(f"{name}: header metadata is not valid JSON") from exc
    if not isinstance(meta, dict):
        raise SslogError(f"{name}: header metadata is not a JSON object")
    return start_ns / 1e9, meta


def read_records(path: str | Path, info: SslogInfo | None = None) -> Iterator[Record]:
    """Yield every record of a log set in order; ``info`` (if given) is filled in."""
    paths = log_parts(path)
    if info is not None:
        info.paths = paths
    for index, part in enumerate(paths):
        with part.open("rb") as f:
            start, meta = _read_header(f, part.name)
            if index == 0 and info is not None:
                info.start_time, info.meta = start, meta
            t_us = round(start * 1e6)
            while True:
                kind = f.read(1)
                if not kind:
                    break
                dt = _read_uvarint(f)
                length = _read_uvarint(f) if dt is not None else None
                if length is None or dt is None:
                    if info is not None:
                        info.truncated = True
                    break
                if kind[0] not in (KIND_RX, KIND_TX, KIND_MARK, KIND_INFO):
                    raise SslogError(f"{part.name}: unknown record kind {kind[0]}")
                payload = f.read(length)
                if len(payload) < length:
                    if info is not None:
                        info.truncated = True
                    break
                t_us += dt
                yield Record(kind[0], t_us / 1e6, payload)


def received_lines(records: Iterator[Record]) -> Iterator[tuple[float, str]]:
    """Reassemble received bytes into text lines, each stamped with the time its end arrived."""
    pending = b""
    last_t = 0.0
    for rec in records:
        if rec.kind != KIND_RX:
            continue
        last_t = rec.t
        pending += rec.payload
        *lines, pending = pending.split(b"\n")
        for line in lines:
            yield rec.t, line.rstrip(b"\r").decode("ascii", errors="replace")
    if pending.strip():
        yield last_t, pending.rstrip(b"\r").decode("ascii", errors="replace")


class SslogWriter:
    """Write a single-part ``.sslog`` file (used for fixtures and synthetic demos)."""

    def __init__(self, path: str | Path, start_time: float, meta: dict[str, Any]) -> None:
        self._f = Path(path).open("wb")  # noqa: SIM115 - closed by close() / __exit__
        meta_bytes = json.dumps(meta, sort_keys=True).encode("utf-8")
        self._last_us = round(start_time * 1e6)
        self._f.write(_HEADER.pack(MAGIC, VERSION, self._last_us * 1000, len(meta_bytes)))
        self._f.write(meta_bytes)

    def write(self, kind: int, t: float, payload: bytes) -> None:
        t_us = round(t * 1e6)
        dt = max(0, t_us - self._last_us)
        self._last_us += dt
        self._f.write(bytes([kind]) + uvarint(dt) + uvarint(len(payload)) + payload)

    def close(self) -> None:
        self._f.close()

    def __enter__(self) -> SslogWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
