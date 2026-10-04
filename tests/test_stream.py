"""Tests for stream.py (SPEC.md section 5, test_stream.py). No hardware."""

from __future__ import annotations

import socket
import struct
import threading

import pytest

from a1580_openhtf.stream import (
    HEADER_FORMAT,
    HEADER_SIZE,
    MAGIC,
    AScanHeader,
    FrameReader,
    _split,
    build_packet,
    packet_size,
    parse_header,
    split_packets,
)

LENGTH = 8


def _pkt(number: int = 0, length: int = LENGTH) -> bytes:
    return build_packet(range(length), packet_number=number)


# ── 1. layout ────────────────────────────────────────────────────────────────


def test_layout_constants() -> None:
    assert MAGIC == b'FtH1'
    assert struct.calcsize(HEADER_FORMAT) == HEADER_SIZE == 28


@pytest.mark.parametrize(('length', 'size'), [(1, 30), (1024, 2076), (8192, 16412)])
def test_packet_size(length: int, size: int) -> None:
    assert packet_size(length) == size


@pytest.mark.parametrize('length', [0, -1])
def test_packet_size_rejects_non_positive(length: int) -> None:
    with pytest.raises(ValueError):
        packet_size(length)


def test_build_parse_round_trip() -> None:
    packet = build_packet(
        [1, -2, 32767, -32768],
        packet_number=200,
        ctp=(1, 2, 3),
        length_lo=4,
        length_hi=5,
        telemetry_a=6,
        telemetry_b=7,
        telemetry_c=8,
        is_full=1,
        buffer_fill=9,
        ascan_count=10,
        reserved_b=11,
        reserved_c=12,
    )
    assert len(packet) == packet_size(4)
    assert parse_header(packet) == AScanHeader(
        MAGIC, (1, 2, 3), 4, 5, 200, 6, 7, 8, 1, 9, 10, 11, 12
    )
    assert struct.unpack('<4h', packet[HEADER_SIZE:]) == (1, -2, 32767, -32768)
    assert parse_header(packet[:HEADER_SIZE]).packet_number == 200  # header alone is enough


def test_build_packet_defaults_to_zero_fields() -> None:
    header = parse_header(build_packet([0]))
    assert header.magic == MAGIC
    assert header.ctp == (0, 0, 0)
    assert header[3:] == (0,) * 10


def test_build_packet_rejects_bad_input() -> None:
    with pytest.raises(TypeError, match='nonsense'):
        build_packet([0], nonsense=1)
    with pytest.raises(ValueError):
        build_packet([0], packet_number=256)
    with pytest.raises(ValueError):
        build_packet([0], ctp=(1, 2))


def test_parse_header_wrong_magic() -> None:
    bad = b'XXXX' + _pkt()[4:]
    with pytest.raises(ValueError, match='magic'):
        parse_header(bad)


@pytest.mark.parametrize('size', [0, 4, 27])
def test_parse_header_short_buffer(size: int) -> None:
    with pytest.raises(ValueError, match='28'):
        parse_header(_pkt()[:size])


# ── 2. split_packets ─────────────────────────────────────────────────────────


def test_split_exact_packets() -> None:
    a, b = _pkt(0), _pkt(1)
    buf = bytearray(a)
    assert split_packets(buf, LENGTH) == [a]
    assert buf == b''
    buf = bytearray(a + b)
    assert split_packets(buf, LENGTH) == [a, b]
    assert not buf


def test_split_two_packets_in_one_chunk_keeps_tail() -> None:
    a, b, c = _pkt(0), _pkt(1), _pkt(2)
    buf = bytearray(a + b + c[:10])
    assert split_packets(buf, LENGTH) == [a, b]
    assert bytes(buf) == c[:10]


def test_split_one_packet_across_three_chunks() -> None:
    p = _pkt(7)
    buf = bytearray()
    found: list[bytes] = []
    for chunk in (p[:5], p[5:30], p[30:]):
        buf += chunk
        found += split_packets(buf, LENGTH)
    assert found == [p]
    assert not buf


def test_split_drops_garbage_before_magic() -> None:
    p = _pkt(3)
    buf = bytearray(b'garbage!' + p)
    assert split_packets(buf, LENGTH) == [p]
    assert not buf
    # the bytes dropped are counted by FrameReader.stats (see test_reader_counts_resync)


def test_split_no_magic_keeps_everything_up_to_two_packets() -> None:
    size = packet_size(LENGTH)
    buf = bytearray(b'\x00' * (2 * size))
    assert split_packets(buf, LENGTH) == []
    assert len(buf) == 2 * size


def test_split_no_magic_longer_than_two_packets_keeps_last_packet_size() -> None:
    size = packet_size(LENGTH)
    data = bytes(i % 251 for i in range(2 * size + 1))  # no 'FtH1' in there
    assert MAGIC not in data
    buf = bytearray(data)
    assert split_packets(buf, LENGTH) == []
    assert bytes(buf) == data[-size:]


def test_split_magic_split_across_chunks_is_not_lost() -> None:
    p = _pkt(4)
    buf = bytearray(b'zz' + p[:2])
    assert split_packets(buf, LENGTH) == []
    buf += p[2:]
    assert split_packets(buf, LENGTH) == [p]


# ── 2b. misalignment ─────────────────────────────────────────────────────────


def test_split_counts_a_packet_not_followed_by_magic() -> None:
    a, b = _pkt(0), _pkt(1)
    buf = bytearray(a + b'XXXXXXXX' + b)  # the next 4+ bytes do not start with FtH1
    assert _split(buf, LENGTH)[0] == [a, b]
    buf = bytearray(a + b'XXXXXXXX' + b)
    _packets, dropped, misaligned = _split(buf, LENGTH)
    assert (dropped, misaligned) == (8, 1)


def test_split_aligned_and_short_tails_are_not_misaligned() -> None:
    a, b = _pkt(0), _pkt(1)
    assert _split(bytearray(a + b), LENGTH)[2] == 0
    assert _split(bytearray(a + b[:3]), LENGTH)[2] == 0  # fewer than 4 bytes: cannot tell
    assert _split(bytearray(a + b[:4]), LENGTH)[2] == 0  # starts with the magic
    assert _split(bytearray(b'junk' + a), LENGTH)[2] == 0  # garbage before is dropped, not this


def test_reader_counts_misaligned_packets() -> None:
    a, b = _pkt(0), _pkt(1)
    reader = FrameReader(StubSocket([a + b'XXXXXXXX' + b]), LENGTH)
    assert reader.read(2, 1.0) == [a, b]
    assert reader.misaligned == reader.stats.misaligned == 1
    assert reader.dropped_bytes == 8
    clean = FrameReader(StubSocket([a + b]), LENGTH)
    clean.read(2, 1.0)
    assert clean.misaligned == 0


def test_reader_with_too_small_a_length_is_misaligned() -> None:
    wide = build_packet(range(LENGTH * 2), packet_number=0)  # 16 samples on an 8-sample reader
    reader = FrameReader(StubSocket([wide]), LENGTH)
    assert len(reader.read(1, 1.0)) == 1
    assert reader.misaligned == 1


# ── 3. FrameReader against a scripted stub ───────────────────────────────────


class StubSocket:
    """recv returns the scripted chunks; an exception instance in the script is raised."""

    def __init__(self, script: list[bytes | BaseException]) -> None:
        self.script = list(script)
        self.timeouts: list[float | None] = []

    def settimeout(self, value: float | None) -> None:
        self.timeouts.append(value)

    def recv(self, n: int) -> bytes:
        if not self.script:
            raise TimeoutError('script exhausted')
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def test_reader_collects_n_from_odd_chunks() -> None:
    packets = [_pkt(i) for i in range(3)]
    stream = b''.join(packets)
    script: list[bytes | BaseException] = [stream[i : i + 13] for i in range(0, len(stream), 13)]
    reader = FrameReader(StubSocket(script), LENGTH)
    assert reader.read(3, 1.0) == packets
    assert reader.stats.packets == reader.packets == 3
    assert reader.dropped_bytes == reader.resyncs == 0


def test_reader_keeps_packets_beyond_n_for_the_next_call() -> None:
    packets = [_pkt(i) for i in range(3)]
    reader = FrameReader(StubSocket([b''.join(packets)]), LENGTH)
    assert reader.read(1, 1.0) == packets[:1]
    assert reader.read(2, 1.0) == packets[1:]


def test_reader_counts_resync() -> None:
    p = _pkt(0)
    reader = FrameReader(StubSocket([b'junk' + p]), LENGTH)
    assert reader.read(1, 1.0) == [p]
    assert reader.dropped_bytes == 4
    assert reader.resyncs == 1


def test_reader_timeout_reports_count_and_keeps_the_packets() -> None:
    a = _pkt(0)
    reader = FrameReader(StubSocket([a, TimeoutError('x')]), LENGTH)
    with pytest.raises(TimeoutError, match=r'1 of 2 packets'):
        reader.read(2, 0.05)
    assert reader.read(1, 1.0) == [a]  # not lost by the failed call
    assert reader.packets == 1


def test_reader_timeout_with_nothing() -> None:
    reader = FrameReader(StubSocket([]), LENGTH)
    with pytest.raises(TimeoutError, match=r'0 of 1 packets'):
        reader.read(1, 0.05)


def test_reader_empty_recv_is_connection_error() -> None:
    reader = FrameReader(StubSocket([_pkt(0), b'']), LENGTH)
    with pytest.raises(ConnectionError):
        reader.read(2, 1.0)


@pytest.mark.parametrize(('n', 'timeout_s'), [(0, 1.0), (1, 0.0), (1, -1.0)])
def test_reader_bad_arguments(n: int, timeout_s: float) -> None:
    with pytest.raises(ValueError):
        FrameReader(StubSocket([]), LENGTH).read(n, timeout_s)


def test_reader_sets_the_socket_timeout() -> None:
    sock = StubSocket([_pkt(0)])
    FrameReader(sock, LENGTH).read(1, 2.0)
    assert sock.timeouts and all(t is not None and 0 < t <= 2.0 for t in sock.timeouts)


# ── 4. FrameReader against a localhost TCP server (the only socket test) ─────


def test_reader_over_localhost_tcp() -> None:
    packets = [_pkt(i) for i in range(5)]
    stream = b'xx' + b''.join(packets)  # two garbage bytes in front
    sizes = [1, 7, 30, 3, 100, 11]  # odd pieces, cycled
    failures: list[BaseException] = []
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(('localhost', 0))
    server.listen(1)
    server.settimeout(5)
    port = server.getsockname()[1]

    def serve() -> None:
        try:
            conn, _ = server.accept()
            with conn:
                pos, i = 0, 0
                while pos < len(stream):
                    step = sizes[i % len(sizes)]
                    conn.sendall(stream[pos : pos + step])
                    pos += step
                    i += 1
        except BaseException as exc:  # noqa: BLE001 - reported in the main thread
            failures.append(exc)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    client = socket.create_connection(('localhost', port), timeout=5)
    try:
        reader = FrameReader(client, LENGTH)
        assert reader.read(5, 5.0) == packets
        assert reader.dropped_bytes == 2
        assert reader.resyncs == 1
    finally:
        client.close()
        thread.join(5)
        server.close()
    assert not thread.is_alive()
    assert not failures
