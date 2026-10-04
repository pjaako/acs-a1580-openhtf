# Adapted from rigol-dho-openhtf fake_resource.py (shape of the fake: log, error queue,
# reject map, generic settings store). The command set, reply formats and the data socket
# are specific to the A1580 and follow PROTOCOL.md; nothing in here was measured.
"""Fake A1580: a stand-in for the pyvisa SCPI resource and for the A-scan data socket.

A fake only knows what we told it. Every reply format below comes from the vendor
documents (`PROTOCOL.md`); where those leave the device's behaviour UNKNOWN the choice is
marked `UNKNOWN default` or `UNKNOWN`. Nothing is done until it has run on the instrument.
"""

from __future__ import annotations

import time

import numpy as np

from .plug import _NUMBER_WITH_UNIT, _REPLY_UNIT_SI, _UNIT_SUFFIXES, REPLY_UNITS, normalize_header
from .stream import build_packet

_KEYWORDS = ('MIN', 'MINIMUM', 'MAX', 'MAXIMUM', 'DEF', 'DEFAULT', 'UP', 'DOWN')

# Default replies by normalised header, in the device's reply format (PROTOCOL.md command
# table). UNKNOWN defaults take the vendor example's value.
DEFAULTS: dict[str, str] = {
    'FREQ': '100000000',
    'DATA:LENG': '1024',
    'DATA:PORT': '2758',  # vendor code default; the doc example shows 5025 (CONFLICT 1)
    'MODE': 'MASTER',  # UNKNOWN default (vendor example sets MASTer)
    'TRAN:TYPE': 'SING',
    'TRAN:REV': 'OFF',
    'TRAN:PULS': '20',
    'TRAN:FREQ': '5000000',
    'TRAN:DUR': '1',
    'TRAN:GAP': '5',
    'TRAN:DAMP': '0',
    'TRAN:DAMP:GAP': '10',
    'TRAN:IMP': 'HIGH',
    'TRAN:ENAB': 'OFF',
    'TRIG:MODE': 'INT',  # UNKNOWN default (vendor example sets INTERNAL)
    'TRIG:INT': '10.0E-3',
    'TRIG:DEL': '15000',
    'GAIN': '0',
    'GAIN:PRE:COMB': '0',  # UNKNOWN default
    'GAIN:PRE:SPLIT': '0',  # UNKNOWN default
    'GAIN:TGC:MODE': 'OFF',
    'GAIN:TGC:LIN': '20.0, 0.1',  # UNKNOWN default (doc example reply)
    'GAIN:TGC:ARB': '0,5,2,20,5,20,10,40,30,10',  # UNKNOWN default (vendor example points)
    'AVER:COUN': '0',
    'AVER:DEL:CONS:AUTO': 'ON',  # UNKNOWN default (vendor example sets ON)
    'AVER:DEL:CONS': '10.0E-6',
    'AVER:DEL:RAND': '2.0E-6',
    'FILT:HPAS:IND': '1',  # UNKNOWN default (doc example reply)
}

# How each header stores a written value. Boolean reply styles follow the doc formats:
# ON/OFF for TRAN:ENAB, TRAN:REV and AVER:DEL:CONS:AUTO, 0/1 for TRAN:DAMP and the preamps.
_ONOFF = frozenset({'TRAN:ENAB', 'TRAN:REV', 'AVER:DEL:CONS:AUTO'})
_DIGIT = frozenset({'TRAN:DAMP', 'GAIN:PRE:COMB', 'GAIN:PRE:SPLIT'})
_ENUM = frozenset({'MODE', 'TRAN:TYPE', 'TRIG:MODE', 'GAIN:TGC:MODE', 'TRAN:IMP'})
_LIST = frozenset({'GAIN:TGC:LIN', 'GAIN:TGC:ARB'})
_READ_ONLY = frozenset({'DATA:PORT'})
_SIGNALS = ('burst', 'zeros', 'none')


def _plain(value: float) -> str:
    """Integer text if the value is integral, else up to 10 significant digits."""
    if float(value).is_integer():
        return str(int(value))
    return format(value, '.10g')


def _engineering(value: float) -> str:
    """`0.1` -> `100.0E-3`, `5e-05` -> `50.0E-6` (the doc's seconds format)."""
    if value == 0:
        return '0.0E0'
    mantissa_text, exponent_text = f'{value:.12e}'.split('e')
    exp3 = (int(exponent_text) // 3) * 3
    mantissa = float(mantissa_text) * 10 ** (int(exponent_text) - exp3)
    text = format(mantissa, '.12g')
    if '.' not in text:
        text += '.0'
    return f'{text}E{exp3}'


def _short_enum(token: str) -> str:
    """Short upper form of an enumeration token: `LINear` -> `LIN`, `INTERNAL` -> `INT`."""
    lead = ''.join(c for c in token if c.isupper())
    if token != token.upper() and len(lead) >= 3:
        return lead
    token = token.upper()
    if len(token) > 4:
        token = token[:3] if token[3] in 'AEIOU' else token[:4]
    return token


class FakeA1580Resource:
    """Emulate the pyvisa message interface of an A1580 plus its A-scan data port.

    `visa_errors=True`: unknown-header queries raise `pyvisa.errors.VisaIOError` (timeout)
    like the real resource instead of `TimeoutError`, when pyvisa is importable. A header
    with an unknown node (`GAINX`, `FREQUE`, see `normalize_header`) queues `-113` and
    stores nothing.
    """

    def __init__(
        self,
        *,
        reject: dict[str, str] | None = None,
        length: int = 1024,
        idn: str = 'ACS-Solutions GmbH,A1580-HF,100500,1.6.b41',
        chunk: int | None = None,
        garbage_prefix: bytes = b'',
        signal: str = 'burst',
        visa_errors: bool = False,
    ) -> None:
        if signal not in _SIGNALS:
            raise ValueError(f'signal must be one of {_SIGNALS}, got {signal!r}')
        if chunk is not None and chunk < 1:
            raise ValueError(f'chunk must be >= 1, got {chunk}')
        self.log: list[str] = []
        self.closed = False
        self.timeout = 5000
        self.encoding = 'iso-8859-1'
        self.read_termination = '\r\n'
        self.write_termination = '\r\n'
        self.idn = idn
        self.chunk = chunk
        self.garbage_prefix = garbage_prefix
        self.signal = signal
        self.visa_errors = visa_errors
        self.connections: list[tuple[str, int]] = []
        self.sockets: list[FakeDataSocket] = []
        self.connect_count = 0
        self.started = False
        self.start_count = 0
        self.stop_count = 0
        self._reject = {normalize_header(k): v for k, v in (reject or {}).items()}
        self._errors: list[str] = []
        self._values = dict(DEFAULTS)
        self._values['DATA:LENG'] = str(length)

    # ── write / query ─────────────────────────────────────────────────────────

    def write(self, cmd: str) -> None:
        self.log.append(cmd)
        text = cmd.strip()
        head, _, arg = text.partition(' ')
        try:
            header = normalize_header(head)
        except ValueError:  # an unknown node (GAINX, FREQUE): the device does not know it
            self._errors.append(f'-113,"Undefined header;Command: {text}"')
            return
        arg = arg.strip()
        if header in self._reject:
            self._errors.append(self._reject[header])
            return
        if header == 'STAR' and arg.upper() == 'AUTO':
            self.started = True
            self.start_count += 1
        elif header == 'STOP':
            self.started = False
            self.stop_count += 1
        elif header == '*RST':
            self._values = dict(DEFAULTS)
            self.started = False
        elif header == '*CLS':
            self._errors.clear()
        elif header == 'MEM:CLE':
            pass
        elif header in self._values and header not in _READ_ONLY and arg:
            if header == 'AVER:DEL:CONS' and self._values['AVER:DEL:CONS:AUTO'] == 'ON':
                self._errors.append('-221,"Settings conflict"')
                return
            self._store(header, arg)
        else:
            self._errors.append(f'-113,"Undefined header;Command: {text}"')

    def query(self, cmd: str) -> str:
        self.log.append(cmd)
        text = cmd.strip()
        try:
            header = normalize_header(text)
        except ValueError:  # an unknown node: nothing answers
            self._errors.append(f'-113,"Undefined header;Command: {text}"')
            raise self._timeout_error(f'no reply to {text!r}') from None
        if header == '*IDN':
            return self.idn
        if header == 'SYST:ERR':
            return self._errors.pop(0) if self._errors else '0,"No error"'
        if header == 'SYST:ERR:COUN':
            return str(len(self._errors))
        if header == '*OPC':
            return '1'
        if header == '*TST':
            return '0'
        if text.endswith('?') and header in self._values:
            return self._values[header]
        # A real socket resource would time out waiting for the reply of a bad query.
        self._errors.append(f'-113,"Undefined header;Command: {text}"')
        raise self._timeout_error(f'no reply to {text!r}')

    def _timeout_error(self, message: str) -> Exception:
        """What a SCPI read that gets no reply raises: pyvisa's VisaIOError or TimeoutError.

        With `visa_errors=True` and pyvisa importable it is
        `pyvisa.errors.VisaIOError(StatusCode.error_timeout)`, which is what the real
        resource raises (and which is not an OSError). The data socket is a plain socket and
        keeps raising TimeoutError.
        """
        if self.visa_errors:
            try:
                import pyvisa.constants
                import pyvisa.errors
            except ImportError:
                pass
            else:
                return pyvisa.errors.VisaIOError(pyvisa.constants.StatusCode.error_timeout)
        return TimeoutError(message)

    def flush(self, mask: object = None) -> None:
        """Stand-in for `resource.flush(mask)`: discards nothing, records itself."""
        self.log.append('flush')

    def clear(self) -> None:
        """Stand-in for `resource.clear()`: discards nothing, records itself."""
        self.log.append('clear')

    def _store(self, header: str, arg: str) -> None:
        """Convert `arg` to the reply format of `header` and keep it (clamping hook for tests)."""
        try:
            self._values[header] = self._convert(header, arg)
        except ValueError as exc:
            self._errors.append(f'-102,"Syntax error;{exc}"')  # UNKNOWN: code and text invented

    def _convert(self, header: str, arg: str) -> str:
        """Reply text for a written `arg`. Raises ValueError for text the header cannot take."""
        upper = arg.upper()
        if upper in _KEYWORDS:
            return self._values[header]  # MIN/MAX/DEF/UP/DOWN: the fake does not know the limits
        if header in _ONOFF or header in _DIGIT:
            if upper in ('ON', '1'):
                truth = True
            elif upper in ('OFF', '0'):
                truth = False
            else:
                raise ValueError(f'{arg!r} is not a boolean')
            if header in _DIGIT:
                return '1' if truth else '0'
            return 'ON' if truth else 'OFF'
        if header in _LIST:
            parts = [p.strip() for p in arg.split(',')]
            if header == 'GAIN:TGC:ARB':
                for p in parts:
                    float(p)  # raises ValueError for a non-number
                return ','.join(parts)
            return ', '.join(repr(float(p)) for p in parts)
        if header in _ENUM:
            number = _NUMBER_WITH_UNIT.match(arg)
            if number:
                return _plain(float(number.group(1)))
            if header == 'MODE':  # the doc example sends MASTER and shows the reply MASTER
                for word in ('MASTER', 'SLAVE'):
                    if upper[:4] == word[:4]:
                        return word
            return _short_enum(arg)
        number = _NUMBER_WITH_UNIT.match(arg)
        if number is None:
            raise ValueError(f'{arg!r} is not a number')
        value = float(number.group(1))
        suffix = number.group(2).upper()
        unit = REPLY_UNITS.get(header)
        if suffix and unit is not None:
            if suffix not in _UNIT_SUFFIXES:
                raise ValueError(f'unknown unit {suffix!r}')
            dimension, factor = _UNIT_SUFFIXES[suffix]
            reply_dimension, reply_factor = _REPLY_UNIT_SI[unit]
            if dimension != reply_dimension:
                raise ValueError(f'{suffix} does not fit {header}')
            value = value * factor / reply_factor
        # UNKNOWN: a bare number is taken to be in the reply unit; the device may assume
        # another one (PROTOCOL.md "Write and query semantics").
        if unit == 's':
            return _engineering(value)
        return _plain(value)

    def close(self) -> None:
        self.closed = True

    # ── data channel ──────────────────────────────────────────────────────────

    def data_socket_factory(self, host: str, port: int) -> FakeDataSocket:
        """Stand-in for `socket.create_connection((host, port))`; records the address."""
        self.connections.append((host, port))
        self.connect_count += 1
        sock = FakeDataSocket(self)
        self.sockets.append(sock)
        return sock

    def _packet(self, number: int) -> bytes:
        """One synthetic A-scan packet from the current settings."""
        length = int(float(self._values['DATA:LENG']))
        if self.signal == 'zeros':
            samples = np.zeros(length, dtype=np.int16)
        else:
            fs = float(self._values['FREQ'])
            freq = float(self._values['TRAN:FREQ'])
            gain_db = float(self._values['GAIN'])
            t = np.arange(length) / fs
            t0 = 0.1 * length / fs
            tau = 5.0 / freq  # decay time: five burst periods
            envelope = np.where(t >= t0, np.exp(-np.maximum(t - t0, 0.0) / tau), 0.0)
            amplitude = 1000.0 * 10.0 ** (gain_db / 20.0)
            wave = amplitude * envelope * np.sin(2.0 * np.pi * freq * (t - t0))
            samples = np.rint(np.clip(wave, -32768, 32767)).astype(np.int16)
        return build_packet(
            samples.tolist(),
            packet_number=number % 256,
            ascan_count=int(float(self._values['AVER:COUN'])) & 0xFF,
        )


class FakeDataSocket:
    """Socket-like object: `recv`, `settimeout`, `close`, `shutdown`."""

    def __init__(self, resource: FakeA1580Resource) -> None:
        self._resource = resource
        self._out = bytearray()
        self._next_number = 0
        self._prefix_sent = False
        self.timeout: float | None = None
        self.closed = False
        self.shut = False

    def settimeout(self, value: float | None) -> None:
        self.timeout = value

    def shutdown(self, how: int) -> None:
        self.shut = True

    def close(self) -> None:
        self.closed = True

    def recv(self, n: int) -> bytes:
        if self.closed:
            raise OSError('fake data socket is closed')
        if self.shut:
            return b''  # like a real socket after shutdown: end of stream
        resource = self._resource
        if not resource.started or resource.signal == 'none':
            time.sleep(0.001)  # like a blocking recv: readers must not spin at 100 % CPU
            raise TimeoutError('fake data socket: nothing to receive')
        if not self._out:
            if not self._prefix_sent:
                self._out += resource.garbage_prefix
                self._prefix_sent = True
            self._out += resource._packet(self._next_number)
            self._next_number += 1
        limit = n if resource.chunk is None else min(n, resource.chunk)
        data = bytes(self._out[:limit])
        del self._out[:limit]
        return data
