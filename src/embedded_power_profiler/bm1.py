"""The ``$BM1`` telemetry frame of ina219-battery-monitor (independent re-implementation).

Format, as documented in https://github.com/projectbin07-stack/ina219-battery-monitor
(``lib/powercore/src/powercore/telemetry.h``)::

    $BM1,<seq>,<t_ms>,<bus_mV>,<current_uA>,<power_uW>,<charge_uAh>,<energy_uWh>,<soc_permille>,<flags>*<HH>

All fields are decimal integers. ``HH`` is CRC-8 (polynomial 0x07, initial value 0, MSB first,
no reflection, no final XOR) over every character between ``$`` and ``*``, as two hex digits.
Lines starting with ``#`` are status messages from the firmware.
"""

from __future__ import annotations

from dataclasses import dataclass

FRAME_TAG = "BM1"
UINT32_MAX = 0xFFFFFFFF
INT32_MIN = -(2**31)
INT32_MAX = 2**31 - 1

FLAG_OVERFLOW = 0x01  # INA219 math overflow: current and power are not valid
FLAG_NOT_READY = 0x02  # no new conversion since the previous read: values are repeated
FLAG_SENSOR_ERROR = 0x04  # I2C failure: measurement fields are zero
FLAG_CUTOFF = 0x08  # low-voltage cutoff tripped (measurement still valid)

# Samples carrying these flags have no valid current and are dropped by the loaders.
INVALID_FLAGS = FLAG_OVERFLOW | FLAG_SENSOR_ERROR

_RANGES: tuple[tuple[str, int, int], ...] = (
    ("seq", 0, UINT32_MAX),
    ("t_ms", 0, UINT32_MAX),
    ("bus_mV", INT32_MIN, INT32_MAX),
    ("current_uA", INT32_MIN, INT32_MAX),
    ("power_uW", INT32_MIN, INT32_MAX),
    ("charge_uAh", INT32_MIN, INT32_MAX),
    ("energy_uWh", INT32_MIN, INT32_MAX),
    ("soc_permille", 0, 1000),
    ("flags", 0, 255),
)


class FrameError(ValueError):
    """A line that is not a valid ``$BM1`` frame."""


def crc8(data: bytes, crc: int = 0x00) -> int:
    """CRC-8, polynomial x^8 + x^2 + x + 1 (0x07), MSB first. ``crc8(b"123456789") == 0xF4``."""
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


@dataclass(frozen=True)
class Frame:
    """One telemetry frame in the integer units used on the wire."""

    seq: int
    t_ms: int
    bus_mv: int
    current_ua: int
    power_uw: int
    charge_uah: int
    energy_uwh: int
    soc_permille: int
    flags: int

    def fields(self) -> list[int]:
        return [
            self.seq,
            self.t_ms,
            self.bus_mv,
            self.current_ua,
            self.power_uw,
            self.charge_uah,
            self.energy_uwh,
            self.soc_permille,
            self.flags,
        ]

    def encode(self) -> str:
        """The wire form without a line terminator."""
        body = FRAME_TAG + "," + ",".join(str(v) for v in self.fields())
        return f"${body}*{crc8(body.encode('ascii')):02X}"


def _parse_int(text: str, low: int, high: int, name: str) -> int:
    digits = text[1:] if text.startswith("-") else text
    if not digits or not digits.isascii() or not digits.isdigit() or len(digits) > 10:
        raise FrameError(f"field {name!r} is not a decimal integer: {text!r}")
    value = int(text)
    if not low <= value <= high:
        raise FrameError(f"field {name!r} out of range: {value}")
    return value


def decode_frame(line: str) -> Frame:
    """Decode one line (a trailing CR/LF is accepted). Raises :class:`FrameError`."""
    text = line.strip()
    if not text.startswith("$"):
        raise FrameError("missing '$' start marker")
    body, star, checksum = text[1:].rpartition("*")
    if not star:
        raise FrameError("missing '*' checksum marker")
    if len(checksum) != 2 or any(c not in "0123456789ABCDEFabcdef" for c in checksum):
        raise FrameError(f"malformed checksum: {checksum!r}")
    try:
        raw = body.encode("ascii")
    except UnicodeEncodeError as exc:
        raise FrameError("non-ASCII character in frame") from exc
    expected = crc8(raw)
    if int(checksum, 16) != expected:
        raise FrameError(f"checksum mismatch: got {checksum.upper()}, computed {expected:02X}")
    fields = body.split(",")
    if fields[0] != FRAME_TAG:
        raise FrameError(f"unknown frame tag: {fields[0]!r}")
    if len(fields) != len(_RANGES) + 1:
        raise FrameError(f"expected {len(_RANGES)} fields, got {len(fields) - 1}")
    values = [
        _parse_int(text, low, high, name)
        for text, (name, low, high) in zip(fields[1:], _RANGES, strict=True)
    ]
    return Frame(*values)
