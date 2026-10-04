"""A-scan packet layout, parser and stream framing. See SPEC.md section 2.

Pure Python, standard library only. The only I/O is `FrameReader.read`, which calls
`recv` on whatever socket-like object it was given.

Every statement about the packet layout and the resynchronisation algorithm comes from
`PROTOCOL.md` ("Packet layout", "Resynchronisation algorithm"). Nothing here has been
verified on hardware.
"""

from __future__ import annotations

import array
import struct
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import NamedTuple, Protocol

MAGIC = b'FtH1'
HEADER_FORMAT = '<4s3IH2B6B2B'
HEADER_SIZE = 28

_HEADER = struct.Struct(HEADER_FORMAT)
_RECV_BYTES = 65536


class AScanHeader(NamedTuple):
    """The 28-byte packet header. The meaning of most fields is UNKNOWN (PROTOCOL.md)."""

    magic: bytes
    ctp: tuple[int, int, int]
    length_lo: int
    length_hi: int
    packet_number: int
    telemetry_a: int
    telemetry_b: int
    telemetry_c: int
    is_full: int
    buffer_fill: int
    ascan_count: int
    reserved_b: int
    reserved_c: int


class _RecvSocket(Protocol):
    """What `FrameReader` needs from a socket."""

    def recv(self, n: int, /) -> bytes: ...

    def settimeout(self, value: float | None, /) -> None: ...


# ── layout ──────────────────────────────────────────────────────────────────


def packet_size(length: int) -> int:
    """Bytes in one packet for `length` samples: 28 + 2 * length."""
    if length < 1:
        raise ValueError(f'length must be >= 1, got {length}')
    return HEADER_SIZE + 2 * length


def parse_header(buf: bytes) -> AScanHeader:
    """Parse the header at the start of `buf` (which may be a whole packet).

    Raises ValueError if `buf` is shorter than 28 bytes or does not start with `FtH1`.
    """
    if len(buf) < HEADER_SIZE:
        raise ValueError(f'need at least {HEADER_SIZE} header bytes, got {len(buf)}')
    values = _HEADER.unpack_from(buf)
    if values[0] != MAGIC:
        raise ValueError(f'bad magic {bytes(values[0])!r}, expected {MAGIC!r}')
    return AScanHeader(values[0], (values[1], values[2], values[3]), *values[4:])


def build_packet(
    samples: Iterable[int], *, packet_number: int = 0, **header_fields: object
) -> bytes:
    """Inverse of `parse_header` plus the sample block. Used by the fake and by tests.

    `header_fields` are any `AScanHeader` fields except `packet_number` (default 0 for
    every integer field, `ctp` defaults to (0, 0, 0), `magic` to `MAGIC`).
    """
    fields: dict[str, object] = {
        'magic': MAGIC,
        'ctp': (0, 0, 0),
        'length_lo': 0,
        'length_hi': 0,
        'telemetry_a': 0,
        'telemetry_b': 0,
        'telemetry_c': 0,
        'is_full': 0,
        'buffer_fill': 0,
        'ascan_count': 0,
        'reserved_b': 0,
        'reserved_c': 0,
    }
    unknown = set(header_fields) - set(fields)
    if unknown:
        raise TypeError(f'unknown header field(s): {sorted(unknown)}')
    fields.update(header_fields)
    body = array.array('h', samples)
    if sys.byteorder == 'big':
        body.byteswap()
    ctp = fields['ctp']
    if not isinstance(ctp, tuple) or len(ctp) != 3:
        raise ValueError(f'ctp must be a tuple of 3 ints, got {ctp!r}')
    try:
        header = _HEADER.pack(
            fields['magic'],
            *ctp,
            fields['length_lo'],
            fields['length_hi'],
            packet_number,
            fields['telemetry_a'],
            fields['telemetry_b'],
            fields['telemetry_c'],
            fields['is_full'],
            fields['buffer_fill'],
            fields['ascan_count'],
            fields['reserved_b'],
            fields['reserved_c'],
        )
    except struct.error as exc:
        raise ValueError(f'header field out of range: {exc}') from exc
    return header + body.tobytes()


# ── framing ─────────────────────────────────────────────────────────────────


def _split(buffer: bytearray, length: int) -> tuple[list[bytes], int]:
    """`split_packets` that also returns how many bytes were thrown away."""
    size = packet_size(length)
    packets: list[bytes] = []
    dropped = 0
    pos = 0
    while True:
        idx = buffer.find(MAGIC, pos)
        if idx < 0:
            # No magic in the rest: keep everything unless it is longer than two packets,
            # then keep only the last `size` bytes (PROTOCOL.md step 4).
            rest = len(buffer) - pos
            if rest > 2 * size:
                dropped += rest - size
                pos = len(buffer) - size
            break
        dropped += idx - pos  # bytes before the magic (step 5)
        pos = idx
        if len(buffer) - pos < size:
            break  # incomplete packet, wait for more data (step 6)
        packets.append(bytes(buffer[pos : pos + size]))
        pos += size
    del buffer[:pos]
    return packets, dropped


def split_packets(buffer: bytearray, length: int) -> list[bytes]:
    """Cut complete packets out of `buffer` in place (the vendor resync algorithm).

    Bytes before the first `FtH1` are dropped. Complete packets of `packet_size(length)`
    bytes are returned in order and removed from `buffer`; an incomplete tail stays. With
    no magic at all and more than two packets of data, only the last `packet_size(length)`
    bytes are kept. Like the vendor, it does not check that the next packet starts with
    the magic, nor `packet_number` continuity.
    """
    return _split(buffer, length)[0]


@dataclass
class FrameStats:
    """Counters kept by `FrameReader` (plain ints, also readable as reader attributes)."""

    packets: int = 0  # complete packets cut from the stream
    dropped_bytes: int = 0  # bytes discarded by resynchronisation
    resyncs: int = 0  # number of times some bytes had to be discarded


class FrameReader:
    """Collect whole packets from a TCP-like socket.

    The reader owns the byte buffer, so a partial packet at the end of one `read` is
    completed by the next one, and packets that arrived beyond the requested count are
    handed out by later calls.

    `stats` is a `FrameStats` (`packets`, `dropped_bytes`, `resyncs`, all plain ints);
    the same three counters are also readable directly on the reader.
    """

    def __init__(self, sock: _RecvSocket, length: int) -> None:
        packet_size(length)  # validates length
        self._sock = sock
        self._length = length
        self._buffer = bytearray()
        self._pending: list[bytes] = []
        self.stats = FrameStats()

    @property
    def packets(self) -> int:
        return self.stats.packets

    @property
    def dropped_bytes(self) -> int:
        return self.stats.dropped_bytes

    @property
    def resyncs(self) -> int:
        return self.stats.resyncs

    def read(self, n: int, timeout_s: float) -> list[bytes]:
        """Return `n` complete packets.

        Raises TimeoutError (message carries the count so far; the packets already
        collected stay available for the next call) when `timeout_s` passes, ConnectionError
        when the peer closes the connection, ValueError for bad arguments.
        """
        if n < 1:
            raise ValueError(f'n must be >= 1, got {n}')
        if timeout_s <= 0:
            raise ValueError(f'timeout_s must be > 0, got {timeout_s}')
        deadline = time.monotonic() + timeout_s
        while len(self._pending) < n:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f'FrameReader: {len(self._pending)} of {n} packets within {timeout_s} s '
                    f'(packets={self.packets}, dropped_bytes={self.dropped_bytes}, '
                    f'resyncs={self.resyncs})'
                )
            self._sock.settimeout(remaining)
            try:
                chunk = self._sock.recv(_RECV_BYTES)
            except TimeoutError:  # socket.timeout is an alias of TimeoutError
                continue
            if not chunk:
                raise ConnectionError(
                    f'data socket closed by the peer after {self.packets} packets '
                    f'({len(self._pending)} of {n} collected)'
                )
            self._buffer += chunk
            found, dropped = _split(self._buffer, self._length)
            self.stats.packets += len(found)
            if dropped:
                self.stats.dropped_bytes += dropped
                self.stats.resyncs += 1
            self._pending += found
        out = self._pending[:n]
        del self._pending[:n]
        return out
