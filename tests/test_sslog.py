from __future__ import annotations

import struct
from pathlib import Path

import pytest

from embedded_power_profiler.sslog import (
    KIND_INFO,
    KIND_MARK,
    KIND_RX,
    KIND_TX,
    SslogError,
    SslogInfo,
    SslogWriter,
    is_sslog,
    log_parts,
    read_records,
    received_lines,
    uvarint,
)


def test_uvarint_encoding() -> None:
    # LEB128: 300 = 0b10_0101100 -> 0xAC 0x02
    assert uvarint(0) == b"\x00"
    assert uvarint(127) == b"\x7f"
    assert uvarint(300) == b"\xac\x02"
    with pytest.raises(ValueError):
        uvarint(-1)


def test_header_layout_matches_format(tmp_path: Path) -> None:
    path = tmp_path / "a.sslog"
    with SslogWriter(path, 1.5, {"port": "COM5"}) as w:
        w.write(KIND_RX, 1.5 + 0.000300, b"hi")
    raw = path.read_bytes()
    magic, version, start_ns, meta_len = struct.unpack("<5sBQI", raw[:18])
    assert (magic, version, start_ns) == (b"SSLOG", 1, 1_500_000_000)
    assert raw[18 : 18 + meta_len] == b'{"port": "COM5"}'
    # record: kind 1, dt 300 us = uvarint AC 02, length 2, payload
    assert raw[18 + meta_len :] == b"\x01\xac\x02\x02hi"


def test_round_trip_all_kinds(tmp_path: Path) -> None:
    path = tmp_path / "b.sslog"
    with SslogWriter(path, 100.0, {"decoder": "ina219"}) as w:
        w.write(KIND_INFO, 100.0, b"connected")
        w.write(KIND_RX, 100.25, b"abc")
        w.write(KIND_TX, 100.5, b"RATE 20\n")
        w.write(KIND_MARK, 101.0, b"note")
    info = SslogInfo(paths=[])
    recs = list(read_records(path, info))
    assert [(r.kind, r.t, r.payload) for r in recs] == [
        (KIND_INFO, 100.0, b"connected"),
        (KIND_RX, 100.25, b"abc"),
        (KIND_TX, 100.5, b"RATE 20\n"),
        (KIND_MARK, 101.0, b"note"),
    ]
    assert info.meta == {"decoder": "ina219"} and info.start_time == 100.0
    assert not info.truncated
    assert is_sslog(path)


def test_truncated_file_reads_complete_records(tmp_path: Path) -> None:
    path = tmp_path / "c.sslog"
    with SslogWriter(path, 0.0, {}) as w:
        w.write(KIND_RX, 0.1, b"first\n")
        w.write(KIND_RX, 0.2, b"second\n")
    path.write_bytes(path.read_bytes()[:-3])
    info = SslogInfo(paths=[])
    recs = list(read_records(path, info))
    assert [r.payload for r in recs] == [b"first\n"]
    assert info.truncated


def test_rejects_other_files(tmp_path: Path) -> None:
    bad = tmp_path / "x.sslog"
    bad.write_bytes(b"NOTAL" + bytes(13))
    with pytest.raises(SslogError, match="bad magic"):
        list(read_records(bad))
    bad.write_bytes(struct.pack("<5sBQI", b"SSLOG", 2, 0, 0))
    with pytest.raises(SslogError, match="version 2"):
        list(read_records(bad))
    bad.write_bytes(b"SSL")
    with pytest.raises(SslogError, match="too short"):
        list(read_records(bad))
    bad.write_bytes(struct.pack("<5sBQI", b"SSLOG", 1, 0, 5) + b"[1,2]")
    with pytest.raises(SslogError, match="not a JSON object"):
        list(read_records(bad))
    bad.write_bytes(struct.pack("<5sBQI", b"SSLOG", 1, 0, 3) + b"{x}")
    with pytest.raises(SslogError, match="not valid JSON"):
        list(read_records(bad))
    bad.write_bytes(struct.pack("<5sBQI", b"SSLOG", 1, 0, 9) + b"{}")
    with pytest.raises(SslogError, match="header truncated"):
        list(read_records(bad))
    bad.write_bytes(struct.pack("<5sBQI", b"SSLOG", 1, 0, 0) + b"\x09\x00\x00")
    with pytest.raises(SslogError, match="unknown record kind 9"):
        list(read_records(bad))


def test_rotated_parts_are_followed_in_order(tmp_path: Path) -> None:
    for name, t, payload in (
        ("run.sslog", 0.0, b"a\n"),
        ("run_002.sslog", 10.0, b"b\n"),
        ("run_003.sslog", 20.0, b"c\n"),
        ("run_005.sslog", 30.0, b"lost\n"),  # not consecutive: ignored
    ):
        with SslogWriter(tmp_path / name, t, {}) as w:
            w.write(KIND_RX, t + 1, payload)
    assert [p.name for p in log_parts(tmp_path / "run.sslog")] == [
        "run.sslog",
        "run_002.sslog",
        "run_003.sslog",
    ]
    lines = list(received_lines(read_records(tmp_path / "run.sslog")))
    assert lines == [(1.0, "a"), (11.0, "b"), (21.0, "c")]


def test_lines_are_reassembled_across_chunks(tmp_path: Path) -> None:
    path = tmp_path / "d.sslog"
    with SslogWriter(path, 0.0, {}) as w:
        w.write(KIND_RX, 1.0, b"$BM1,1")
        w.write(KIND_TX, 1.1, b"ignored\n")
        w.write(KIND_RX, 1.2, b",2*00\r\n#OK\r")
        w.write(KIND_RX, 1.3, b"\ntail")
    assert list(received_lines(read_records(path))) == [
        (1.2, "$BM1,1,2*00"),
        (1.3, "#OK"),
        (1.3, "tail"),
    ]
