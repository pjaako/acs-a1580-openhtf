# Adapted from rigol-dho-openhtf fake_resource.py (shape of the fake: log, error queue,
# reject map, generic settings store). The command set, reply formats and the data socket
# are specific to the A1580 and follow PROTOCOL.md. Phase A of the first hardware session
# (2026-10-05, fw 1.16 (861f022a), read-only queries) and phase B (pulser off, acquisition) are
# folded in where a comment says `measured`; everything else was not measured.
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

# Reply of `*RST`: the vendor's `DEFault` column in the device's reply format (PROTOCOL.md
# command table). UNKNOWN defaults take the vendor example's value. What the device does on
# `*RST` was not measured; what it has after power-on is `POWER_ON` below.
DEFAULTS: dict[str, str] = {
    'FREQ': '100000000',
    'DATA:LENG': '1024',
    'DATA:PORT': '2758',  # vendor code default; the doc example shows 5025 (CONFLICT 1)
    'MODE': 'MASTer',  # UNKNOWN default (vendor example sets MASTer)
    'TRAN:TYPE': 'SINGle',
    'TRAN:REV': '0',
    'TRAN:PULS': '20',
    'TRAN:FREQ': '5000000',
    'TRAN:DUR': '1',
    'TRAN:GAP': '5',
    'TRAN:DAMP': '0',
    'TRAN:DAMP:GAP': '10',
    'TRAN:IMP': 'HIGH',
    'TRAN:ENAB': '0',
    'TRIG:MODE': 'INTernal',  # UNKNOWN default (vendor example sets INTERNAL)
    # Time replies are shortest decimals (measured 2026-10-05, fw 1.16, see `_seconds`).
    # `0.01` for 10 ms is measured too (2026-10-05, fw 1.16); `1e-05` for 10 us is not.
    'TRIG:INT': '0.01',
    'TRIG:DEL': '15000',
    'GAIN': '0',
    'GAIN:PRE:COMB': '0',  # UNKNOWN default
    'GAIN:PRE:SPLIT': '0',  # UNKNOWN default
    'GAIN:TGC:MODE': 'OFF',
    'GAIN:TGC:LIN': '20.0, 0.1',  # UNKNOWN default (doc example reply)
    'GAIN:TGC:ARB': '0,5,2,20,5,20,10,40,30,10',  # UNKNOWN default (vendor example points)
    'AVER:COUN': '0',
    'AVER:DEL:CONS:AUTO': '1',  # UNKNOWN default (vendor example sets ON)
    'AVER:DEL:CONS': '1e-05',  # only seen while AUTO is off; with AUTO on `query` computes it
    'AVER:DEL:RAND': '2e-06',
    'FILT:HPAS:IND': '1',  # UNKNOWN default (doc example reply)
}

# State of the device right after power-on (measured 2026-10-05, fw 1.16: a power cycle
# brought back exactly these values, whatever had been set before): the pulser is ON at
# 20 V, `TRAN:TYPE` DUAL, `TRIG:INT` 1 s, `DATA:LENG` 114688 (a value its own setter refuses,
# see `_check_range`), `AVER:DEL:CONS` 0.99992925 (automatic), no HPAS index, TGC `0,0` and an
# empty `GAIN:TGC:ARB`. The fake starts from this only with `power_on=True` (a fake that
# starts with the pulser on and a `DATA:LENG` its own setter refuses changed 36 tests of the
# suite, so it is opt-in); `*RST` always goes to `DEFAULTS`.
POWER_ON: dict[str, str] = {
    **DEFAULTS,
    'DATA:LENG': '114688',
    'TRAN:TYPE': 'DUAL',
    'TRAN:ENAB': '1',
    'TRIG:INT': '1',
    'GAIN:TGC:LIN': '0,0',
    'GAIN:TGC:ARB': '',
    'AVER:DEL:CONS': '0.99992925',
    'FILT:HPAS:IND': '0',
}

# How each header stores a written value. Booleans are accepted as ON/OFF/1/0 and always
# answer 0/1 (measured 2026-10-05, fw 1.16, for all six: TRAN:ENAB, TRAN:REV, TRAN:DAMP,
# GAIN:PRE:COMB, GAIN:PRE:SPLIT, AVER:DEL:CONS:AUTO; the vendor doc shows ON/OFF).
_BOOLEAN = frozenset(
    {'TRAN:ENAB', 'TRAN:REV', 'TRAN:DAMP', 'GAIN:PRE:COMB', 'GAIN:PRE:SPLIT', 'AVER:DEL:CONS:AUTO'}
)
# Enumerations answer with the vendor's mixed-case notation string, whatever form was sent
# (measured 2026-10-05, fw 1.16: MODE? -> `MASTer`, TRIG:MODE? -> `INTernal`, TRAN:TYPE? ->
# `DUAL`, TRAN:IMP? -> `HIGH`, GAIN:TGC:MODE? -> `OFF`). Measured notations: MASTer,
# INTernal, DUAL, HIGH, OFF. Assumed by analogy with the doc's notation: SLAVe, CTP,
# ENCoder, TTL, SINGle, LINear, ARBitrary, 200, 1000.
_ENUM_VALUES: dict[str, tuple[str, ...]] = {
    'MODE': ('MASTer', 'SLAVe'),
    'TRAN:TYPE': ('SINGle', 'DUAL'),
    'TRIG:MODE': ('INTernal', 'CTP', 'ENCoder', 'TTL'),
    'GAIN:TGC:MODE': ('OFF', 'LINear', 'ARBitrary'),
    'TRAN:IMP': ('HIGH', '200', '1000'),
}
_ENUM = frozenset(_ENUM_VALUES)
_LIST = frozenset({'GAIN:TGC:LIN', 'GAIN:TGC:ARB'})
_READ_ONLY = frozenset({'DATA:PORT'})
_SIGNALS = ('burst', 'zeros', 'noise', 'none', 'transmission')
# `signal='transmission'`: measured 2026-10-05, fw 1.16, one pair of 50 kHz transducers face to
# face, 20 V (one bench setup). With `TRAN:ENAB` on the record holds noise plus a damped sine at
# `TRAN:FREQ` that starts 3 us after sample 0 (3 samples at 1 MHz) and does NOT move with
# `TRIG:DEL`: burst and record are delayed together. Amplitude about 18 counts at `GAIN` 0 and
# about 1350 at 40 dB (measured 37.4 dB for the 40 dB step, a ratio of 74; modelled as 18 counts
# times 10^(0.935 * gain / 20), 1332 at 40 dB). It rings down in about 5 periods (measured
# until about 120 us at 50 kHz, only noise from 200 us). The pulse-voltage scaling (counts
# proportional to `TRAN:PULS` / 20 V) is a guess: only 20 V was measured. Pulser off: noise
# alone, offset 4.5 counts (4.3 to 4.65 at 1 MHz), std 3.6 counts at 0 dB and 25.7 at 40 dB
# (7.1 times); modelled as 3.6 * 7.1^(gain / 40), also with the pulser off.
_TRANSMISSION_ARRIVAL_S = 3e-6
_TRANSMISSION_AMPLITUDE = 18.0
_TRANSMISSION_SIGNAL_DB_PER_DB = 37.4 / 40.0
_TRANSMISSION_NOISE_STD = 3.6
_TRANSMISSION_NOISE_PER_40_DB = 7.1
_TRANSMISSION_OFFSET = 4.5
_ERROR_QUEUE_DEPTH = 16  # measured 2026-10-05, fw 1.16: 16 entries, then `-350` as the 17th
_QUEUE_OVERFLOW = '-350,"Queue overflow"'
# measured for DATA:LENG out of range (2026-10-05, fw 1.16), assumed for out-of-range values
# of the other headers and for bad enumeration words
_DATA_LENG_MIN = 1024  # measured 2026-10-05, fw 1.16: `DATA:LENG MIN`; 1023 is refused
_DATA_LENG_MAX = 36864  # measured: `DATA:LENG MAX`; 36865, 65536 and 114688 are refused
_DATA_LENG_DEFAULT = 1024  # measured: `DATA:LENG DEF`
# `DATA:LENG MIN/MAX/DEF` answer these on the next query (measured for the short forms; the
# long forms are the same SCPI keywords, not measured). The other headers' limits are unknown.
_DATA_LENG_KEYWORDS = {
    'MIN': _DATA_LENG_MIN,
    'MINIMUM': _DATA_LENG_MIN,
    'MAX': _DATA_LENG_MAX,
    'MAXIMUM': _DATA_LENG_MAX,
    'DEF': _DATA_LENG_DEFAULT,
    'DEFAULT': _DATA_LENG_DEFAULT,
}
# Automatic `AVER:DEL:CONS` = `TRIG:INT` minus this many seconds. Measured 2026-10-05, fw 1.16
# at `AVER:COUN` 0 and `FREQ` 100 MHz only: 0.99992925 at 1 s, 0.00992925 at 10 ms. Whether it
# depends on `AVER:COUN` or `FREQ` is not measured.
_AUTO_CONSTANT_DELAY_OFFSET_S = 70.75e-6
# `DATA:LENG` above the limit (only the power-on value 114688 gets there) gives packets that
# are partly invalid: measured 2026-10-05, fw 1.16 at 114688, samples from this index on are
# exactly 0 (81906 = 5 * 16384 - 14). The device also has a long constant stretch (30 counts)
# in the first 65522 samples, at a different start in every packet; not modelled.
_ZERO_TAIL_START = 81906
_ILLEGAL_PARAMETER = '-224,"Illegal parameter value"'
_MAX_LINE = 256  # see `write`


def _plain(value: float) -> str:
    """Integer text if the value is integral, else up to 10 significant digits."""
    if float(value).is_integer():
        return str(int(value))
    return format(value, '.10g')


def _seconds(value: float) -> str:
    """Seconds reply: shortest decimal, lower-case `e`, no `.0` (`1`, `0.01`, `2e-06`).

    Measured 2026-10-05, fw 1.16: `1`, `0.01`, `0.99992925`, `0.00992925`, `2e-06`. That
    `1e-05` comes out the same way is an extrapolation from these replies.
    """
    value = float(f'{value:.12g}')  # drop the float noise of unit conversion (2000 ns)
    if value.is_integer():
        return str(int(value))
    return repr(value)


def _enum_notation(header: str, token: str) -> str:
    """The notation string of `token` (`MASTER`, `mast`, `MASTer`): `MASTer`. Raises ValueError."""
    upper = token.upper()
    for notation in _ENUM_VALUES[header]:
        short = ''.join(c for c in notation if c.isupper() or c.isdigit())
        if upper in (notation.upper(), short):
            return notation
    raise ValueError(f'{token!r} is not one of {_ENUM_VALUES[header]}')


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
        length: int | None = None,
        idn: str = 'ACS-Solutions GmbH,A1580-HF,100500,1.16 (861f022a)',
        chunk: int | None = None,
        garbage_prefix: bytes = b'',
        signal: str = 'burst',
        visa_errors: bool = False,
        power_on: bool = False,
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
        self._values = dict(POWER_ON if power_on else DEFAULTS)
        if length is not None:
            self._values['DATA:LENG'] = str(length)
        # measured 2026-10-05, fw 1.16: the first packet after the first `STAR AUTO` after
        # power-on had number 2 and the numbering went on across STOP/START (2..11, then
        # 12..21). Seen once: nothing may be built on the value 2, only on the counter not
        # restarting. The header field is one byte, so `packet_number` wraps at 256 (layout, not
        # measured); `_next_packet` itself does not wrap: it goes into `ctp[0]` (uint32).
        self._next_packet = 2
        self._flights = 0  # number of STOPs that found the stream running

    # ── write / query ─────────────────────────────────────────────────────────

    def write(self, cmd: str) -> None:
        # measured 2026-10-05, fw 1.16: 25 lines (about 320 bytes) written back to back
        # without a read left one processed `-113` and one `-363,"Input buffer overrun"` in
        # the queue, the rest was discarded and the next query was never answered. Bursts of
        # up to ten 16-byte lines (160 bytes) were all processed, so the limit is an input
        # buffer size between 160 and about 320 bytes, not pacing. The fake cannot see
        # bursts; it models only a single line longer than 256 bytes (the 256 is a guess).
        # measured 2026-10-05, fw 1.16: opening, querying and closing a second connection
        # to port 5025 killed the first one (seen once, open or close not isolated). The
        # fake has one resource and no client count: not modelled yet.
        self.log.append(cmd)
        if len(cmd.encode(self.encoding, errors='replace')) > _MAX_LINE:
            self._queue_error('-363,"Input buffer overrun"')
            return
        text = cmd.strip()
        head, _, arg = text.partition(' ')
        try:
            header = normalize_header(head)
        except ValueError:  # an unknown node (GAINX, FREQUE): the device does not know it
            self._queue_error(f'-113,"Undefined header;{text}"')
            return
        arg = arg.strip()
        if header in self._reject:
            self._queue_error(self._reject[header])
            return
        if header == 'STAR' and arg.upper() == 'AUTO':
            self.started = True
            self.start_count += 1
        elif header == 'STOP':
            if self.started:
                self._flights += 1
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
            if header == 'AVER:DEL:CONS' and self._values['AVER:DEL:CONS:AUTO'] == '1':
                self._queue_error('-221,"Settings conflict"')
                return
            self._store(header, arg)
        else:
            self._queue_error(f'-113,"Undefined header;{text}"')

    def query(self, cmd: str) -> str:
        self.log.append(cmd)
        text = cmd.strip()
        try:
            header = normalize_header(text)
        except ValueError:  # an unknown node: nothing answers
            self._queue_error(f'-113,"Undefined header;{text}"')
            raise self._timeout_error(f'no reply to {text!r}') from None
        if header == '*IDN':
            return self.idn
        if header == 'SYST:VERS':
            return '1999.0'  # measured 2026-10-05, fw 1.16
        if header == 'SYST:ERR':
            return self._errors.pop(0) if self._errors else '0,"No error"'
        if header == 'SYST:ERR:COUN':
            return str(len(self._errors))
        if header == '*OPC':
            return '1'
        if header == '*TST':
            return '0'
        if (
            header == 'AVER:DEL:CONS'
            and text.endswith('?')
            and self._values['AVER:DEL:CONS:AUTO'] == '1'
        ):
            # measured 2026-10-05, fw 1.16 at AVER:COUN 0, FREQ 100 MHz only (see the constant)
            return _seconds(float(self._values['TRIG:INT']) - _AUTO_CONSTANT_DELAY_OFFSET_S)
        if text.endswith('?') and header in self._values:
            return self._values[header]
        # A real socket resource would time out waiting for the reply of a bad query.
        self._queue_error(f'-113,"Undefined header;{text}"')
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

    def _queue_error(self, entry: str) -> None:
        """Queue an error like the device: 16 entries, then `-350`, later errors are dropped.

        Measured 2026-10-05, fw 1.16 with 40 undefined headers: `SYST:ERR:COUN?` went 1..16,
        then 17 and stayed; draining gave the first 16 and `-350,"Queue overflow"` last.
        """
        if len(self._errors) < _ERROR_QUEUE_DEPTH:
            self._errors.append(entry)
        elif self._errors[-1] != _QUEUE_OVERFLOW:
            self._errors.append(_QUEUE_OVERFLOW)

    def _store(self, header: str, arg: str) -> None:
        """Convert `arg` to the reply format of `header` and keep it (clamping hook for tests)."""
        try:
            value = self._convert(header, arg)
            self._check_range(header, value)
            self._values[header] = value
        except ValueError:
            self._queue_error(_ILLEGAL_PARAMETER)

    @staticmethod
    def _check_range(header: str, value: str) -> None:
        """Raise ValueError for a number the device refuses (error, old value kept).

        Measured 2026-10-05, fw 1.16: `DATA:LENG` 1024 and 36864 are accepted, 1023, 36865,
        65536 and 114688 give `-224,"Illegal parameter value"` and the value is unchanged (no
        clamping); 1025 is accepted, so there is no block-size rule. No other header has a
        range here.
        """
        if header == 'DATA:LENG' and not _DATA_LENG_MIN <= float(value) <= _DATA_LENG_MAX:
            raise ValueError(f'{value} is out of range')

    def _convert(self, header: str, arg: str) -> str:
        """Reply text for a written `arg`. Raises ValueError for text the header cannot take."""
        upper = arg.upper()
        if header == 'DATA:LENG' and upper in _DATA_LENG_KEYWORDS:
            return str(_DATA_LENG_KEYWORDS[upper])
        if upper in _KEYWORDS:
            return self._values[header]  # MIN/MAX/DEF/UP/DOWN: the fake does not know the limits
        if header in _BOOLEAN:
            if upper in ('ON', '1'):
                truth = True
            elif upper in ('OFF', '0'):
                truth = False
            else:
                raise ValueError(f'{arg!r} is not a boolean')
            return '1' if truth else '0'
        if header in _LIST:
            parts = [p.strip() for p in arg.split(',')]
            if header == 'GAIN:TGC:ARB':
                for p in parts:
                    float(p)  # raises ValueError for a non-number
                return ','.join(parts)
            numbers = [float(p) for p in parts]
            stored = self._values[header]
            # not measured, so that hw_probe's restore of a power-on `0,0` reads back as it was
            # found: the same numbers as the stored value keep the stored text
            if stored and [float(x) for x in stored.split(',')] == numbers:
                return stored
            return ', '.join(repr(x) for x in numbers)
        if header in _ENUM:
            number = _NUMBER_WITH_UNIT.match(arg)
            if number:
                return _plain(float(number.group(1)))
            return _enum_notation(header, arg)
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
            return _seconds(value)
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

    def _packet(self) -> bytes:
        """One synthetic A-scan packet from the current settings; numbers never restart."""
        number = self._next_packet
        self._next_packet = number + 1
        length = int(float(self._values['DATA:LENG']))
        averaging = int(float(self._values['AVER:COUN']))
        if self.signal == 'zeros':
            samples = np.zeros(length, dtype=np.int16)
        elif self.signal == 'noise':
            # An open input, pulser off. Measured 2026-10-05, fw 1.16: offset about +11
            # counts, std 3.59 counts at AVER:COUN 0 and 0.94 at AVER:COUN 4, so the count is
            # an exponent and the result a mean over 2^N acquisitions: std / 2^(N/2).
            rng = np.random.default_rng(number)
            noise = rng.normal(11.0, 3.6 / 2.0 ** (averaging / 2.0), length)
            samples = np.rint(noise).astype(np.int16)
        elif self.signal == 'transmission':
            # measured 2026-10-05, fw 1.16 (see `_TRANSMISSION_ARRIVAL_S`); averaging keeps the
            # signal and divides the noise std by 2^(N/2) (measured with the pulser on: peak
            # ratio 0.999 at AVER:COUN 4, the noise ratio under the signal is not measured)
            rng = np.random.default_rng(number)
            gain_db = float(self._values['GAIN'])
            std = _TRANSMISSION_NOISE_STD * _TRANSMISSION_NOISE_PER_40_DB ** (gain_db / 40.0)
            wave = rng.normal(_TRANSMISSION_OFFSET, std / 2.0 ** (averaging / 2.0), length)
            if self._values['TRAN:ENAB'] == '1':
                fs = float(self._values['FREQ'])
                freq = float(self._values['TRAN:FREQ'])
                volts = float(self._values['TRAN:PULS'])
                first = round(_TRANSMISSION_ARRIVAL_S * fs)
                t = (np.arange(length) - first) / fs
                envelope = np.where(t >= 0.0, np.exp(-np.maximum(t, 0.0) * freq / 2.5), 0.0)
                amplitude = (
                    _TRANSMISSION_AMPLITUDE
                    * 10.0 ** (_TRANSMISSION_SIGNAL_DB_PER_DB * gain_db / 20.0)
                    * volts
                    / 20.0
                )
                wave = wave + amplitude * envelope * np.sin(2.0 * np.pi * freq * t)
            samples = np.rint(np.clip(wave, -32768, 32767)).astype(np.int16)
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
        if length > _DATA_LENG_MAX:
            samples[_ZERO_TAIL_START:] = 0
        return build_packet(
            samples.tolist(),
            packet_number=number % 256,
            # measured 2026-10-05, fw 1.16 in `TRIG:MODE INT`: ctp[0] equals the packet number
            # (35, 36, ... 112 seen, 2, 3, 4 right after power-on), ctp[1] = ctp[2] = 0. Whether
            # ctp[0] goes on past 255 is not measured; here it does (the counter is not wrapped).
            ctp=(number, 0, 0),
            # measured 2026-10-05, fw 1.16 at DATA:LENG 1024, 2048, 8192, 36864 and 114688:
            # (length_hi << 16 | length_lo) = sample count + 16 (a 24-bit value; at 114688 it is
            # 114704, length_hi 1, length_lo 49168); what the 16 is, is unknown. Telemetry 120,
            # 86, 52; reserved bytes 0.
            length_lo=(length + 16) & 0xFFFF,
            length_hi=(length + 16) >> 16,
            telemetry_a=120,
            telemetry_b=86,
            telemetry_c=52,
            ascan_count=1,  # measured: 1 for AVER:COUN 0 and 4, the averaging is not counted here
        )


class FakeDataSocket:
    """Socket-like object: `recv`, `settimeout`, `close`, `shutdown`."""

    def __init__(self, resource: FakeA1580Resource) -> None:
        self._resource = resource
        self._out = bytearray()
        self._tail = 0  # packets still in flight after a STOP
        self._flights = resource._flights
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
        if resource.started:
            self._tail = 0
            self._flights = resource._flights
        elif resource._flights > self._flights:
            # measured 2026-10-05, fw 1.16: after STOP two more packets arrived within 20 ms,
            # then the socket went idle and stayed open (the device does not close it)
            self._flights = resource._flights
            self._tail = 2
        if resource.signal == 'none' or not (resource.started or self._tail or self._out):
            time.sleep(0.001)  # like a blocking recv: readers must not spin at 100 % CPU
            raise TimeoutError('fake data socket: nothing to receive')
        if not self._out:
            if not self._prefix_sent:
                self._out += resource.garbage_prefix
                self._prefix_sent = True
            self._out += resource._packet()
            if not resource.started:
                self._tail -= 1
        limit = n if resource.chunk is None else min(n, resource.chunk)
        data = bytes(self._out[:limit])
        del self._out[:limit]
        return data
