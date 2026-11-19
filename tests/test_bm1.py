from __future__ import annotations

import pytest

from embedded_power_profiler.bm1 import Frame, FrameError, crc8, decode_frame


def test_crc8_check_value() -> None:
    # The CRC-8/SMBUS check value from the firmware's telemetry.h: crc8("123456789") == 0xF4.
    assert crc8(b"123456789") == 0xF4
    assert crc8(b"") == 0


def test_decodes_frame_from_ina219_battery_monitor_fixture() -> None:
    # Copied from ina219-battery-monitor tools/tests/data/sample_session.log (line 3).
    f = decode_frame("$BM1,1,1012,4010,151900,609000,21,84,790,0*2C\r\n")
    assert f == Frame(1, 1012, 4010, 151900, 609000, 21, 84, 790, 0)


def test_encode_round_trip_and_negative_current() -> None:
    frame = Frame(7, 4294967295, 3300, -1500, -4950, -2, -7, 1000, 9)
    line = frame.encode()
    assert line.startswith("$BM1,7,4294967295,3300,-1500,")
    assert decode_frame(line) == frame


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("BM1,1,1,1,1,1,1,1,1,1*00", "start marker"),
        ("$BM1,1,1,1,1,1,1,1,1,1", "checksum marker"),
        ("$BM1,1,1,1,1,1,1,1,1,1*G0", "malformed checksum"),
        ("$BM1,1,1012,4010,151900,609000,21,84,790,0*2D", "checksum mismatch"),
        ("$BM1,1,2,3é*00", "non-ASCII"),
    ],
)
def test_rejects_malformed_lines(line: str, message: str) -> None:
    with pytest.raises(FrameError, match=message):
        decode_frame(line)


def _framed(body: str) -> str:
    return f"${body}*{crc8(body.encode('ascii')):02X}"


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("XX1,1,1,1,1,1,1,1,1,1", "unknown frame tag"),
        ("BM1,1,1,1,1,1,1,1,1", "expected 9 fields"),
        ("BM1,1,1,1,1,1,1,1,1001,1", "out of range"),
        ("BM1,1,1,1,1.5,1,1,1,1,1", "not a decimal integer"),
        ("BM1,-1,1,1,1,1,1,1,1,1", "out of range"),
    ],
)
def test_rejects_bad_fields_with_valid_crc(body: str, message: str) -> None:
    with pytest.raises(FrameError, match=message):
        decode_frame(_framed(body))
