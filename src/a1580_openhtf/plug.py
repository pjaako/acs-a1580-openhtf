# Adapted from rigol-dho-openhtf rigol_dho_plug.py (structure of the plug, check_errors,
# write_checked, apply_setup with read-back, get_state/set_state, tearDown). The SCPI
# dialect, the data channel, the streaming and values_match are specific to the A1580.
"""OpenHTF plug for the ACS A1580 ultrasonic pulser-receiver. See SPEC.md section 3.

Everything here is built from the vendor material digested in `PROTOCOL.md` and has not
been run against a real instrument.
"""

from __future__ import annotations

import math
import re
import socket
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

import numpy as np
from openhtf.plugs import BasePlug
from openhtf.util import configuration

from .stream import HEADER_SIZE, AScanHeader, FrameReader, parse_header

if TYPE_CHECKING:
    from .capture import Capture

CONF = configuration.CONF
CONF.declare(
    'a1580_host',
    default_value='192.168.200.18',
    description='IP address or host name of the A1580 (vendor factory default address)',
)
CONF.declare(
    'a1580_scpi_port',
    default_value=5025,
    description='TCP port of the A1580 SCPI session',
)
CONF.declare(
    'a1580_restore_state',
    default_value=True,
    description=('Restore the settings snapshot taken when the plug was created, in tearDown()'),
)

# ── header normalisation ─────────────────────────────────────────────────────

_ROOTS = ('SOUR', 'SENS')
_TRAILING_OPTIONAL = ('LEV', 'VAL', 'NEXT')  # PULSe[:LEVel], CONStant[:VALue], ERRor[:NEXT]

# Every SCPI node the A1580 documents (PROTOCOL.md command list): canonical short form ->
# all accepted spellings, upper case. A node outside this table is an error, never guessed:
# a typo such as `GAINX` or `FREQUE` must not be taken for `GAIN` or `FREQ`. The vendor writes
# `MODe` and `SPLIT` but its code also sends `MODE`, so both spellings of those are accepted.
_NODE_FORMS: dict[str, tuple[str, ...]] = {
    'TRAN': ('TRAN', 'TRANSMITTER'),
    'PULS': ('PULS', 'PULSE'),
    'FREQ': ('FREQ', 'FREQUENCY'),
    'ENAB': ('ENAB', 'ENABLE'),
    'REV': ('REV', 'REVERSE'),
    'DAMP': ('DAMP',),
    'IMP': ('IMP', 'IMPEDANCE'),
    'TRIG': ('TRIG', 'TRIGGERING'),
    'MODE': ('MOD', 'MODE'),
    'INT': ('INT', 'INTERVAL'),
    'DEL': ('DEL', 'DELAY'),
    'GAIN': ('GAIN',),
    'LEV': ('LEV', 'LEVEL'),
    'PRE': ('PRE', 'PREAMP'),
    'COMB': ('COMB', 'COMBINED'),
    'SPLIT': ('SPL', 'SPLIT'),
    'TGC': ('TGC',),
    'LIN': ('LIN', 'LINEAR'),
    'ARB': ('ARB', 'ARBITRARY'),
    'AVER': ('AVER', 'AVERAGE'),
    'COUN': ('COUN', 'COUNT'),
    'CONS': ('CONS', 'CONSTANT'),
    'VAL': ('VAL', 'VALUE'),
    'AUTO': ('AUTO',),
    'RAND': ('RAND', 'RANDOM'),
    'FILT': ('FILT', 'FILTER'),
    'HPAS': ('HPAS', 'HPASS'),
    'IND': ('IND', 'INDEX'),
    'DATA': ('DATA',),
    'LENG': ('LENG', 'LENGTH'),
    'PORT': ('PORT',),
    'MEM': ('MEM', 'MEMORY'),
    'CLE': ('CLE', 'CLEAR'),
    'SYST': ('SYST', 'SYSTEM'),
    'ERR': ('ERR', 'ERROR'),
    'NEXT': ('NEXT',),
    'VERS': ('VERS', 'VERSION'),
    'SOUR': ('SOUR', 'SOURCE'),
    'SENS': ('SENS', 'SENSE'),
    'STAR': ('STAR', 'START'),
    'STOP': ('STOP',),
    'DUR': ('DUR', 'DURATION'),
    'TYPE': ('TYPE',),
    'GAP': ('GAP',),
}
_NODES: dict[str, str] = {form: short for short, forms in _NODE_FORMS.items() for form in forms}
# IEEE 488.2 common commands (PROTOCOL.md "Common commands"), one `*` node each.
_COMMON_COMMANDS = frozenset(
    {'*IDN', '*CLS', '*ESE', '*ESR', '*OPC', '*RST', '*SRE', '*STB', '*TST', '*WAI'}
)


# Consecutive transport-type failures (not a device-reported RuntimeError) after which
# `get_state` / `set_state` give up on the remaining headers instead of timing out on each.
_DEAD_LINK_LIMIT = 3


def normalize_header(header: str) -> str:
    """Canonical key of a SCPI header, accepting long and short forms.

    Upper-cases, drops a trailing `?`, a leading `:` and a leading `SOUR:`/`SENS:` root,
    reduces each mnemonic to its short form and drops the optional last nodes `:ENAB` of
    `TRAN:DAMP`, `:LEV`, `:VAL` and `:NEXT`. `TRANsmitter:PULSe:LEVel` -> `TRAN:PULS`.

    Every node must be a known short or long form from the A1580 command list (see
    `_NODE_FORMS`) or a common command such as `*IDN`; anything else raises `ValueError`
    naming the node (`GAINX`, `FREQUE`, the empty header).
    """
    text = header.strip().removesuffix('?').lstrip(':')
    raw_nodes = [n for n in text.split(':') if n]
    if not raw_nodes:
        raise ValueError(f'empty SCPI header {header!r}')
    nodes: list[str] = []
    for raw in raw_nodes:
        upper = raw.upper()
        node = _NODES.get(upper) or (upper if upper in _COMMON_COMMANDS else None)
        if node is None:
            raise ValueError(f'unknown SCPI node {raw!r} in header {header!r}')
        nodes.append(node)
    if len(nodes) > 1 and nodes[0] in _ROOTS:
        nodes.pop(0)
    if len(nodes) > 1 and nodes[-1] in _TRAILING_OPTIONAL:
        nodes.pop()
    if nodes[-2:] == ['DAMP', 'ENAB']:
        nodes.pop()
    return ':'.join(nodes)


# ── value comparison (SPEC 3.1) ──────────────────────────────────────────────

#: Unit of the query reply, by normalised header. Headers not listed reply unit-less.
REPLY_UNITS: dict[str, str] = {
    'FREQ': 'Hz',
    'TRAN:FREQ': 'Hz',
    'TRIG:INT': 's',
    'AVER:DEL:CONS': 's',
    'AVER:DEL:RAND': 's',
    'TRIG:DEL': 'ns',
    'TRAN:GAP': 'ns',
    'TRAN:DAMP:GAP': 'ns',
    'TRAN:PULS': 'V',
    'GAIN': 'dB',
}

# Argument unit suffix -> (dimension, factor to the SI base unit of that dimension).
_UNIT_SUFFIXES: dict[str, tuple[str, float]] = {
    'HZ': ('Hz', 1.0),
    'KHZ': ('Hz', 1e3),
    'MHZ': ('Hz', 1e6),
    'GHZ': ('Hz', 1e9),
    'S': ('s', 1.0),
    'MS': ('s', 1e-3),
    'US': ('s', 1e-6),
    'NS': ('s', 1e-9),
    'V': ('V', 1.0),
    'MV': ('V', 1e-3),
    'DB': ('dB', 1.0),
}
# Reply unit -> (dimension, factor from the reply's unit to the SI base unit).
_REPLY_UNIT_SI: dict[str, tuple[str, float]] = {
    'Hz': ('Hz', 1.0),
    's': ('s', 1.0),
    'ns': ('s', 1e-9),
    'V': ('V', 1.0),
    'dB': ('dB', 1.0),
}

_NUMBER_WITH_UNIT = re.compile(r'^([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*([A-Za-z]*)$')
_KEYWORDS = frozenset({'MIN', 'MINIMUM', 'MAX', 'MAXIMUM', 'DEF', 'DEFAULT', 'UP', 'DOWN'})
_TRUE = frozenset({'ON', '1'})
_FALSE = frozenset({'OFF', '0'})

#: Headers whose argument is a boolean: only these compare `ON`/`OFF`/`1`/`0` by truth value.
BOOLEAN_HEADERS: frozenset[str] = frozenset(
    {'TRAN:ENAB', 'TRAN:REV', 'TRAN:DAMP', 'GAIN:PRE:COMB', 'GAIN:PRE:SPLIT', 'AVER:DEL:CONS:AUTO'}
)
#: Headers whose reply is an integer in its reply unit: compared with an absolute tolerance
#: of half a unit (a relative tolerance would accept a clamped 2147483647 NS as 2147483000).
INTEGER_REPLY_HEADERS: frozenset[str] = frozenset(
    {
        'FREQ',
        'TRAN:FREQ',
        'TRIG:DEL',
        'TRAN:GAP',
        'TRAN:DAMP:GAP',
        'DATA:LENG',
        'TRAN:PULS',
        'AVER:COUN',
        'FILT:HPAS:IND',
    }
)
_INTEGER_ABS_TOL = 0.5


def _to_float(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


def _numbers_close(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-12)


def _lists_match(sent: str, reply: str) -> bool:
    a = [x.strip() for x in sent.split(',')]
    b = [x.strip() for x in reply.split(',')]
    if len(a) != len(b):
        return False
    for x, y in zip(a, b, strict=True):
        fx, fy = _to_float(x), _to_float(y)
        if fx is not None and fy is not None:
            if not _numbers_close(fx, fy):
                return False
        elif x.upper() != y.upper():
            return False
    return True


def _enum_match(sent: str, reply: str) -> bool:
    up_sent, up_reply = sent.upper(), reply.upper()
    if up_sent == up_reply:
        return True
    # Short and long forms (INT vs INTERNAL) only for an alphabetic token of 3+ letters: a
    # number such as `200` is never a "short form" of `2000 OHM`.
    if not (sent.isalpha() and len(sent) >= 3):
        return False
    capitals = ''.join(c for c in sent if c.isupper())  # INTernal -> INT
    if len(capitals) >= 3 and up_reply == capitals:
        return True
    # INT vs INTERNAL in either direction (SPEC 3.1 names one direction, its own test list
    # needs the other: sent INTERNAL, reply INT). The shorter word must look like a SCPI
    # short form (3 or 4 letters); LINEAR is not a short form of LINEARX.
    short, long_ = sorted((up_sent, up_reply), key=len)
    return 3 <= len(short) <= 4 and long_.startswith(short)


def values_match(header: str, sent: str, reply: str) -> bool:
    """Whether a read-back `reply` agrees with the `sent` argument of `header`.

    Handles booleans (only for `BOOLEAN_HEADERS`), enumerations (long/short/case), numbers
    with a unit suffix (converted to the reply unit of the header, see `REPLY_UNITS`) and
    comma lists. Headers in `INTEGER_REPLY_HEADERS` are compared with an absolute tolerance
    of 0.5 reply units, the others with a relative one of 1e-6. A bare number is compared as
    it is, in the reply unit, because the unit the device assumes for a bare number is
    UNKNOWN (PROTOCOL.md). An empty `sent` never matches. `MIN`/`MAX`/`DEF`/`UP`/`DOWN`
    cannot be verified and return True (`apply_setup` refuses them for `TRAN:PULS`).
    Raises ValueError for a header with an unknown node.
    """
    normal = normalize_header(header)
    sent, reply = sent.strip(), reply.strip()
    if not sent:
        return False
    if sent.upper() in _KEYWORDS:
        return True
    if ',' in sent or ',' in reply:
        return _lists_match(sent, reply)
    up_sent, up_reply = sent.upper(), reply.upper()
    if (
        normal in BOOLEAN_HEADERS
        and {up_sent, up_reply} & {'ON', 'OFF'}
        and up_sent in _TRUE | _FALSE
        and up_reply in _TRUE | _FALSE
    ):
        return (up_sent in _TRUE) == (up_reply in _TRUE)
    match = _NUMBER_WITH_UNIT.match(sent)
    reply_num = _to_float(reply)
    if match and reply_num is not None:
        value = float(match.group(1))
        suffix = match.group(2).upper()
        reply_unit = REPLY_UNITS.get(normal)
        integer = normal in INTEGER_REPLY_HEADERS
        if not suffix:
            if integer:
                return abs(value - reply_num) <= _INTEGER_ABS_TOL
            return _numbers_close(value, reply_num)
        if suffix in _UNIT_SUFFIXES:
            dimension, factor = _UNIT_SUFFIXES[suffix]
            if reply_unit is None:  # no base unit known: unit ignored
                if integer:
                    return abs(value - reply_num) <= _INTEGER_ABS_TOL
                return _numbers_close(value, reply_num)
            reply_dimension, reply_factor = _REPLY_UNIT_SI[reply_unit]
            if dimension != reply_dimension:
                return False
            if integer:
                return abs(value * factor / reply_factor - reply_num) <= _INTEGER_ABS_TOL
            return _numbers_close(value * factor, reply_num * reply_factor)
    return _enum_match(sent, reply)


# ── state snapshot (SPEC 3.2) ────────────────────────────────────────────────

#: Headers captured by `get_state` and written back, in this order, by `set_state`.
#: Short forms as in the vendor example. If the device's real short forms differ, that is a
#: hardware finding; keep the list in this one place. The TGC curves come before
#: `GAIN:TGC:MODE` so that the mode is switched on only after its curve is in place.
STATE_HEADERS: tuple[str, ...] = (
    'FREQ',
    'DATA:LENG',
    'MODE',
    'TRAN:TYPE',
    'TRAN:REV',
    'TRAN:PULS',
    'TRAN:FREQ',
    'TRAN:DUR',
    'TRAN:GAP',
    'TRAN:DAMP',
    'TRAN:DAMP:GAP',
    'TRAN:IMP',
    'TRAN:ENAB',
    'TRIG:MODE',
    'TRIG:INT',
    'TRIG:DEL',
    'GAIN',
    'GAIN:PRE:COMB',
    'GAIN:PRE:SPLIT',
    'GAIN:TGC:LIN',
    'GAIN:TGC:ARB',
    'GAIN:TGC:MODE',
    'AVER:COUN',
    'AVER:DEL:CONS:AUTO',
    'AVER:DEL:CONS',
    'AVER:DEL:RAND',
    'FILT:HPAS:IND',
)

# Query replies carry bare numbers in the reply unit, but the unit a bare number has in a
# command is UNKNOWN. set_state therefore writes the unit forms the vendor example uses.
# header -> (argument unit suffix, size of that unit in the reply unit)
_RESTORE_UNITS: dict[str, tuple[str, float]] = {
    'FREQ': ('MHZ', 1e6),
    'TRAN:FREQ': ('KHZ', 1e3),
    'TRIG:INT': ('US', 1e-6),
    'AVER:DEL:CONS': ('NS', 1e-9),
    'AVER:DEL:RAND': ('NS', 1e-9),
    'TRIG:DEL': ('NS', 1.0),
    'TRAN:GAP': ('NS', 1.0),
    'TRAN:DAMP:GAP': ('NS', 1.0),
    'TRAN:PULS': ('V', 1.0),
}


def _restore_value(header: str, reply: str) -> str:
    """The argument text that writes back the query `reply` of `header`."""
    spec = _RESTORE_UNITS.get(normalize_header(header))
    number = _to_float(reply)
    if spec is None or number is None:
        return reply
    suffix, size = spec
    return f'{format(number / size, ".10g")} {suffix}'


def _format_value(value: object) -> str:
    """Text of an `apply_setup` value."""
    if isinstance(value, bool):
        return 'ON' if value else 'OFF'
    if isinstance(value, float):
        return format(value, '.10g')
    if isinstance(value, (list, tuple)):
        return ','.join(_format_value(v) for v in value)
    return str(value)


# ── data classes ─────────────────────────────────────────────────────────────


class Identity(NamedTuple):
    """The four fields of `*IDN?`."""

    manufacturer: str
    model: str
    serial: str
    firmware: str


class AScan(NamedTuple):
    """One A-scan: raw int16 samples, the packet header and the time base.

    There is deliberately no voltage array: the count-to-volt scaling of the A1580 is
    UNKNOWN (PROTOCOL.md "Amplitude scale"), so only raw counts are stored. `t` is derived
    on demand from `fs_hz`. What time zero means (trigger, pulse, end of `TRIG:DEL`) is
    UNKNOWN too.
    """

    raw: np.ndarray  # int16, length DATA:LENG
    header: AScanHeader
    fs_hz: float  # FREQ? reply
    trigger_delay_ns: int  # TRIG:DEL? reply

    @property
    def t(self) -> np.ndarray:
        """Sample times in seconds, `arange(n) / fs_hz`, float64, recomputed on each access."""
        return np.arange(len(self.raw), dtype=np.float64) / self.fs_hz

    @classmethod
    def from_packet(cls, packet: bytes, fs_hz: float, trigger_delay_ns: int) -> AScan:
        header = parse_header(packet)
        body = len(packet) - HEADER_SIZE
        if body < 2 or body % 2:
            raise ValueError(f'packet of {len(packet)} bytes has no whole sample block')
        raw = np.frombuffer(packet, dtype='<i2', offset=HEADER_SIZE).astype(np.int16)
        return cls(raw, header, fs_hz, trigger_delay_ns)


# ── the plug ─────────────────────────────────────────────────────────────────

_ERROR_QUEUE_QUERY = 'SYSTem:ERRor?'
_MAX_ERROR_READS = 20
_STREAM_SLICE_S = 0.1  # the stream thread reads in slices so that stop_stream is prompt
_PULSER_OFF = 'TRAN:ENAB OFF'
_MIN_ACQUIRE_TIMEOUT_S = 5.0
# The input buffer is 256 bytes: a line of up to 255 bytes including CRLF is parsed, a longer
# one is discarded with -363 "Input buffer overrun" (measured 2026-10-05, fw 1.16: 255 ok,
# 256 and more discarded).
MAX_LINE_BYTES = 255


def _reply_float(command: str, reply: str) -> float:
    """`reply` as a finite float, else RuntimeError naming the command and the reply."""
    try:
        value = float(reply)
    except ValueError:
        raise RuntimeError(f'{command} reply {reply!r} is not a number') from None
    if not math.isfinite(value):
        raise RuntimeError(f'{command} reply {reply!r} is not a finite number')
    return value


def _reply_int(command: str, reply: str) -> int:
    """`reply` as an int; an integral float text such as `1024.0` is accepted."""
    value = _reply_float(command, reply)
    if not value.is_integer():
        raise RuntimeError(f'{command} reply {reply!r} is not an integer')
    return int(value)


class _Session(NamedTuple):
    """What `_begin_acquisition` set up: the reader, its socket and the time-base facts."""

    reader: FrameReader
    sock: Any
    length: int
    fs: float
    delay: int
    port: int


@dataclass(eq=False)
class _Stream:
    """Everything one `start_stream` call owns; the reader thread gets this object only."""

    reader: FrameReader
    socket: Any
    timeout_s: float
    stop_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    count: int = 0
    failure: Exception | None = None  # transport failure, stall or any reader-thread error
    callback_failure: Exception | None = None
    peer_closed: bool = False  # the device closed the data socket
    zombie: bool = False  # stop_stream could not join the thread in time

    def alive(self) -> bool:
        return self.thread is not None and self.thread.is_alive()


class A1580Plug(BasePlug):
    """OpenHTF plug for one A1580 over SCPI (TCP 5025) plus the A-scan data socket.

    Threading: the pyvisa resource is shared between the caller and, while a stream runs,
    the stream thread, whose callback may call `query` and `write` (for example to read a
    setting for every A-scan). `write` and `query` therefore hold `self._scpi_lock` (an
    `RLock`) for the duration of one command. Sequences such as write, drain, read-back
    are not atomic: do not run them from two threads at once.

    Pulser safety: `tearDown` sends one unchecked `TRAN:ENAB OFF` before anything else, and
    `set_state` never writes `TRAN:ENAB ON` (a snapshot taken with the pulser on is restored
    with the pulser off; the plug warns). The device ends with the snapshot restored and
    the pulser off.
    """

    auto_placeholder = True

    def __init__(
        self,
        resource: Any = None,
        data_socket_factory: Callable[[str, int], Any] | None = None,
        host: str | None = None,
        restore_state: bool | None = None,
    ) -> None:
        self._scpi_lock = threading.RLock()
        self._rm: Any = None
        self._owns_resource = False
        self._data_sock: Any = None
        self._stream: _Stream | None = None
        self._scpi_port = int(CONF.a1580_scpi_port)
        self.host = CONF.a1580_host if host is None else host
        self.identity = Identity('', '', '', '')
        self._initial_state: dict[str, str] | None = None
        self._restore_state = (
            bool(CONF.a1580_restore_state) if restore_state is None else restore_state
        )

        if resource is not None:
            self._resource = resource
            if data_socket_factory is None:
                data_socket_factory = getattr(resource, 'data_socket_factory', None)
        else:
            import pyvisa  # lazy: importing the package must not need pyvisa

            self._rm = pyvisa.ResourceManager('@py')
            try:
                self._resource = self._rm.open_resource(
                    f'TCPIP::{self.host}::{self._scpi_port}::SOCKET'
                )
            except Exception:
                self._rm.close()
                self._rm = None
                raise
            self._owns_resource = True
        self._data_socket_factory = data_socket_factory or _default_data_socket
        try:
            self._resource.encoding = 'iso-8859-1'
            self._resource.timeout = 5000
            self._resource.read_termination = '\r\n'
            self._resource.write_termination = '\r\n'
            self.check_errors()  # discard whatever an earlier session left in the queue
            self.identity = self.idn()
            if self._restore_state:
                self._initial_state = self.get_state()
                if _is_on(self._initial_state.get('TRAN:ENAB', '')):
                    self.logger.warning(
                        'pulser was already enabled when the plug was created; '
                        'it will be switched off in tearDown'
                    )
        except Exception:
            if self._owns_resource:
                self._close_resource()
            raise

    # ── low level ────────────────────────────────────────────────────────────

    @staticmethod
    def _check_line_length(cmd: str) -> None:
        """Raise ValueError, before anything is sent, if `cmd` plus CRLF exceeds the limit."""
        size = len(cmd.encode('iso-8859-1', errors='replace')) + 2
        if size > MAX_LINE_BYTES:
            header = cmd.split(None, 1)[0] if cmd.strip() else ''
            raise ValueError(
                f'{header}: line is {size} bytes including CRLF, the limit is {MAX_LINE_BYTES}; '
                'the A1580 discards lines over 255 bytes including CRLF with -363; '
                'measured 2026-10-05, fw 1.16'
            )

    def write(self, cmd: str) -> None:
        self._check_line_length(cmd)
        with self._scpi_lock:
            self._resource.write(cmd)

    def query(self, cmd: str) -> str:
        self._check_line_length(cmd)
        with self._scpi_lock:
            return str(self._resource.query(cmd)).strip()

    def idn(self) -> Identity:
        reply = self.query('*IDN?')
        fields = [f.strip() for f in reply.split(',')]
        if len(fields) != 4:
            raise RuntimeError(f'*IDN? reply {reply!r} does not have 4 comma-separated fields')
        return Identity(*fields)

    def stop(self) -> None:
        self.write('STOP')

    def _discard_late_reply(self) -> None:
        """After a failed read, drop a reply that may still arrive (best effort, never raises).

        A query that timed out can be answered late; that reply would then be taken for the
        answer to the next query. Uses `flush` (pyvisa: discard the read buffer) or, failing
        that, `clear`, whichever the resource has.
        """
        with self._scpi_lock:
            for name in ('flush', 'clear'):
                method = getattr(self._resource, name, None)
                if method is None:
                    continue
                try:
                    if name == 'flush' and self._owns_resource:
                        from pyvisa.constants import BufferOperation  # lazy

                        method(BufferOperation.discard_read_buffer)
                    else:
                        method()
                except Exception as exc:  # noqa: BLE001 - best effort only
                    self.logger.debug('%s() after a failed read did not work: %s', name, exc)
                    continue
                return

    # ── errors, setup, state ─────────────────────────────────────────────────

    def check_errors(self) -> list[str]:
        """Drain the error queue (at most 20 reads); return the entries that are not 0."""
        errors: list[str] = []
        for _ in range(_MAX_ERROR_READS):
            reply = self.query(_ERROR_QUEUE_QUERY)
            number = reply.split(',', 1)[0].strip()
            try:
                code = int(number)
            except ValueError:
                raise RuntimeError(
                    f'{_ERROR_QUEUE_QUERY} reply {reply!r} has no error number'
                ) from None
            if code == 0:
                break
            errors.append(reply)
        return errors

    def write_checked(self, cmd: str) -> None:
        self.write(cmd)
        errors = self.check_errors()
        if errors:
            raise RuntimeError(f'{cmd!r}: {errors}')

    def reset(self) -> None:
        """`*RST`, then drain the error queue. What `*RST` resets is UNKNOWN (PROTOCOL.md)."""
        self.write('*RST')
        self.check_errors()

    def apply_setup(self, settings: Mapping[str, object]) -> None:
        """Write each `header: value`, read it back and compare; raise one RuntimeError.

        Every write is followed by an error-queue drain and a `<header>?` query. All
        failures (unknown header node, device error, no answer, any exception, read-back
        mismatch) are collected and reported together; settings after a failed one are still
        applied. After an exception from the resource the late reply is discarded
        (`flush`/`clear`, if the resource has them).

        Raises ValueError, before anything is written, for a `MIN`/`MAX`/`DEF`/`UP`/`DOWN`
        argument on `TRAN:PULS`: the pulser voltage is never set by keyword.
        """
        items = [(key, _format_value(value)) for key, value in settings.items()]
        for key, sent in items:
            if sent.strip().upper() in _KEYWORDS and _normal_or_none(key) == 'TRAN:PULS':
                raise ValueError(
                    f'{key} {sent}: refuse to set the pulser voltage by keyword; '
                    'give a number with a unit, for example 20 V'
                )
        failures: list[str] = []
        for key, sent in items:
            try:
                failure = self._apply_one(key, sent)
            except Exception as exc:  # noqa: BLE001 - one bad setting must not stop the others
                if not isinstance(exc, ValueError):  # a ValueError means nothing was sent
                    self._discard_late_reply()
                failure = f'{key}: sent={sent!r} raised {type(exc).__name__}: {exc}'
            if failure is not None:
                failures.append(failure)
        if failures:
            raise RuntimeError('apply_setup failed: ' + '; '.join(failures))

    def _apply_one(self, key: str, sent: str) -> str | None:
        """Apply one setting; return the failure text or None. May raise."""
        try:
            normalize_header(key)
        except ValueError as exc:  # an unknown node: nothing is written, nothing to flush
            return f'{key}: sent={sent!r} nothing written: {exc}'
        self.write(f'{key} {sent}')
        errors = self.check_errors()
        reply: str | None
        try:
            reply = self.query(f'{key}?')
        except Exception as exc:  # noqa: BLE001 - an unknown header is never answered
            reply = None
            self._discard_late_reply()
            errors += [f'no answer to {key}?: {type(exc).__name__}'] + self.check_errors()
        if reply is not None and sent.strip().upper() in _KEYWORDS:
            self.logger.warning('%s %s cannot be verified by read-back (%r)', key, sent, reply)
        if errors or reply is None or not values_match(key, sent, reply):
            return f'{key}: sent={sent!r} readback={reply!r} errors={errors}'
        return None

    def _warn_dead_link(self, errors: int, skipped: int) -> None:
        self.logger.warning(
            'link appears dead after %d consecutive transport errors; '
            'skipping the remaining %d headers',
            errors,
            skipped,
        )

    def get_state(self) -> dict[str, str]:
        """Query every header of `STATE_HEADERS`; return `{header: reply}`.

        A header that raises (typically a timeout because this firmware does not know it)
        is logged, its error-queue entry drained, and left out of the snapshot; the
        constructor therefore survives a header that is wrong on a first hardware session.
        After `_DEAD_LINK_LIMIT` consecutive transport-type failures (anything but a
        `RuntimeError`) the link is taken as dead: a warning is logged and the remaining
        headers are skipped.
        """
        state: dict[str, str] = {}
        transport_errors = 0
        for index, header in enumerate(STATE_HEADERS):
            try:
                state[header] = self.query(f'{header}?')
                transport_errors = 0
            except Exception as exc:  # noqa: BLE001 - one unknown header must not abort the snapshot
                transport_errors = 0 if isinstance(exc, RuntimeError) else transport_errors + 1
                self.logger.warning(
                    'get_state: %s? failed (%s: %s); left out of the snapshot',
                    header,
                    type(exc).__name__,
                    exc,
                )
                self._discard_late_reply()
                try:
                    self.check_errors()  # drain the -113 the failed query queued
                except Exception as drain_exc:  # noqa: BLE001 - best effort
                    self.logger.warning(
                        'get_state: draining the error queue after %s? failed: %s',
                        header,
                        drain_exc,
                    )
                if transport_errors >= _DEAD_LINK_LIMIT:
                    self._warn_dead_link(transport_errors, len(STATE_HEADERS) - index - 1)
                    break
        return state

    def set_state(self, state: Mapping[str, str]) -> None:
        """Write a `get_state` snapshot back; the pulser is off at the end, always.

        Order: `TRAN:ENAB OFF` (unchecked, before anything else), a drain of the error
        queue (stale entries and the answer to that write are discarded; if the drain
        itself raises it is logged and the restore goes on), then every other header of
        `STATE_HEADERS` in order with `write_checked`, then `TRAN:ENAB OFF` again, checked.
        `TRAN:ENAB ON` is never written from a snapshot: if the snapshot had the pulser on,
        a warning says so and the pulser stays off.

        Headers whose snapshot value is empty are skipped. `AVER:DEL:CONS` is skipped
        unless `AVER:DEL:CONS:AUTO` is OFF (the device answers -221 otherwise). A failed
        header does not stop the restore; after the final OFF one RuntimeError lists all
        failures. After `_DEAD_LINK_LIMIT` consecutive transport-type failures (anything but
        a `RuntimeError`) the remaining headers are skipped with a warning; the final OFF is
        still attempted.
        """
        auto = state.get('AVER:DEL:CONS:AUTO', '').strip().upper()
        try:
            self.write(_PULSER_OFF)
        except Exception as exc:
            raise RuntimeError(
                f'set_state: {_PULSER_OFF!r} could not be sent, nothing restored: {exc}'
            ) from exc
        try:
            self.check_errors()
        except Exception as exc:  # noqa: BLE001 - the restore must go on
            self.logger.warning('set_state: draining the error queue failed: %s', exc)
        if _is_on(state.get('TRAN:ENAB', '')):
            self.logger.warning(
                'set_state: the snapshot had the pulser ON; it is NOT restored, '
                'the pulser is left off'
            )
        failures: list[str] = []
        todo = [
            header
            for header in STATE_HEADERS
            if header != 'TRAN:ENAB'
            and header in state
            and state[header].strip()
            and not (header == 'AVER:DEL:CONS' and auto not in ('OFF', '0'))
        ]
        transport_errors = 0
        for index, header in enumerate(todo):
            try:
                self.write_checked(f'{header} {_restore_value(header, state[header])}')
                transport_errors = 0
            except Exception as exc:  # noqa: BLE001 - restore the rest, report at the end
                failures.append(f'{header}: {exc}')
                if isinstance(exc, (RuntimeError, ValueError)):  # not a transport error
                    transport_errors = 0
                else:
                    transport_errors += 1
                    self._discard_late_reply()
                if transport_errors >= _DEAD_LINK_LIMIT:
                    self._warn_dead_link(transport_errors, len(todo) - index - 1)
                    break
        try:
            self.write_checked(_PULSER_OFF)
        except Exception as exc:  # noqa: BLE001 - report with the others
            failures.append(f'{_PULSER_OFF}: {exc}')
        if failures:
            raise RuntimeError('set_state failed: ' + '; '.join(failures))

    def apply_capture(
        self, capture_or_path: Capture | str | Path, *, reset: bool = False
    ) -> Capture:
        """Apply a capture file (or a `Capture`) with read-back verification; return the Capture.

        Thin delegate to `a1580_openhtf.capture.apply_capture`: the capture is loaded and
        validated before anything is sent, `reset=True` sends `*RST` first, then
        `apply_setup(capture.to_scpi())` runs. Raises `CaptureError` for an invalid capture.
        """
        from .capture import apply_capture  # lazy: keeps yaml out of the plug's import

        return apply_capture(self, capture_or_path, reset=reset)

    # ── acquisition ──────────────────────────────────────────────────────────

    def _begin_acquisition(self) -> _Session:
        """Steps 1 to 3 of SPEC 3.3: query the geometry, open the data socket, `STAR AUTO`."""
        length = _reply_int('DATA:LENG?', self.query('DATA:LENG?'))
        fs = _reply_float('FREQ?', self.query('FREQ?'))
        delay_reply = self.query('TRIG:DEL?')
        delay_value = _reply_float('TRIG:DEL?', delay_reply)
        delay = int(round(delay_value))
        if not delay_value.is_integer():
            self.logger.warning(
                'TRIG:DEL? reply %r is not an integer; rounded to %d. The unit may not be '
                'nanoseconds (PROTOCOL.md CONFLICT 4)',
                delay_reply,
                delay,
            )
        port = _reply_int('DATA:PORT?', self.query('DATA:PORT?'))
        if port == self._scpi_port:
            raise RuntimeError(
                f'DATA:PORT? returned the SCPI port {port}; refusing to open the data '
                'socket on it; see PROTOCOL.md CONFLICT 1'
            )
        sock = self._data_socket_factory(self.host, port)
        self._data_sock = sock
        try:
            reader = FrameReader(sock, length)
            self.write('STAR AUTO')
        except BaseException:
            self._end_acquisition()
            raise
        return _Session(reader, sock, length, fs, delay, port)

    def _end_acquisition(
        self, thread: threading.Thread | None = None, join_timeout_s: float = 0.0
    ) -> tuple[list[str], list[str]]:
        """`STOP`, join `thread` if given, close the data socket, drain the error queue.

        Never raises. Returns `(failures, device_errors)`: what went wrong in the cleanup
        itself (a failed `STOP`, an error queue that could not be read) and the entries the
        instrument had queued.
        """
        failures: list[str] = []
        device_errors: list[str] = []
        try:
            self.stop()
        except Exception as exc:  # noqa: BLE001 - cleanup must run to the end
            failures.append(f'STOP failed: {exc}')
        if thread is not None:
            thread.join(join_timeout_s)
        self._close_data_socket()
        try:
            device_errors += self.check_errors()
        except Exception as exc:  # noqa: BLE001 - cleanup must run to the end
            failures.append(f'{_ERROR_QUEUE_QUERY} failed: {exc}')
        return failures, device_errors

    def _close_data_socket(self) -> None:
        sock, self._data_sock = self._data_sock, None
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except (OSError, AttributeError):
            pass
        try:
            sock.close()
        except OSError:
            pass

    def _log_frame_stats(self, where: str, reader: FrameReader) -> None:
        stats = reader.stats
        self.logger.info('%s: frame stats %s', where, stats)
        if stats.dropped_bytes > 0 or stats.misaligned > 0:
            self.logger.warning(
                '%s: framing problems (dropped_bytes=%d, misaligned=%d); DATA:LENG may not '
                'match the stream: %s',
                where,
                stats.dropped_bytes,
                stats.misaligned,
                stats,
            )

    def _default_acquire_timeout(self, n: int) -> float:
        """`max(5, 2 * n * TRIG:INT + 2)` seconds: n acquisitions at the trigger interval."""
        try:
            interval = _reply_float('TRIG:INT?', self.query('TRIG:INT?'))
        except Exception as exc:  # noqa: BLE001 - fall back to the minimum
            self.logger.warning(
                'acquire: TRIG:INT? failed (%s); using %s s as the timeout',
                exc,
                _MIN_ACQUIRE_TIMEOUT_S,
            )
            self._discard_late_reply()
            return _MIN_ACQUIRE_TIMEOUT_S
        return max(_MIN_ACQUIRE_TIMEOUT_S, 2.0 * n * interval + 2.0)

    def _refuse_while_stream(self, running: str) -> None:
        """Raise RuntimeError if a stream exists (running, ended or zombie)."""
        stream = self._stream
        if stream is None:
            return
        if stream.alive():
            if stream.zombie:
                raise RuntimeError('previous stream thread still alive')
            raise RuntimeError(running)
        if stream.zombie:  # it finished meanwhile; nobody is left to report to
            self.logger.warning('the earlier stream thread has finished; clearing it')
            self._stream = None
            return
        if stream.peer_closed:
            reason = 'the device closed the data socket'
        elif stream.callback_failure is not None:
            reason = 'the callback raised'
        elif stream.failure is not None:
            reason = f'{type(stream.failure).__name__}: {stream.failure}'
        else:
            reason = 'it stopped'
        raise RuntimeError(f'the stream has ended ({reason}); call stop_stream() first')

    def acquire(self, n: int = 1, *, timeout_s: float | None = None) -> list[AScan]:
        """Start the acquisition, collect `n` A-scans, stop. See SPEC 3.3.

        `timeout_s=None` means `max(5, 2 * n * TRIG:INT + 2)` seconds, from a `TRIG:INT?`
        query. Raises ValueError for bad arguments, RuntimeError while a stream exists, for
        a `DATA:PORT?` that names the SCPI port and for a cleanup that failed (`STOP`, error
        queue unreadable), TimeoutError or ConnectionError (with DATA:LENG and port) when
        fewer than `n` packets arrive. Instrument errors queued during the acquisition are
        logged as a warning; the packets are still returned.
        """
        if n < 1:
            raise ValueError(f'n must be >= 1, got {n}')
        if timeout_s is not None and timeout_s <= 0:
            raise ValueError(f'timeout_s must be > 0, got {timeout_s}')
        self._refuse_while_stream('acquire() while a stream is running; call stop_stream() first')
        if timeout_s is None:
            timeout_s = self._default_acquire_timeout(n)
        session = self._begin_acquisition()
        reader = session.reader
        suffix = f'(DATA:LENG={session.length}, port={session.port})'
        failures: list[str] = []
        try:
            packets = reader.read(n, timeout_s)
        except TimeoutError as exc:
            raise TimeoutError(f'{exc} {suffix}') from exc
        except ConnectionError as exc:
            raise ConnectionError(f'{exc} {suffix}') from exc
        finally:
            failures, device_errors = self._end_acquisition()
            self._log_frame_stats('acquire', reader)
            if failures or device_errors:
                self.logger.warning(
                    'acquire cleanup: failures=%s instrument errors=%s', failures, device_errors
                )
        if failures:
            raise RuntimeError(f'acquire: {failures}')
        return [AScan.from_packet(p, session.fs, session.delay) for p in packets]

    def start_stream(self, callback: Callable[[AScan], object], *, timeout_s: float = 5.0) -> None:
        """Start the acquisition and call `callback(ascan)` from a daemon thread. SPEC 3.4.

        `timeout_s` is how long the stream may stay silent before it counts as stalled.
        The callback may use `query`/`write` (they hold the SCPI lock per command).
        """
        if timeout_s <= 0:
            raise ValueError(f'timeout_s must be > 0, got {timeout_s}')
        self._refuse_while_stream('a stream is already running; call stop_stream() first')
        session = self._begin_acquisition()
        stream = _Stream(reader=session.reader, socket=session.sock, timeout_s=timeout_s)
        thread = threading.Thread(
            target=self._stream_loop,
            args=(stream, session.fs, session.delay, callback),
            name='a1580-stream',
            daemon=True,
        )
        stream.thread = thread
        self._stream = stream
        thread.start()

    def _stream_loop(
        self, stream: _Stream, fs: float, delay: int, callback: Callable[[AScan], object]
    ) -> None:
        last = time.monotonic()
        try:
            while not stream.stop_event.is_set():
                try:
                    packets = stream.reader.read(1, _STREAM_SLICE_S)
                except TimeoutError:
                    if stream.stop_event.is_set():
                        return
                    if time.monotonic() - last > stream.timeout_s:
                        stream.failure = TimeoutError(
                            f'no A-scan for {stream.timeout_s} s after {stream.count} delivered'
                        )
                        return
                    continue
                last = time.monotonic()
                ascan = AScan.from_packet(packets[0], fs, delay)
                try:
                    callback(ascan)
                except Exception as exc:  # noqa: BLE001 - re-raised by stop_stream
                    self.logger.exception('A-scan callback raised; stopping the stream')
                    stream.callback_failure = exc
                    return
                stream.count += 1
        except Exception as exc:  # noqa: BLE001 - whatever it is, stop_stream reports it
            if not stream.stop_event.is_set():
                stream.failure = exc
                if isinstance(exc, ConnectionError):
                    stream.peer_closed = True
                    self.logger.warning('the device closed the data socket: %s', exc)

    def stop_stream(self) -> int:
        """Stop the stream and return the number of A-scans delivered (0 if none running).

        Sends `STOP`, joins the thread, closes the data socket and drains the error queue.
        Afterwards it re-raises, in this order: the callback's exception (the other
        problems are logged first), a stream failure (stall, lost connection, any error in
        the reader thread), a thread that did not stop in time, a failed `STOP`, queued
        instrument errors. A thread that cannot be joined leaves the stream marked as a
        zombie: `start_stream` and `acquire` then raise until the thread has finished.
        """
        stream = self._stream
        if stream is None:
            return 0
        if stream.zombie and stream.alive():
            raise RuntimeError('previous stream thread still alive')
        stream.stop_event.set()
        failures, device_errors = self._end_acquisition(stream.thread, stream.timeout_s + 1)
        problems = failures + device_errors
        alive = stream.alive()
        if alive:
            stream.zombie = True  # keep self._stream: the thread is still running
        else:
            self._stream = None
        self._log_frame_stats('stop_stream', stream.reader)
        count = stream.count
        callback_failure, stream.callback_failure = stream.callback_failure, None
        stream_failure, stream.failure = stream.failure, None
        others: list[str] = []
        if stream_failure is not None:
            others.append(f'stream failure: {type(stream_failure).__name__}: {stream_failure}')
        if alive:
            others.append('the stream thread did not stop in time')
        others += problems
        if callback_failure is not None:
            if others:
                self.logger.warning('stop_stream: besides the callback failure: %s', others)
            raise callback_failure
        if stream_failure is not None:
            if len(others) > 1:
                self.logger.warning('stop_stream: besides the stream failure: %s', others[1:])
            raise stream_failure
        if alive:
            raise RuntimeError('the stream thread did not stop in time')
        if problems:
            raise RuntimeError(f'stop_stream: {problems}')
        return count

    # ── lifecycle ────────────────────────────────────────────────────────────

    def tearDown(self) -> None:
        """Make the pulser safe first, then stop, optionally restore the settings, close all.

        Order: one unchecked best-effort `TRAN:ENAB OFF` (before the stream is stopped,
        before `STOP`, before any error-queue read); stop a running stream; `STOP`; then
        either restore the initial settings (`restore_state` on: `set_state` switches the
        pulser off again around the restore and ends with it off) or, with `restore_state`
        off, a checked `TRAN:ENAB OFF`. The device ends with the snapshot restored and the
        pulser off. Every failure is logged, nothing is raised.
        """
        try:
            self.write(_PULSER_OFF)
        except Exception as exc:  # noqa: BLE001 - tearDown must not raise
            self.logger.warning('Failed to switch the pulser off first in tearDown: %s', exc)
        try:
            self.stop_stream()
        except Exception as exc:  # noqa: BLE001 - tearDown must not raise
            self.logger.warning('stop_stream failed in tearDown: %s', exc)
        try:
            self.stop()
        except Exception as exc:  # noqa: BLE001 - tearDown must not raise
            self.logger.warning('STOP failed in tearDown: %s', exc)
        if self._restore_state and self._initial_state is not None:
            try:
                self.set_state(self._initial_state)
            except Exception as exc:  # noqa: BLE001 - must not block closing the resource
                self.logger.warning('Failed to restore A1580 state: %s', exc)
        elif not self._restore_state:
            try:
                self.write_checked(_PULSER_OFF)
            except Exception as exc:  # noqa: BLE001 - best effort, must not block closing
                self.logger.warning('Failed to switch the pulser off in tearDown: %s', exc)
        self._close_data_socket()
        self._close_resource()

    def _close_resource(self) -> None:
        try:
            self._resource.close()
        except Exception as exc:  # noqa: BLE001 - tearDown must not raise
            self.logger.warning('closing the SCPI resource failed: %s', exc)
        if self._rm is not None:
            try:
                self._rm.close()
            except Exception as exc:  # noqa: BLE001 - tearDown must not raise
                self.logger.warning('closing the resource manager failed: %s', exc)
            self._rm = None


def _is_on(text: str) -> bool:
    return text.strip().upper() in _TRUE


def _normal_or_none(header: str) -> str | None:
    try:
        return normalize_header(header)
    except ValueError:
        return None


def _default_data_socket(host: str, port: int) -> socket.socket:
    return socket.create_connection((host, port), timeout=5)
