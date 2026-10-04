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
_SHORT_ALIASES = {'MOD': 'MODE', 'SPL': 'SPLIT'}  # vendor writes MODe but also uses MODE/SPLIT
_VOWELS = 'AEIOU'


def _short_mnemonic(node: str) -> str:
    """Short form of one mnemonic: `TRANsmitter` -> `TRAN`, `FREQUENCY` -> `FREQ`."""
    lead = re.match(r'[A-Z0-9]+', node)
    if lead and len(lead.group()) >= 3 and node != node.upper():
        short = lead.group()  # mixed case: the capitals are the short form
    else:
        short = node.upper()
        if len(short) > 4:  # SCPI: first four letters, three if the fourth is a vowel
            short = short[:3] if short[3] in _VOWELS else short[:4]
    return _SHORT_ALIASES.get(short, short)


def normalize_header(header: str) -> str:
    """Canonical key of a SCPI header, accepting long and short forms.

    Upper-cases, drops a trailing `?`, a leading `:` and a leading `SOUR:`/`SENS:` root,
    reduces each mnemonic to its short form and drops the optional last nodes `:ENAB` of
    `TRAN:DAMP`, `:LEV`, `:VAL` and `:NEXT`. `TRANsmitter:PULSe:LEVel` -> `TRAN:PULS`.
    """
    text = header.strip().removesuffix('?').lstrip(':')
    nodes = [_short_mnemonic(n) for n in text.split(':') if n]
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
    capitals = ''.join(c for c in sent if c.isupper())  # INTernal -> INT
    if len(capitals) >= 3 and up_reply == capitals:
        return True
    short, long_ = sorted((up_sent, up_reply), key=len)
    # INT vs INTERNAL in either direction (SPEC 3.1 names one direction, its own test list
    # needs the other: sent INTERNAL, reply INT).
    return len(short) >= 3 and long_.startswith(short)


def values_match(header: str, sent: str, reply: str) -> bool:
    """Whether a read-back `reply` agrees with the `sent` argument of `header`.

    Handles booleans, enumerations (long/short/case), numbers with a unit suffix
    (converted to the reply unit of the header, see `REPLY_UNITS`) and comma lists.
    A bare number is compared as it is, in the reply unit, because the unit the device
    assumes for a bare number is UNKNOWN (PROTOCOL.md). `MIN`/`MAX`/`DEF`/`UP`/`DOWN` cannot
    be verified and return True.
    """
    sent, reply = sent.strip(), reply.strip()
    if sent.upper() in _KEYWORDS:
        return True
    if ',' in sent or ',' in reply:
        return _lists_match(sent, reply)
    up_sent, up_reply = sent.upper(), reply.upper()
    if (
        {up_sent, up_reply} & {'ON', 'OFF'}
        and up_sent in _TRUE | _FALSE
        and up_reply in _TRUE | _FALSE
    ):
        return (up_sent in _TRUE) == (up_reply in _TRUE)
    match = _NUMBER_WITH_UNIT.match(sent)
    reply_num = _to_float(reply)
    if match and reply_num is not None:
        value = float(match.group(1))
        suffix = match.group(2).upper()
        reply_unit = REPLY_UNITS.get(normalize_header(header))
        if not suffix:
            return _numbers_close(value, reply_num)
        if suffix in _UNIT_SUFFIXES:
            dimension, factor = _UNIT_SUFFIXES[suffix]
            if reply_unit is None:
                return _numbers_close(value, reply_num)  # no base unit known: unit ignored
            reply_dimension, reply_factor = _REPLY_UNIT_SI[reply_unit]
            if dimension != reply_dimension:
                return False
            return _numbers_close(value * factor, reply_num * reply_factor)
    return _enum_match(sent, reply)


# ── state snapshot (SPEC 3.2) ────────────────────────────────────────────────

#: Headers captured by `get_state` and written back, in this order, by `set_state`.
#: Short forms as in the vendor example. If the device's real short forms differ, that is a
#: hardware finding; keep the list in this one place.
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
    'GAIN:TGC:MODE',
    'GAIN:TGC:LIN',
    'GAIN:TGC:ARB',
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


class A1580Plug(BasePlug):
    """OpenHTF plug for one A1580 over SCPI (TCP 5025) plus the A-scan data socket."""

    auto_placeholder = True

    def __init__(
        self,
        resource: Any = None,
        data_socket_factory: Callable[[str, int], Any] | None = None,
        host: str | None = None,
        restore_state: bool | None = None,
    ) -> None:
        self._rm: Any = None
        self._owns_resource = False
        self._data_sock: Any = None
        self._stream_thread: threading.Thread | None = None
        self._stream_stop = threading.Event()
        self._stream_count = 0
        self._stream_failure: Exception | None = None  # transport failure or stall
        self._callback_failure: Exception | None = None
        self._stream_timeout_s = 5.0
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
                    f'TCPIP::{self.host}::{CONF.a1580_scpi_port}::SOCKET'
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
        except Exception:
            if self._owns_resource:
                self._close_resource()
            raise

    # ── low level ────────────────────────────────────────────────────────────

    def write(self, cmd: str) -> None:
        self._resource.write(cmd)

    def query(self, cmd: str) -> str:
        return str(self._resource.query(cmd)).strip()

    def idn(self) -> Identity:
        reply = self.query('*IDN?')
        fields = [f.strip() for f in reply.split(',')]
        if len(fields) != 4:
            raise RuntimeError(f'*IDN? reply {reply!r} does not have 4 comma-separated fields')
        return Identity(*fields)

    def stop(self) -> None:
        self.write('STOP')

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
        failures (device error, no answer, read-back mismatch) are collected and reported
        together; settings after a failed one are still applied.
        """
        failures: list[str] = []
        for key, value in settings.items():
            sent = _format_value(value)
            self.write(f'{key} {sent}')
            errors = self.check_errors()
            reply: str | None
            try:
                reply = self.query(f'{key}?')
            except Exception as exc:  # noqa: BLE001 - an unknown header is never answered
                reply = None
                errors += [f'no answer to {key}?: {type(exc).__name__}'] + self.check_errors()
            if reply is not None and sent.strip().upper() in _KEYWORDS:
                self.logger.warning('%s %s cannot be verified by read-back (%r)', key, sent, reply)
            if errors or reply is None or not values_match(key, sent, reply):
                failures.append(f'{key}: sent={sent!r} readback={reply!r} errors={errors}')
        if failures:
            raise RuntimeError('apply_setup failed: ' + '; '.join(failures))

    def get_state(self) -> dict[str, str]:
        """Query every header of `STATE_HEADERS`; return `{header: reply}`."""
        return {header: self.query(f'{header}?') for header in STATE_HEADERS}

    def set_state(self, state: Mapping[str, str]) -> None:
        """Write a `get_state` snapshot back, each write with write_checked.

        The pulser is switched off first (`TRAN:ENAB OFF`, before any other restore write)
        so that it is off while amplitude, frequency and impedance change. Then come the
        other headers in `STATE_HEADERS` order and, last, the snapshot's `TRAN:ENAB` value
        (nothing is written for it if the snapshot has none, so the pulser stays off).

        `AVER:DEL:CONS` is skipped unless `AVER:DEL:CONS:AUTO` is OFF (the device answers
        -221 otherwise). Errors queued before the call are discarded first.
        """
        self.check_errors()
        auto = state.get('AVER:DEL:CONS:AUTO', '').strip().upper()
        self.write_checked('TRAN:ENAB OFF')
        for header in STATE_HEADERS:
            if header == 'TRAN:ENAB' or header not in state:
                continue
            if header == 'AVER:DEL:CONS' and auto not in ('OFF', '0'):
                continue
            self.write_checked(f'{header} {_restore_value(header, state[header])}')
        if 'TRAN:ENAB' in state:
            self.write_checked(f'TRAN:ENAB {_restore_value("TRAN:ENAB", state["TRAN:ENAB"])}')

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

    def _begin_acquisition(self) -> tuple[FrameReader, int, float, int, int]:
        """Steps 1 to 3 of SPEC 3.3: query the geometry, open the data socket, `STAR AUTO`."""
        length = int(self.query('DATA:LENG?'))
        fs = float(self.query('FREQ?'))
        delay = int(float(self.query('TRIG:DEL?')))
        port = int(self.query('DATA:PORT?'))
        sock = self._data_socket_factory(self.host, port)
        self._data_sock = sock
        try:
            reader = FrameReader(sock, length)
            self.write('STAR AUTO')
        except BaseException:
            self._end_acquisition()
            raise
        return reader, length, fs, delay, port

    def _end_acquisition(
        self, thread: threading.Thread | None = None, join_timeout_s: float = 0.0
    ) -> list[str]:
        """`STOP`, join `thread` if given, close the data socket, drain the error queue.

        Never raises; returns what went wrong (a failed `STOP`, queued instrument errors).
        """
        problems: list[str] = []
        try:
            self.stop()
        except Exception as exc:  # noqa: BLE001 - cleanup must run to the end
            problems.append(f'STOP failed: {exc}')
        if thread is not None:
            thread.join(join_timeout_s)
        self._close_data_socket()
        try:
            problems += self.check_errors()
        except Exception as exc:  # noqa: BLE001 - cleanup must run to the end
            problems.append(f'{_ERROR_QUEUE_QUERY} failed: {exc}')
        return problems

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

    def acquire(self, n: int = 1, *, timeout_s: float = 5.0) -> list[AScan]:
        """Start the acquisition, collect `n` A-scans, stop. See SPEC 3.3.

        Raises ValueError for bad arguments, RuntimeError while a stream is running or when
        the instrument queued errors, TimeoutError (with DATA:LENG and port) when fewer than
        `n` packets arrive within `timeout_s`.
        """
        if n < 1:
            raise ValueError(f'n must be >= 1, got {n}')
        if timeout_s <= 0:
            raise ValueError(f'timeout_s must be > 0, got {timeout_s}')
        if self._stream_thread is not None:
            raise RuntimeError('acquire() while a stream is running; call stop_stream() first')
        reader, length, fs, delay, port = self._begin_acquisition()
        try:
            packets = reader.read(n, timeout_s)
        except TimeoutError as exc:
            raise TimeoutError(f'{exc} (DATA:LENG={length}, port={port})') from exc
        finally:
            problems = self._end_acquisition()
            if problems:
                self.logger.warning('acquire cleanup: %s', problems)
        if problems:
            raise RuntimeError(f'acquire: {problems}')
        return [AScan.from_packet(p, fs, delay) for p in packets]

    def start_stream(self, callback: Callable[[AScan], object], *, timeout_s: float = 5.0) -> None:
        """Start the acquisition and call `callback(ascan)` from a daemon thread. SPEC 3.4.

        `timeout_s` is how long the stream may stay silent before it counts as stalled.
        """
        if timeout_s <= 0:
            raise ValueError(f'timeout_s must be > 0, got {timeout_s}')
        if self._stream_thread is not None:
            raise RuntimeError('a stream is already running; call stop_stream() first')
        reader, _length, fs, delay, _port = self._begin_acquisition()
        self._stream_stop = threading.Event()
        self._stream_count = 0
        self._stream_failure = None
        self._callback_failure = None
        self._stream_timeout_s = timeout_s
        self._stream_thread = threading.Thread(
            target=self._stream_loop,
            args=(reader, fs, delay, callback, timeout_s, self._stream_stop),
            name='a1580-stream',
            daemon=True,
        )
        self._stream_thread.start()

    def _stream_loop(
        self,
        reader: FrameReader,
        fs: float,
        delay: int,
        callback: Callable[[AScan], object],
        timeout_s: float,
        stop: threading.Event,
    ) -> None:
        last = time.monotonic()
        while not stop.is_set():
            try:
                packets = reader.read(1, _STREAM_SLICE_S)
            except TimeoutError:
                if stop.is_set():
                    return
                if time.monotonic() - last > timeout_s:
                    self._stream_failure = TimeoutError(
                        f'no A-scan for {timeout_s} s after {self._stream_count} delivered'
                    )
                    return
                continue
            except (OSError, ValueError) as exc:  # ConnectionError is an OSError
                if not stop.is_set():
                    self._stream_failure = exc
                return
            last = time.monotonic()
            try:
                callback(AScan.from_packet(packets[0], fs, delay))
            except Exception as exc:  # noqa: BLE001 - re-raised by stop_stream
                self.logger.exception('A-scan callback raised; stopping the stream')
                self._callback_failure = exc
                return
            self._stream_count += 1

    def stop_stream(self) -> int:
        """Stop the stream and return the number of A-scans delivered (0 if none running).

        Sends `STOP`, joins the thread, closes the data socket and drains the error queue.
        Afterwards it re-raises, in this order: the callback's exception, a stream failure
        (stall or lost connection), a failed `STOP`, queued instrument errors.
        """
        thread = self._stream_thread
        if thread is None:
            return 0
        self._stream_stop.set()
        problems = self._end_acquisition(thread, self._stream_timeout_s + 1)
        alive = thread.is_alive()
        self._stream_thread = None
        count = self._stream_count
        callback_failure, self._callback_failure = self._callback_failure, None
        stream_failure, self._stream_failure = self._stream_failure, None
        if callback_failure is not None:
            raise callback_failure
        if stream_failure is not None:
            raise stream_failure
        if alive:
            raise RuntimeError('the stream thread did not stop in time')
        if problems:
            raise RuntimeError(f'stop_stream: {problems}')
        return count

    # ── lifecycle ────────────────────────────────────────────────────────────

    def tearDown(self) -> None:
        """Stop, make the pulser safe, optionally restore the settings, close everything.

        Order: stop a running stream, `STOP`, then either restore the initial settings
        (`restore_state` on: `set_state` writes `TRAN:ENAB OFF` first and the snapshot's
        value last) or, when `restore_state` is off, write `TRAN:ENAB OFF` so that a station
        that does not restore never leaves the high-voltage pulser running. That write is
        best effort. Every failure is logged, nothing is raised.
        """
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
                self.write_checked('TRAN:ENAB OFF')
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


def _default_data_socket(host: str, port: int) -> socket.socket:
    return socket.create_connection((host, port), timeout=5)
