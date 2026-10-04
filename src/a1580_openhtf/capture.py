"""Capture conditions for the ACS A1580 pulser-receiver: a typed model plus a YAML front end.

Provenance: the mechanics are copied and adapted from `capture.py` of
https://github.com/pjaako/rigol-dho-openhtf: frozen keyword-only slotted dataclasses, `Literal`
aliases as the single source of truth, `Decimal` quantities, all problems reported in one pass as
`<file>:<line>: <key path>: ...`, `difflib` suggestions, duplicate-key detection, the 'quote it'
hint for YAML 1.1 booleans, the `extra:` escape hatch and schema generation from the dataclasses.
The instrument model, units, ranges and SCPI headers are specific to the A1580 (`PROTOCOL.md`).

Python code builds a `Capture` (`Capture(pulser=Pulser(voltage=Decimal(20)))`); YAML is the
front end for people who do not know SCPI (`load_capture('captures/vendor_example.yaml')`).
Both end at `Capture.to_scpi()`, which is what `A1580Plug.apply_setup()` takes.

Quantities held by the model are `Decimal` numbers in the field's canonical unit, which is also the
unit the SCPI argument is written in (see the `unit` of each field's spec and its help text).

Command line:
    python -m a1580_openhtf.capture schema [--write]
    python -m a1580_openhtf.capture check FILE...
"""

import argparse
import dataclasses
import difflib
import functools
import json
import logging
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Protocol, get_args, get_origin, get_type_hints

_LOG = logging.getLogger(__name__)

# ── the one source of truth for allowed choices ─────────────────────────
# Each alias is read by the parser, `to_scpi()` and the schema generator.

Mode = Literal['master', 'slave']
SampleRate = Literal[1, 2, 5, 10, 25, 50, 100]  # MHz
PulserType = Literal['single', 'dual']
Impedance = Literal['high', '200', '1000']
HighPass = Literal['1 kHz', '100 kHz', '500 kHz', '1 MHz']
TgcMode = Literal['off', 'linear', 'arbitrary']
TriggerMode = Literal['internal', 'ctp', 'encoder', 'ttl']

# SCPI spelling of each choice. Mixed-case mnemonics on purpose: the device may reply with the short
# form (`INT`), and the plug accepts a reply that equals the capitals of the sent mnemonic.
_MODE_SCPI: dict[str, str] = {'master': 'MASTer', 'slave': 'SLAVe'}
_TYPE_SCPI: dict[str, str] = {'single': 'SINGle', 'dual': 'DUAL'}
_IMPEDANCE_SCPI: dict[str, str] = {'high': 'HIGH', '200': '200', '1000': '1000'}
_TGC_SCPI: dict[str, str] = {'off': 'OFF', 'linear': 'LINear', 'arbitrary': 'ARBitrary'}
_TRIGGER_SCPI: dict[str, str] = {
    'internal': 'INTernal',
    'ctp': 'CTP',
    'encoder': 'ENCoder',
    'ttl': 'TTL',
}

# Pulser voltage above which `check` warns (the owner wants high voltage to be deliberate).
HIGH_VOLTAGE_WARNING_V = Decimal(50)

# ── quantities: units, parsing and formatting ───────────────────────────

_D = Decimal
_FACTORS: dict[str, dict[str, Decimal]] = {
    'frequency': {'hz': _D(1), 'khz': _D(1000), 'mhz': _D(1000000)},
    'time': {'ns': _D('1e-9'), 'us': _D('1e-6'), 'ms': _D('1e-3'), 's': _D(1)},
    'voltage': {'v': _D(1)},
    'gain': {'db': _D(1)},
    'slope': {'db/us': _D(1)},
}
_SPELLING: dict[str, str] = {
    'hz': 'Hz',
    'khz': 'kHz',
    'mhz': 'MHz',
    'ns': 'ns',
    'us': 'us',
    'ms': 'ms',
    's': 's',
    'v': 'V',
    'db': 'dB',
    'db/us': 'dB/us',
}
_DIM_NOUN: dict[str, str] = {
    'frequency': 'a frequency',
    'time': 'a time',
    'voltage': 'a voltage',
    'gain': 'a gain',
    'slope': 'a slope',
}
_DIM_DOC: dict[str, str] = {
    'frequency': 'A frequency with a unit (Hz, kHz or MHz), e.g. {example}.',
    'time': 'A time with a unit (ns, us, ms or s), e.g. {example}.',
    'voltage': 'A voltage with a unit (V), e.g. {example}.',
    'gain': 'A gain with a unit (dB), e.g. {example}.',
    'slope': 'A gain slope with a unit (dB/us), e.g. {example}.',
}
_DIM_EXAMPLE: dict[str, str] = {
    'frequency': '2.5 MHz',
    'time': '10 us',
    'voltage': '20 V',
    'gain': '10 dB',
    'slope': '0.1 dB/us',
}

_QTY_RE = re.compile(
    r'^\s*(?P<num>[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d{1,3})?)\s*(?P<unit>.*?)\s*$', re.DOTALL
)


def _fmt_num(value: Decimal | int) -> str:
    """A number as plain decimal text: `Decimal('1E+2')` becomes `100`, no exponent, no `.0`."""
    d = Decimal(value)
    if d == 0:
        return '0'
    return format(d.normalize(), 'f')


def _human(value: Decimal, dim: str, canonical: str) -> str:
    """`value` (in the canonical unit) written in the largest unit that keeps it at or above 1."""
    factors = _FACTORS[dim]
    base = value * factors[canonical.lower()]
    best = canonical.lower()
    if base != 0:
        for unit, factor in sorted(factors.items(), key=lambda kv: kv[1], reverse=True):
            if abs(base) >= factor:
                best = unit
                break
    return f'{_fmt_num(base / factors[best])} {_SPELLING[best]}'


def _ci(unit: str) -> str:
    """A regex that matches `unit` in any letter case (JSON schema regexes have no flags)."""
    out = []
    for ch in unit:
        if ch == '/':
            out.append(r'\s*/\s*')
        elif ch == 'u':
            out.append('[uUµ]')
        elif ch.isalpha():
            out.append(f'[{ch.lower()}{ch.upper()}]')
        else:
            out.append(re.escape(ch))
    return ''.join(out)


def _quantity_pattern(dim: str) -> str:
    units = '|'.join(_ci(u) for u in sorted(_FACTORS[dim], key=len, reverse=True))
    return rf'^\s*[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d{{1,3}})?\s*({units})\s*$'


def _norm_unit(text: str) -> str:
    return re.sub(r'\s+', '', text).lower().replace('µ', 'u')


def _norm_choice(text: str) -> str:
    return re.sub(r'\s+', '', text).lower()


def _d(text: str) -> Decimal:
    return Decimal(text)


# ── field specs: what a field is, how it is validated, how it is documented ──


@dataclass(frozen=True, slots=True)
class _Flag:
    """A boolean."""


@dataclass(frozen=True, slots=True)
class _Choice:
    """One of the values of the field's `Literal` annotation (compared case-insensitively)."""


@dataclass(frozen=True, slots=True)
class _Qty:
    """A quantity written with a unit. The Python value is a `Decimal` in `unit`.

    If the annotation holds a `Literal` (and `auto` is false) the value must be one of its members.
    `auto=True` additionally accepts the text `auto`.
    """

    dim: str
    unit: str
    lo: Decimal | None = None
    hi: Decimal | None = None
    step: Decimal | None = None
    auto: bool = False
    example: str = ''


@dataclass(frozen=True, slots=True)
class _Whole:
    """An integer with optional bounds."""

    lo: int | None = None
    hi: int | None = None


@dataclass(frozen=True, slots=True)
class _Num:
    """A plain number (no unit); the Python value is a `Decimal`."""

    lo: Decimal | None = None
    hi: Decimal | None = None
    step: Decimal | None = None


@dataclass(frozen=True, slots=True)
class _Group:
    """A nested block; the dataclass comes from the field's annotation."""


@dataclass(frozen=True, slots=True)
class _Curve:
    """A non-empty list of blocks of the dataclass `item`."""

    item: type


@dataclass(frozen=True, slots=True)
class _Pairs:
    """A free mapping header -> string (`extra`)."""


_Spec = _Flag | _Choice | _Qty | _Whole | _Num | _Group | _Curve | _Pairs


def _opt(spec: _Spec, doc: str, scpi: str = '') -> Any:
    return field(default=None, metadata={'f': (spec, doc, scpi)})


def _req(spec: _Spec, doc: str) -> Any:
    return field(metadata={'f': (spec, doc, '')})


class CaptureError(ValueError):
    """All problems found in one capture description, in file order.

    `.problems` is the list of formatted lines, each `<file>:<line>: <key path>: <what is wrong>`
    (for objects built in Python: `<Class>.<field> = <value>: <what is wrong>`).
    """

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__('\n'.join(problems))


# ── the model ────────────────────────────────────────────────────────────


@dataclass(frozen=True, kw_only=True, slots=True)
class TgcPoint:
    """One point of an arbitrary TGC curve."""

    time: Decimal = _req(_Qty('time', 'us', example='10 us'), 'Time of the point, e.g. 10 us.')
    gain: Decimal = _req(
        _Qty('gain', 'dB', example='5 dB'), 'Gain added to the receiver gain at this time.'
    )

    def __post_init__(self) -> None:
        _check_init(self)


@dataclass(frozen=True, kw_only=True, slots=True)
class TgcLinear:
    """Linear TGC: constant gain up to `offset`, then `slope` dB per microsecond."""

    offset: Decimal = _req(
        _Qty('time', 'us', example='20 us'), 'Time before the gain starts to change.'
    )
    slope: Decimal = _req(
        _Qty('slope', 'dB/us', example='0.1 dB/us'),
        'Gain change per time; may be negative or zero. The device stops at its own limits.',
    )

    def __post_init__(self) -> None:
        _check_init(self)


def _first_unordered(points: tuple[TgcPoint, ...]) -> int | None:
    """Index of the first point whose time is not later than the one before it."""
    for i in range(1, len(points)):
        if points[i].time <= points[i - 1].time:
            return i
    return None


@dataclass(frozen=True, kw_only=True, slots=True)
class Tgc:
    """Time-gain compensation. `mode` is emitted after the curve it selects."""

    mode: TgcMode | None = _opt(
        _Choice(), 'Constant gain (off), linear ramp, or arbitrary curve.', 'GAIN:TGC:MODE'
    )
    linear: TgcLinear | None = _opt(_Group(), 'Curve for mode linear.', 'GAIN:TGC:LIN')
    arbitrary: tuple[TgcPoint, ...] | None = _opt(
        _Curve(TgcPoint),
        'Curve for mode arbitrary: points of time and gain, times strictly increasing. '
        'The gains are added to receiver.gain. Maximum number of points is UNKNOWN.',
        'GAIN:TGC:ARB',
    )

    @staticmethod
    def cross_problems(values: Mapping[str, object]) -> list[tuple[str, str]]:
        found: list[tuple[str, str]] = []
        mode = values.get('mode')
        if mode == 'linear' and values.get('linear') is None:
            found.append(('mode', "mode 'linear' needs a 'linear' curve (offset and slope)"))
        if mode == 'arbitrary' and values.get('arbitrary') is None:
            found.append(('mode', "mode 'arbitrary' needs an 'arbitrary' list of points"))
        return found

    def __post_init__(self) -> None:
        _check_init(self)

    def _scpi(self) -> dict[str, str]:
        out: dict[str, str] = {}
        if self.linear is not None:
            out['GAIN:TGC:LIN'] = f'{float(self.linear.offset)!r}, {float(self.linear.slope)!r}'
        if self.arbitrary is not None:
            out['GAIN:TGC:ARB'] = ','.join(
                f'{_fmt_num(p.time)},{_fmt_num(p.gain)}' for p in self.arbitrary
            )
        if self.mode is not None:
            out['GAIN:TGC:MODE'] = _TGC_SCPI[self.mode]
        return out


@dataclass(frozen=True, kw_only=True, slots=True)
class Sampling:
    """A-scan sampling."""

    rate: SampleRate | None = _opt(
        _Qty('frequency', 'MHz', example='100 MHz'),
        'Sampling frequency; only 1, 2, 5, 10, 25, 50 and 100 MHz exist.',
        'FREQ',
    )
    length: int | None = _opt(
        _Whole(1024, 36864),
        'Samples per A-scan. Whether odd sizes are accepted is UNKNOWN.',
        'DATA:LENG',
    )

    def __post_init__(self) -> None:
        _check_init(self)

    def _scpi(self) -> dict[str, str]:
        out: dict[str, str] = {}
        if self.rate is not None:
            out['FREQ'] = f'{self.rate} MHZ'
        if self.length is not None:
            out['DATA:LENG'] = str(self.length)
        return out


@dataclass(frozen=True, kw_only=True, slots=True)
class Pulser:
    """Transmitter settings. `enabled` is emitted last of all headers."""

    enabled: bool | None = _opt(
        _Flag(),
        'Pulser on or off. Emitted after every other header so the pulser stays in its old '
        'state while the rest changes.',
        'TRAN:ENAB',
    )
    type: PulserType | None = _opt(
        _Choice(),
        'single: single crystal, burst on IN. dual: dual crystal, burst on OUT.',
        'TRAN:TYPE',
    )
    reverse_polarity: bool | None = _opt(
        _Flag(), 'true: the burst starts negative; false: positive.', 'TRAN:REV'
    )
    voltage: Decimal | None = _opt(
        _Qty('voltage', 'V', _d('5'), _d('100'), _d('5'), example='20 V'),
        'Burst amplitude. Above 50 V `check` prints a warning.',
        'TRAN:PULS',
    )
    frequency: Decimal | None = _opt(
        _Qty('frequency', 'kHz', _d('10'), _d('20000'), example='2.5 MHz'),
        'Burst frequency. Emitted in kHz, like the vendor example.',
        'TRAN:FREQ',
    )
    periods: Decimal | None = _opt(
        _Num(_d('0.5'), _d('16'), _d('0.5')),
        'Number of periods in the burst (the vendor code questions whether this is half-periods).',
        'TRAN:DUR',
    )
    gap: Decimal | None = _opt(
        _Qty('time', 'ns', _d('0'), _d('500'), _d('5'), example='5 ns'),
        'Gap between transmitter pulses (the vendor calls it a debug parameter).',
        'TRAN:GAP',
    )
    damping: bool | None = _opt(_Flag(), 'Pulse damping on or off.', 'TRAN:DAMP')
    damping_gap: Decimal | None = _opt(
        _Qty('time', 'ns', _d('10'), _d('500'), _d('5'), example='30 ns'),
        'Timing gap of the damping circuit.',
        'TRAN:DAMP:GAP',
    )
    impedance: Impedance | None = _opt(
        _Choice(), "Input impedance: 'high', '200' or '1000' (ohm). Quote the numbers.", 'TRAN:IMP'
    )

    def __post_init__(self) -> None:
        _check_init(self)

    def _scpi(self) -> dict[str, str]:
        """Every header except `TRAN:ENAB`, which `Capture.to_scpi` puts last."""
        out: dict[str, str] = {}
        if self.type is not None:
            out['TRAN:TYPE'] = _TYPE_SCPI[self.type]
        if self.reverse_polarity is not None:
            out['TRAN:REV'] = _on_off(self.reverse_polarity)
        if self.voltage is not None:
            out['TRAN:PULS'] = f'{_fmt_num(self.voltage)} V'
        if self.frequency is not None:
            out['TRAN:FREQ'] = f'{_fmt_num(self.frequency)} KHZ'
        if self.periods is not None:
            out['TRAN:DUR'] = _fmt_num(self.periods)
        if self.gap is not None:
            out['TRAN:GAP'] = f'{_fmt_num(self.gap)} NS'
        if self.damping is not None:
            out['TRAN:DAMP'] = _on_off(self.damping)
        if self.damping_gap is not None:
            out['TRAN:DAMP:GAP'] = f'{_fmt_num(self.damping_gap)} NS'
        if self.impedance is not None:
            out['TRAN:IMP'] = _IMPEDANCE_SCPI[self.impedance]
        return out


@dataclass(frozen=True, kw_only=True, slots=True)
class Receiver:
    """Receiver settings."""

    gain: Decimal | None = _opt(
        _Qty('gain', 'dB', _d('0'), _d('80'), example='10 dB'),
        'Constant gain; the base level of the TGC modes.',
        'GAIN',
    )
    preamp_combined: bool | None = _opt(
        _Flag(), 'Combined preamplifier path, adds 20 dB.', 'GAIN:PRE:COMB'
    )
    preamp_split: bool | None = _opt(
        _Flag(),
        'Split preamplifier path, adds 20 dB. Whether both paths may be on at once is UNKNOWN.',
        'GAIN:PRE:SPLIT',
    )
    high_pass: HighPass | None = _opt(
        _Choice(),
        'Analog high-pass cut-off. Emitted as the device index 0..3.',
        'FILT:HPAS:IND',
    )
    tgc: Tgc | None = _opt(_Group(), 'Time-gain compensation.')

    def __post_init__(self) -> None:
        _check_init(self)

    def _scpi(self) -> dict[str, str]:
        out: dict[str, str] = {}
        if self.gain is not None:
            out['GAIN'] = _fmt_num(self.gain)
        if self.preamp_combined is not None:
            out['GAIN:PRE:COMB'] = _on_off(self.preamp_combined)
        if self.preamp_split is not None:
            out['GAIN:PRE:SPLIT'] = _on_off(self.preamp_split)
        if self.high_pass is not None:
            out['FILT:HPAS:IND'] = str(get_args(HighPass).index(self.high_pass))
        if self.tgc is not None:
            out |= self.tgc._scpi()
        return out


@dataclass(frozen=True, kw_only=True, slots=True)
class Trigger:
    """Acquisition trigger."""

    mode: TriggerMode | None = _opt(
        _Choice(), 'internal: periodic; ctp, encoder, ttl: external input.', 'TRIG:MODE'
    )
    interval: Decimal | None = _opt(
        _Qty('time', 'us', _d('100'), _d('10000000'), example='10 ms'),
        'Time between acquisitions in internal mode. Emitted in us.',
        'TRIG:INT',
    )
    delay: Decimal | None = _opt(
        _Qty('time', 'ns', _d('0'), _d('2147483647'), example='15 us'),
        'Delay between the trigger and the acquisition. Emitted in ns.',
        'TRIG:DEL',
    )

    def __post_init__(self) -> None:
        _check_init(self)

    def _scpi(self) -> dict[str, str]:
        out: dict[str, str] = {}
        if self.mode is not None:
            out['TRIG:MODE'] = _TRIGGER_SCPI[self.mode]
        if self.interval is not None:
            out['TRIG:INT'] = f'{_fmt_num(self.interval)} US'
        if self.delay is not None:
            out['TRIG:DEL'] = f'{_fmt_num(self.delay)} NS'
        return out


@dataclass(frozen=True, kw_only=True, slots=True)
class Averaging:
    """Device-side averaging."""

    count: int | None = _opt(
        _Whole(0, 8),
        'Averaging count 0..8. Semantics UNKNOWN: it may be the number of acquisitions or an '
        'exponent (PROTOCOL.md CONFLICT 10); nothing here interprets it.',
        'AVER:COUN',
    )
    constant_delay: Decimal | Literal['auto'] | None = _opt(
        _Qty('time', 'ns', _d('0'), _d('2147483647'), auto=True, example='10 us'),
        "'auto' lets the device compute the constant delay (emits AUTO ON); a time emits AUTO OFF "
        'and then the value in ns.',
        'AVER:DEL:CONS:AUTO, AVER:DEL:CONS',
    )
    random_delay: Decimal | None = _opt(
        _Qty('time', 'ns', _d('0'), _d('32767'), example='2 us'),
        'Upper bound of the random part of the pause between averaged acquisitions. Emitted in ns.',
        'AVER:DEL:RAND',
    )

    def __post_init__(self) -> None:
        _check_init(self)

    def _scpi(self) -> dict[str, str]:
        out: dict[str, str] = {}
        if self.count is not None:
            out['AVER:COUN'] = str(self.count)
        if self.constant_delay == 'auto':
            out['AVER:DEL:CONS:AUTO'] = 'ON'
        elif self.constant_delay is not None:
            out['AVER:DEL:CONS:AUTO'] = 'OFF'
            out['AVER:DEL:CONS'] = f'{_fmt_num(Decimal(self.constant_delay))} NS'
        if self.random_delay is not None:
            out['AVER:DEL:RAND'] = f'{_fmt_num(self.random_delay)} NS'
        return out


def _on_off(value: bool) -> str:
    return 'ON' if value else 'OFF'


@dataclass(frozen=True, kw_only=True, slots=True)
class Capture:
    """Everything a test condition can set. All groups and fields are optional."""

    sampling: Sampling | None = _opt(_Group(), 'A-scan sampling.')
    mode: Mode | None = _opt(_Choice(), 'master or slave operation.', 'MODE')
    pulser: Pulser | None = _opt(_Group(), 'Transmitter.')
    receiver: Receiver | None = _opt(_Group(), 'Receiver and gain.')
    trigger: Trigger | None = _opt(_Group(), 'Acquisition trigger.')
    averaging: Averaging | None = _opt(_Group(), 'Device-side averaging.')
    extra: dict[str, str] = field(
        default_factory=dict,
        metadata={
            'f': (
                _Pairs(),
                'Raw SCPI header: argument, for settings this model does not cover, e.g. '
                "{'SENS:AVER:COUN': '3'}. Appended before TRAN:ENAB and never validated.",
                '',
            )
        },
    )

    def __post_init__(self) -> None:
        _check_init(self)

    def to_scpi(self) -> dict[str, str]:
        """Header -> argument, insertion-ordered: sampling, mode, pulser, receiver, trigger,
        averaging, extra, then `TRAN:ENAB` last."""
        out: dict[str, str] = {}
        if self.sampling is not None:
            out |= self.sampling._scpi()
        if self.mode is not None:
            out['MODE'] = _MODE_SCPI[self.mode]
        if self.pulser is not None:
            out |= self.pulser._scpi()
        if self.receiver is not None:
            out |= self.receiver._scpi()
        if self.trigger is not None:
            out |= self.trigger._scpi()
        if self.averaging is not None:
            out |= self.averaging._scpi()
        out |= self.extra
        if self.pulser is not None and self.pulser.enabled is not None:
            out.pop('TRAN:ENAB', None)  # an `extra` entry must not leave it in the middle
            out['TRAN:ENAB'] = _on_off(self.pulser.enabled)
        return out

    def warnings(self) -> list[str]:
        """Things that are allowed but should be a conscious choice."""
        found: list[str] = []
        p = self.pulser
        if p is not None and p.voltage is not None and p.voltage > HIGH_VOLTAGE_WARNING_V:
            found.append(
                f'pulser.voltage {_fmt_num(p.voltage)} V is above '
                f'{_fmt_num(HIGH_VOLTAGE_WARNING_V)} V; make sure high voltage is intended'
            )
        return found

    def describe(self) -> str:
        """A one-line human summary for logs; only the fields that are set."""
        parts: list[str] = []
        if self.mode:
            parts.append(self.mode)
        if (s := self.sampling) is not None:
            bits = []
            if s.rate is not None:
                bits.append(f'{s.rate} MHz')
            if s.length is not None:
                bits.append(f'{s.length} samples')
            if bits:
                parts.append('sampling ' + ' x '.join(bits))
        if (p := self.pulser) is not None:
            bits = []
            if p.enabled is not None:
                bits.append('on' if p.enabled else 'off')
            if p.type:
                bits.append(p.type)
            if p.voltage is not None:
                bits.append(_human(p.voltage, 'voltage', 'V'))
            if p.frequency is not None:
                bits.append(_human(p.frequency, 'frequency', 'kHz'))
            if p.periods is not None:
                bits.append(f'{_fmt_num(p.periods)} periods')
            if bits:
                parts.append('pulser ' + ' '.join(bits))
        if (r := self.receiver) is not None:
            bits = []
            if r.gain is not None:
                bits.append(f'{_fmt_num(r.gain)} dB')
            if r.high_pass:
                bits.append(f'high-pass {r.high_pass}')
            if r.tgc is not None and r.tgc.mode:
                bits.append(f'tgc {r.tgc.mode}')
            if bits:
                parts.append('receiver ' + ' '.join(bits))
        if (t := self.trigger) is not None:
            bits = []
            if t.mode:
                bits.append(t.mode)
            if t.interval is not None:
                bits.append(_human(t.interval, 'time', 'us'))
            if t.delay is not None:
                bits.append('delay ' + _human(t.delay, 'time', 'ns'))
            if bits:
                parts.append('trigger ' + ' '.join(bits))
        if (a := self.averaging) is not None and a.count is not None:
            parts.append(f'averaging {a.count}')
        if self.extra:
            parts.append(f'{len(self.extra)} extra')
        return ', '.join(parts) if parts else 'no settings'


# ── introspection: keys, specs and choices come from the dataclasses above ──


@dataclass(frozen=True, slots=True)
class _Field:
    name: str
    hint: Any
    spec: _Spec
    doc: str
    scpi: str
    required: bool


@functools.cache
def _fields(cls: Any) -> tuple[_Field, ...]:
    hints = get_type_hints(cls)
    out = []
    for f in dataclasses.fields(cls):
        spec, doc, scpi = f.metadata['f']
        required = f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
        out.append(_Field(f.name, hints[f.name], spec, doc, scpi, required))
    return tuple(out)


def _choices(hint: object) -> tuple[Any, ...]:
    """The members of the `Literal` inside an annotation (`Mode | None`), or ()."""
    for arg in get_args(hint) or (hint,):
        if get_origin(arg) is Literal:
            return get_args(arg)
    return ()


def _group_class(hint: object) -> type:
    for arg in get_args(hint) or (hint,):
        if isinstance(arg, type) and dataclasses.is_dataclass(arg):
            return arg
    raise TypeError(f'no dataclass in {hint!r}')


def _qty_choices(f: _Field) -> tuple[Any, ...]:
    """Allowed values of a `_Qty` field that is restricted to a `Literal` (the sampling rate)."""
    return () if isinstance(f.spec, _Qty) and f.spec.auto else _choices(f.hint)


# ── validation shared by Python construction and the YAML parser ─────────


def _is_number(v: object) -> bool:
    return isinstance(v, (int, Decimal)) and not isinstance(v, bool)


def _bounds_text(spec: _Qty | _Whole | _Num) -> str:
    def show(x: Decimal | int) -> str:
        if isinstance(spec, _Qty):
            return _human(Decimal(x), spec.dim, spec.unit)
        return _fmt_num(x)

    text = ''
    if spec.lo is not None and spec.hi is not None:
        text = f'{show(spec.lo)} to {show(spec.hi)}'
    elif spec.lo is not None:
        text = f'at least {show(spec.lo)}'
    elif spec.hi is not None:
        text = f'at most {show(spec.hi)}'
    step = getattr(spec, 'step', None)
    if step is not None:
        text += f', multiples of {show(step)}'
    return text.strip(', ')


def _range_problem(spec: _Qty | _Whole | _Num, v: Decimal | int) -> str | None:
    def show(x: Decimal | int) -> str:
        if isinstance(spec, _Qty):
            return _human(Decimal(x), spec.dim, spec.unit)
        return _fmt_num(x)

    allowed = f' (allowed: {_bounds_text(spec)})'
    if spec.lo is not None and v < spec.lo:
        return f'below the minimum of {show(spec.lo)}{allowed}'
    if spec.hi is not None and v > spec.hi:
        return f'above the maximum of {show(spec.hi)}{allowed}'
    step = getattr(spec, 'step', None)
    if step is not None and Decimal(v) % step != 0:
        return f'not a multiple of {show(step)}{allowed}'
    return None


def _coerce(f: _Field, v: object) -> object:
    """Python-side convenience: ints and floats become `Decimal` where a quantity is expected."""
    spec = f.spec
    if isinstance(v, bool) or not isinstance(v, (int, float, Decimal)):
        return v
    if isinstance(spec, _Qty):
        if _qty_choices(f):
            as_decimal = Decimal(str(v))
            return int(as_decimal) if as_decimal == as_decimal.to_integral_value() else v
        return Decimal(str(v)) if not isinstance(v, Decimal) else v
    if isinstance(spec, _Num):
        return Decimal(str(v)) if not isinstance(v, Decimal) else v
    return v


def _value_problem(f: _Field, v: object) -> str | None:
    """What is wrong with `v` as a value of field `f`, or None. Used for typed values."""
    spec = f.spec
    if v is None:
        return 'is required'
    if isinstance(spec, _Flag):
        return None if isinstance(v, bool) else 'expected true or false'
    if isinstance(spec, _Choice):
        choices = _choices(f.hint)
        return None if v in choices else f'not one of {_join(choices)}'
    if isinstance(spec, _Qty):
        if spec.auto and v == 'auto':
            return None
        if not _is_number(v):
            return 'expected a number in ' + spec.unit
        assert isinstance(v, (int, Decimal))
        if not Decimal(v).is_finite():
            return 'not a finite number'
        choices = _qty_choices(f)
        if choices:
            if not any(Decimal(c) == v for c in choices):
                return f'not one of {", ".join(map(str, choices))} {spec.unit}'
            return None
        return _range_problem(spec, v)
    if isinstance(spec, _Whole):
        if not isinstance(v, int) or isinstance(v, bool):
            return 'expected a whole number'
        return _range_problem(spec, v)
    if isinstance(spec, _Num):
        if not _is_number(v):
            return 'expected a number'
        assert isinstance(v, (int, Decimal))
        if not Decimal(v).is_finite():
            return 'not a finite number'
        return _range_problem(spec, v)
    if isinstance(spec, _Group):
        return None if isinstance(v, _group_class(f.hint)) else 'expected a settings block'
    if isinstance(spec, _Curve):
        if not isinstance(v, tuple) or not v or not all(isinstance(p, spec.item) for p in v):
            return 'expected a non-empty tuple of points'
        i = _first_unordered(v)
        if i is not None:
            return f'point {i}: time is not later than the point before (times must increase)'
        return None
    if isinstance(spec, _Pairs):
        ok = isinstance(v, dict) and all(
            isinstance(k, str) and isinstance(x, str) for k, x in v.items()
        )
        return None if ok else 'expected a mapping of header -> string'
    return None  # pragma: no cover


def _join(choices: tuple[Any, ...]) -> str:
    return ', '.join(repr(c) for c in choices)


def _check_init(obj: object) -> None:
    """`__post_init__` of every model class: coerce numbers, check type and range."""
    cls: Any = type(obj)
    problems: list[str] = []
    values: dict[str, object] = {}
    for f in _fields(cls):
        v = getattr(obj, f.name)
        if v is not None:
            coerced = _coerce(f, v)
            if coerced is not v:
                object.__setattr__(obj, f.name, coerced)
                v = coerced
        values[f.name] = v
        if v is None and not f.required:
            continue
        msg = _value_problem(f, v)
        if msg is not None:
            problems.append(f'{cls.__name__}.{f.name} = {v!r}: {msg}')
    cross = getattr(cls, 'cross_problems', None)
    if cross is not None and not problems:
        problems.extend(f'{cls.__name__}.{name}: {msg}' for name, msg in cross(values))
    if problems:
        raise CaptureError(problems)


# ── parsing: YAML with 1-based line marks ────────────────────────────────


class _Marked(dict[str, Any]):
    """A parsed YAML mapping plus the 1-based line where it starts, and where each key and value
    were written, and the raw text of scalar values (to tell `off` from `'off'`)."""

    def __init__(self, line: int) -> None:
        super().__init__()
        self.line = line
        self.key_line: dict[str, int] = {}
        self.value_line: dict[str, int] = {}
        self.raw_text: dict[str, str] = {}


class _MarkedList(list[Any]):
    """A parsed YAML sequence plus the 1-based line where it starts."""

    def __init__(self, line: int) -> None:
        super().__init__()
        self.line = line


_BAD: Any = object()  # a value that was reported as a problem and must not be used


def _describe_raw(raw: object) -> str:
    if raw is None:
        return 'nothing'
    if isinstance(raw, bool):
        return 'a boolean'
    if isinstance(raw, dict):
        return 'a mapping'
    if isinstance(raw, list):
        return 'a list'
    return repr(raw)


class _Parser:
    """Collects every problem of one capture description; builds the `Capture` if there are none."""

    def __init__(self, source: str) -> None:
        self.source = source
        self.problems: list[tuple[int, int, str]] = []

    def add(self, line: int, path: str, message: str) -> None:
        text = f'{self.source}:{line}: {path}: {message}'
        self.problems.append((line, len(self.problems), text))

    def raise_if_any(self) -> None:
        if self.problems:
            raise CaptureError([text for _, _, text in sorted(self.problems)])

    # ── YAML to marked Python values ──

    def compose(self, text: str) -> Any:
        import yaml

        try:
            root = yaml.compose(text, Loader=yaml.SafeLoader)
        except yaml.YAMLError as exc:
            mark = getattr(exc, 'problem_mark', None)
            line = mark.line + 1 if mark is not None else 1
            detail = getattr(exc, 'problem', None) or str(exc)
            raise CaptureError([f'{self.source}:{line}: yaml: syntax error: {detail}']) from exc
        scalars = yaml.SafeLoader('')

        def walk(node: Any, path: str) -> Any:
            if isinstance(node, yaml.MappingNode):
                result = _Marked(node.start_mark.line + 1)
                for key_node, value_node in node.value:
                    key_line = key_node.start_mark.line + 1
                    if not isinstance(key_node, yaml.ScalarNode):
                        self.add(key_line, path or 'capture', 'keys must be plain text')
                        continue
                    key = str(key_node.value)  # raw text, so `on:` stays 'on'
                    key_path = f'{path}.{key}' if path else key
                    if key in result:
                        self.add(key_line, key_path, 'duplicate key')
                        continue
                    result[key] = walk(value_node, key_path)
                    result.key_line[key] = key_line
                    result.value_line[key] = value_node.start_mark.line + 1
                    if isinstance(value_node, yaml.ScalarNode):
                        result.raw_text[key] = str(value_node.value)
                return result
            if isinstance(node, yaml.SequenceNode):
                items = _MarkedList(node.start_mark.line + 1)
                items.extend(walk(child, f'{path}[{i}]') for i, child in enumerate(node.value))
                return items
            return scalars.construct_object(node, deep=True)

        if root is None:
            return _Marked(1)
        return walk(root, '')

    # ── blocks and fields ──

    def block(self, cls: type, raw: Any, path: str, line: int) -> Any:
        """Parse a mapping into `cls(...)`; None if anything in it was a problem."""
        if not isinstance(raw, _Marked):
            self.add(
                line, path or 'capture', f'expected a block of settings, got {_describe_raw(raw)}'
            )
            return None
        fields = _fields(cls)
        self.unknown_keys(raw, [f.name for f in fields], path)
        before = len(self.problems)
        kwargs: dict[str, Any] = {}
        block_line = raw.line
        for f in fields:
            field_path = f'{path}.{f.name}' if path else f.name
            if f.name not in raw:
                if f.required:
                    self.add(line if path else block_line, field_path, 'missing')
                continue
            value = self.value(f, raw, field_path)
            if value is not _BAD:
                kwargs[f.name] = value
        cross = getattr(cls, 'cross_problems', None)
        if cross is not None and len(self.problems) == before:
            for name, msg in cross(kwargs):
                where = raw.value_line.get(name, block_line)
                self.add(where, f'{path}.{name}' if path else name, msg)
        if len(self.problems) != before:
            return None
        try:
            return cls(**kwargs)
        except CaptureError as exc:  # a rule the parser does not know about; report, do not crash
            for message in exc.problems:
                self.add(line, path or 'capture', message)
            return None

    def unknown_keys(self, raw: _Marked, allowed: list[str], path: str) -> None:
        for key in raw:
            if key in allowed:
                continue
            line = raw.key_line.get(key, raw.line)
            hint = difflib.get_close_matches(key.lower(), allowed, 1)
            suggestion = (
                f", did you mean '{hint[0]}'?" if hint else f' (allowed: {", ".join(allowed)})'
            )
            self.add(line, f'{path}.{key}' if path else key, 'unknown setting' + suggestion)

    def value(self, f: _Field, container: _Marked, path: str) -> Any:
        raw = container[f.name]
        line = container.value_line.get(f.name, 0)
        key_line = container.key_line.get(f.name, line)
        text = container.raw_text.get(f.name, '')
        spec = f.spec
        if raw is None:
            self.add(line, path, 'has no value; give it one or delete the key')
            return _BAD
        if isinstance(spec, _Group):
            return self.block(_group_class(f.hint), raw, path, key_line)
        if isinstance(spec, _Curve):
            return self.curve(spec, raw, path, line)
        if isinstance(spec, _Pairs):
            return self.pairs(raw, path, line)
        if isinstance(spec, _Flag):
            if not isinstance(raw, bool):
                self.add(line, path, f'expected true or false, got {_describe_raw(raw)}')
                return _BAD
            return raw
        if isinstance(spec, _Choice):
            return self.choice(f, raw, text, path, line)
        if isinstance(spec, _Whole):
            if isinstance(raw, bool) or not isinstance(raw, int):
                self.add(line, path, f'expected a whole number, got {_describe_raw(raw)}')
                return _BAD
            return self.ranged(f, raw, raw, path, line)
        if isinstance(spec, _Num):
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                self.add(line, path, f'expected a number, got {_describe_raw(raw)}')
                return _BAD
            return self.ranged(f, Decimal(str(raw)), raw, path, line)
        if isinstance(spec, _Qty):
            converted = self.quantity(f, spec, raw, path, line)
            if converted is _BAD or converted == 'auto':
                return converted
            return self.ranged(f, converted, raw, path, line)
        raise TypeError(spec)  # pragma: no cover

    def ranged(self, f: _Field, value: Any, raw: object, path: str, line: int) -> Any:
        """Check a converted value against its range; report with the text the user wrote."""
        if isinstance(f.spec, _Qty):
            choices = _qty_choices(f)
            if choices:
                if not any(Decimal(c) == value for c in choices):
                    allowed = ', '.join(map(str, choices))
                    self.add(line, path, f'{raw!r} is not one of {allowed} {f.spec.unit}')
                    return _BAD
                return int(value)
        msg = _value_problem(f, value)
        if msg is not None:
            self.add(line, path, f'{raw!r} is {msg}')
            return _BAD
        return value

    def quantity(self, f: _Field, spec: _Qty, raw: Any, path: str, line: int) -> Any:
        if spec.auto and isinstance(raw, str) and raw.strip().lower() == 'auto':
            return 'auto'
        units = _FACTORS[spec.dim]
        names = ', '.join(_SPELLING[u] for u in units)
        example = spec.example or _DIM_EXAMPLE[spec.dim]
        auto_hint = " or 'auto'" if spec.auto else ''
        if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
            self.add(
                line,
                path,
                f'expected {_DIM_NOUN[spec.dim]} like {example!r}, got {_describe_raw(raw)}',
            )
            return _BAD
        text = str(raw)
        m = _QTY_RE.match(text)
        if m is None:
            self.add(
                line,
                path,
                f"{raw!r} is not {_DIM_NOUN[spec.dim]}; write it like '{example}'{auto_hint}",
            )
            return _BAD
        unit = _norm_unit(m['unit'])
        if not unit:
            self.add(
                line,
                path,
                f'{raw!r} has no unit; add one of {names}, e.g. {example!r}{auto_hint}',
            )
            return _BAD
        if unit not in units:
            self.add(
                line,
                path,
                f'{raw!r}: wrong unit {m["unit"]!r}, {path} takes {names}, '
                f'e.g. {example!r}{auto_hint}',
            )
            return _BAD
        factors = _FACTORS[spec.dim]
        # Decimal arithmetic: no binary-float noise such as 2.5e6 / 1e3 == 2500.0000000000005.
        return Decimal(m['num']) * factors[unit] / factors[spec.unit.lower()]

    def choice(self, f: _Field, raw: Any, text: str, path: str, line: int) -> Any:
        choices = _choices(f.hint)
        if isinstance(raw, bool):
            written = text or str(raw).lower()
            self.add(
                line,
                path,
                f"unquoted {written} is read by YAML as a boolean; quote it: '{written}'. "
                f'Allowed: {_join(choices)}',
            )
            return _BAD
        if isinstance(raw, (int, float)):
            if any(_norm_choice(str(c)) == _norm_choice(text or str(raw)) for c in choices):
                self.add(
                    line,
                    path,
                    f"{raw!r} is a number, but this setting takes text; quote it: '{raw}'",
                )
            else:
                self.add(line, path, f'{raw!r} is not one of {_join(choices)}')
            return _BAD
        if not isinstance(raw, str):
            self.add(line, path, f'expected one of {_join(choices)}, got {_describe_raw(raw)}')
            return _BAD
        by_norm = {_norm_choice(str(c)): c for c in choices}
        found = by_norm.get(_norm_choice(raw))
        if found is None:
            close = difflib.get_close_matches(_norm_choice(raw), list(by_norm), 1, 0.8)
            hint = f", did you mean '{by_norm[close[0]]}'?" if close else ''
            self.add(line, path, f'{raw!r} is not one of {_join(choices)}{hint}')
            return _BAD
        return found

    def curve(self, spec: _Curve, raw: Any, path: str, line: int) -> Any:
        if not isinstance(raw, list):
            self.add(line, path, f'expected a list of points, got {_describe_raw(raw)}')
            return _BAD
        if not raw:
            self.add(line, path, 'needs at least one point')
            return _BAD
        before = len(self.problems)
        points = []
        for i, item in enumerate(raw):
            item_line = item.line if isinstance(item, (_Marked, _MarkedList)) else line
            point = self.block(spec.item, item, f'{path}[{i}]', item_line)
            if point is not None:
                points.append(point)
        if len(self.problems) != before:
            return _BAD
        bad = _first_unordered(tuple(points))
        if bad is not None:
            item = raw[bad]
            where = item.value_line.get('time', item.line) if isinstance(item, _Marked) else line
            self.add(
                where,
                f'{path}[{bad}].time',
                f'{_human(points[bad].time, "time", "us")} is not later than the point before '
                f'({_human(points[bad - 1].time, "time", "us")}); times must strictly increase',
            )
            return _BAD
        return tuple(points)

    def pairs(self, raw: Any, path: str, line: int) -> Any:
        if not isinstance(raw, _Marked):
            self.add(
                line, path, f'expected a mapping of header: argument, got {_describe_raw(raw)}'
            )
            return _BAD
        out: dict[str, str] = {}
        failed = False
        for key, value in raw.items():
            key_line = raw.key_line.get(key, line)
            value_line = raw.value_line.get(key, key_line)
            item_path = f'{path}.{key}'
            if not key.strip() or re.search(r'\s', key):
                self.add(
                    key_line,
                    item_path,
                    'a header is one word without spaces; put the argument in the value',
                )
                failed = True
                continue
            if isinstance(value, bool):
                written = raw.raw_text.get(key, str(value).lower())
                self.add(
                    value_line,
                    item_path,
                    f"unquoted {written} is read by YAML as a boolean; quote it: '{written}'",
                )
                failed = True
            elif isinstance(value, (int, float)):
                out[key] = _fmt_num(Decimal(str(value)))
            elif isinstance(value, str) and not re.search(r'[\r\n]', value):
                out[key] = value
            else:
                self.add(
                    value_line, item_path, f'expected one line of text, got {_describe_raw(value)}'
                )
                failed = True
        return _BAD if failed else out


def parse_capture(text: str, *, source: str = '<string>') -> Capture:
    """Parse and validate YAML text. Raises `CaptureError` listing every problem in file order."""
    parser = _Parser(source)
    data = parser.compose(text)
    if not isinstance(data, dict):
        parser.add(
            data.line if isinstance(data, _MarkedList) else 1,
            'capture',
            f'expected a mapping of groups, got {_describe_raw(data)}',
        )
        parser.raise_if_any()
    capture = parser.block(Capture, data, '', 1)
    parser.raise_if_any()
    assert isinstance(capture, Capture)
    return capture


def load_capture(path: str | Path) -> Capture:
    """Read, parse and validate a capture file. Problems are labelled with the path as given."""
    path = Path(path)
    return parse_capture(path.read_text(encoding='utf-8'), source=str(path))


# ── applying to a plug ───────────────────────────────────────────────────


class _SetupPlug(Protocol):
    """What `apply_capture` needs from a plug."""

    def apply_setup(self, setup: dict[str, str], /) -> object: ...

    def reset(self) -> object: ...


def apply_capture(
    plug: _SetupPlug, capture: Capture | str | Path, *, reset: bool = False
) -> Capture:
    """Load and validate `capture` (a `Capture` or a file path) before sending anything, optionally
    reset the instrument, then `plug.apply_setup(capture.to_scpi())`. Returns the `Capture`.

    `reset` defaults to False because what `*RST` restores is UNKNOWN on this device.
    """
    loaded = capture if isinstance(capture, Capture) else load_capture(capture)
    for warning in loaded.warnings():
        _LOG.warning('%s', warning)
    setup = loaded.to_scpi()
    if reset:
        plug.reset()
    plug.apply_setup(setup)
    return loaded


# ── schema generation: same choices, units and ranges, as data ───────────


def _json_num(x: Decimal | int) -> int | float:
    d = Decimal(x)
    return int(d) if d == d.to_integral_value() else float(d)


def _describe_field(f: _Field) -> str:
    text = f.doc
    if isinstance(f.spec, (_Qty, _Whole, _Num)) and not _qty_choices(f):
        bounds = _bounds_text(f.spec)
        if bounds:
            text += f' Allowed: {bounds}.'
    if f.scpi:
        text += f' SCPI: {f.scpi}.'
    return text


def _field_schema(f: _Field) -> dict[str, Any]:
    spec = f.spec
    schema: dict[str, Any]
    if isinstance(spec, _Flag):
        schema = {'type': 'boolean'}
    elif isinstance(spec, _Choice):
        schema = {'enum': list(_choices(f.hint))}
    elif isinstance(spec, _Qty):
        ref = {'$ref': f'#/$defs/{spec.dim}'}
        if spec.auto:
            schema = {'anyOf': [{'const': 'auto'}, ref]}
        elif _qty_choices(f):
            unit = spec.unit
            schema = ref | {'examples': [f'{c} {unit}' for c in _qty_choices(f)]}
        else:
            schema = ref | {'examples': [spec.example]}
    elif isinstance(spec, _Whole):
        schema = {'type': 'integer'}
        if spec.lo is not None:
            schema['minimum'] = spec.lo
        if spec.hi is not None:
            schema['maximum'] = spec.hi
    elif isinstance(spec, _Num):
        schema = {'type': 'number'}
        if spec.lo is not None:
            schema['minimum'] = _json_num(spec.lo)
        if spec.hi is not None:
            schema['maximum'] = _json_num(spec.hi)
        if spec.step is not None:
            schema['multipleOf'] = _json_num(spec.step)
    elif isinstance(spec, _Group):
        schema = _block_schema(_group_class(f.hint))
    elif isinstance(spec, _Curve):
        schema = {'type': 'array', 'minItems': 1, 'items': _block_schema(spec.item)}
    else:
        schema = {'type': 'object', 'additionalProperties': {'type': ['string', 'number']}}
    return schema | {'description': _describe_field(f)}


def _block_schema(cls: type) -> dict[str, Any]:
    fields = _fields(cls)
    block: dict[str, Any] = {
        'type': 'object',
        'additionalProperties': False,
        'properties': {f.name: _field_schema(f) for f in fields},
    }
    required = [f.name for f in fields if f.required]
    if required:
        block['required'] = required
    doc = (cls.__doc__ or '').strip()
    if doc:
        block['description'] = doc
    return block


def capture_schema() -> dict[str, Any]:
    """JSON Schema (draft 2020-12) of the YAML file, derived from the dataclasses."""
    defs = {
        dim: {
            'type': 'string',
            'pattern': _quantity_pattern(dim),
            'description': _DIM_DOC[dim].format(example=repr(_DIM_EXAMPLE[dim])),
        }
        for dim in _FACTORS
    }
    return {
        '$schema': 'https://json-schema.org/draft/2020-12/schema',
        'title': 'ACS A1580 capture conditions',
        'description': 'Settings for the pulser-receiver; every group and field is optional.',
        '$defs': defs,
    } | _block_schema(Capture)


# ── command line ──────────────────────────────────────────────────────────


def _main_schema(write: bool) -> int:
    text = json.dumps(capture_schema(), indent=2, ensure_ascii=False) + '\n'
    if write:
        target = Path.cwd() / 'capture.schema.json'
        target.write_text(text, encoding='utf-8')
        print(f'wrote {target}')
    else:
        sys.stdout.write(text)
    return 0


def _main_check(files: list[str]) -> int:
    failed = False
    for name in files:
        try:
            cap = load_capture(name)
        except CaptureError as exc:
            print('\n'.join(exc.problems), file=sys.stderr)
            failed = True
        except OSError as exc:
            print(f'{name}: cannot read: {exc.strerror or exc}', file=sys.stderr)
            failed = True
        else:
            for warning in cap.warnings():
                print(f'{name}: warning: {warning}', file=sys.stderr)
            print(f'OK {name}')
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='python -m a1580_openhtf.capture', description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p_schema = sub.add_parser('schema', help='print the JSON schema')
    p_schema.add_argument(
        '--write', action='store_true', help='write ./capture.schema.json instead'
    )
    p_check = sub.add_parser('check', help='validate capture files; exit 1 on problems')
    p_check.add_argument('files', nargs='+')
    args = parser.parse_args(argv)
    if args.command == 'schema':
        return _main_schema(args.write)
    return _main_check(args.files)


if __name__ == '__main__':
    sys.exit(main())
