"""Tests for capture.py: the typed model, the YAML front end, the schema, apply_capture, the CLI.

Numbered banners mirror SPEC-capture.md section 2. Provenance: structure adapted from
rigol-dho-openhtf `tests/test_capture.py`.
"""

import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, get_args

import jsonschema
import pytest
import yaml

from a1580_openhtf import capture as capture_module
from a1580_openhtf.capture import (
    Averaging,
    Capture,
    CaptureError,
    Pulser,
    Receiver,
    Sampling,
    Tgc,
    TgcLinear,
    TgcPoint,
    Trigger,
    apply_capture,
    capture_schema,
    load_capture,
    parse_capture,
)

ROOT = Path(__file__).resolve().parent.parent
VENDOR_YAML = ROOT / 'captures' / 'vendor_example.yaml'
BROKEN_YAML = ROOT / 'tests' / 'data' / 'broken.yaml'
SCHEMA_JSON = ROOT / 'capture.schema.json'


def _write(tmp_path: Path, text: str, name: str = 'capture.yaml') -> Path:
    path = tmp_path / name
    path.write_text(text, encoding='utf-8')
    return path


# ── 1. vendor_example.yaml and to_scpi() ─────────────────────────────────

VENDOR_SCPI = {
    'FREQ': '100 MHZ',
    'DATA:LENG': '8192',
    'MODE': 'MASTer',
    'TRAN:TYPE': 'SINGle',
    'TRAN:REV': 'OFF',
    'TRAN:PULS': '20 V',
    'TRAN:FREQ': '2500 KHZ',
    'TRAN:DUR': '1',
    'TRAN:GAP': '5 NS',
    'TRAN:DAMP': 'ON',
    'TRAN:DAMP:GAP': '30 NS',
    'TRAN:IMP': '200',
    'GAIN': '10',
    'GAIN:TGC:MODE': 'OFF',
    'TRIG:MODE': 'INTernal',
    'TRIG:INT': '100000 US',
    'TRIG:DEL': '0 NS',
    'AVER:COUN': '0',
    'AVER:DEL:CONS:AUTO': 'ON',
    'AVER:DEL:RAND': '2000 NS',
    'TRAN:ENAB': 'ON',
}


def test_vendor_example_loads_and_emits_the_expected_ordered_dict() -> None:
    capture = load_capture(VENDOR_YAML)
    scpi = capture.to_scpi()
    assert scpi == VENDOR_SCPI
    assert list(scpi) == list(VENDOR_SCPI)  # dict equality ignores order; this does not
    assert list(scpi)[-1] == 'TRAN:ENAB'


def test_vendor_example_matches_the_same_capture_written_in_python() -> None:
    expected = Capture(
        sampling=Sampling(rate=100, length=8192),
        mode='master',
        pulser=Pulser(
            enabled=True,
            type='single',
            reverse_polarity=False,
            voltage=20,  # type: ignore[arg-type]  # ints are coerced to Decimal
            frequency=Decimal('2500'),
            periods=1,  # type: ignore[arg-type]
            gap=Decimal(5),
            damping=True,
            damping_gap=Decimal(30),
            impedance='200',
        ),
        receiver=Receiver(gain=Decimal(10), tgc=Tgc(mode='off')),
        trigger=Trigger(mode='internal', interval=Decimal(100000), delay=Decimal(0)),
        averaging=Averaging(count=0, constant_delay='auto', random_delay=Decimal(2000)),
    )
    assert load_capture(VENDOR_YAML) == expected


def test_vendor_example_has_no_warnings_and_a_description() -> None:
    capture = load_capture(VENDOR_YAML)
    assert capture.warnings() == []
    text = capture.describe()
    assert '\n' not in text
    assert '20 V' in text and '2.5 MHz' in text and '100 MHz' in text


def test_empty_capture_emits_nothing() -> None:
    assert parse_capture('').to_scpi() == {}
    assert parse_capture('# only a comment\n').to_scpi() == {}
    assert parse_capture('pulser: {}\n').to_scpi() == {}
    assert Capture().describe() == 'no settings'


def test_a_file_can_set_only_what_it_cares_about() -> None:
    assert parse_capture('receiver:\n  gain: 30 dB\n').to_scpi() == {'GAIN': '30'}


FULL_YAML = """\
sampling: {rate: 25 MHz, length: 2048}
mode: slave
pulser:
  enabled: false
  type: dual
  reverse_polarity: true
  voltage: 50 V
  frequency: 5 MHz
  periods: 2.5
  gap: 10 ns
  damping: false
  damping_gap: 15 ns
  impedance: high
receiver:
  gain: 40 dB
  preamp_combined: true
  preamp_split: false
  high_pass: 500 kHz
  tgc:
    mode: arbitrary
    linear: {offset: 20 us, slope: 0.1 dB/us}
    arbitrary:
      - {time: 10 us, gain: 5 dB}
      - {time: 20 us, gain: 10 dB}
trigger: {mode: encoder, interval: 10 ms, delay: 15 us}
averaging: {count: 4, constant_delay: 10 us, random_delay: 2 us}
extra:
  'SENS:AVER:COUN': 3
  FOO: bar
"""

FULL_SCPI = {
    'FREQ': '25 MHZ',
    'DATA:LENG': '2048',
    'MODE': 'SLAVe',
    'TRAN:TYPE': 'DUAL',
    'TRAN:REV': 'ON',
    'TRAN:PULS': '50 V',
    'TRAN:FREQ': '5000 KHZ',
    'TRAN:DUR': '2.5',
    'TRAN:GAP': '10 NS',
    'TRAN:DAMP': 'OFF',
    'TRAN:DAMP:GAP': '15 NS',
    'TRAN:IMP': 'HIGH',
    'GAIN': '40',
    'GAIN:PRE:COMB': 'ON',
    'GAIN:PRE:SPLIT': 'OFF',
    'FILT:HPAS:IND': '2',
    'GAIN:TGC:LIN': '20.0, 0.1',
    'GAIN:TGC:ARB': '10,5,20,10',
    'GAIN:TGC:MODE': 'ARBitrary',
    'TRIG:MODE': 'ENCoder',
    'TRIG:INT': '10000 US',
    'TRIG:DEL': '15000 NS',
    'AVER:COUN': '4',
    'AVER:DEL:CONS:AUTO': 'OFF',
    'AVER:DEL:CONS': '10000 NS',
    'AVER:DEL:RAND': '2000 NS',
    'SENS:AVER:COUN': '3',
    'FOO': 'bar',
    'TRAN:ENAB': 'OFF',
}


def test_every_field_emits_its_header_in_the_specified_order() -> None:
    scpi = parse_capture(FULL_YAML).to_scpi()
    assert scpi == FULL_SCPI
    assert list(scpi) == list(FULL_SCPI)


def test_tgc_mode_follows_its_curve_and_extra_precedes_the_enable() -> None:
    keys = list(parse_capture(FULL_YAML).to_scpi())
    assert keys.index('GAIN:TGC:MODE') > keys.index('GAIN:TGC:ARB')
    assert keys.index('FOO') < keys.index('TRAN:ENAB') == len(keys) - 1


def test_constant_delay_value_emits_auto_off_first() -> None:
    cap = parse_capture('averaging:\n  constant_delay: 1.5 us\n')
    assert cap.to_scpi() == {'AVER:DEL:CONS:AUTO': 'OFF', 'AVER:DEL:CONS': '1500 NS'}


def test_an_extra_enable_cannot_end_up_in_the_middle() -> None:
    cap = parse_capture("pulser: {enabled: true}\nextra: {'TRAN:ENAB': 'OFF', FOO: '1'}\n")
    assert list(cap.to_scpi()) == ['FOO', 'TRAN:ENAB']
    assert cap.to_scpi()['TRAN:ENAB'] == 'ON'


def test_every_header_the_model_emits_is_documented_in_the_schema() -> None:
    schema_text = json.dumps(capture_schema())
    for header in parse_capture(FULL_YAML).to_scpi():
        if header in ('SENS:AVER:COUN', 'FOO'):
            continue  # extra
        assert header in schema_text, header


def test_the_scpi_spelling_tables_cover_exactly_the_literal_choices() -> None:
    module = capture_module
    assert set(module._MODE_SCPI) == set(get_args(module.Mode))
    assert set(module._TYPE_SCPI) == set(get_args(module.PulserType))
    assert set(module._IMPEDANCE_SCPI) == set(get_args(module.Impedance))
    assert set(module._TGC_SCPI) == set(get_args(module.TgcMode))
    assert set(module._TRIGGER_SCPI) == set(get_args(module.TriggerMode))


# ── 1b. quantity spellings, exactness, warnings ──────────────────────────


@pytest.mark.parametrize(
    'text',
    ['2500 KHZ', '2500 kHz', '2500 khz', '2.5 MHz', '2.5 mhz', '2.5MHz', '2500000 Hz', '2.5e3 kHz'],
)
def test_frequency_spellings_and_case(text: str) -> None:
    cap = parse_capture(f'pulser:\n  frequency: {text}\n')
    assert cap.to_scpi() == {'TRAN:FREQ': '2500 KHZ'}


@pytest.mark.parametrize(
    'text', ['0.1 s', '100 ms', '100 MS', '100000 us', '100000 µs', '1e5 us', '100000000 ns']
)
def test_time_spellings_and_case(text: str) -> None:
    assert parse_capture(f'trigger:\n  interval: {text}\n').to_scpi() == {'TRIG:INT': '100000 US'}


def test_slope_and_gain_spellings() -> None:
    cap = parse_capture('receiver:\n  tgc:\n    linear: {offset: 20 us, slope: 0.1 DB / US}\n')
    assert cap.receiver is not None and cap.receiver.tgc is not None
    assert cap.receiver.tgc.linear == TgcLinear(offset=Decimal(20), slope=Decimal('0.1'))


@pytest.mark.parametrize(
    'text,expected',
    [('1.1 ms', '1100 US'), ('0.3 ms', '300 US'), ('250.5 us', '250.5 US'), ('2.2 ms', '2200 US')],
)
def test_quantities_are_exact_decimals(text: str, expected: str) -> None:
    """No binary-float noise such as 0.30000000000000004."""
    assert parse_capture(f'trigger:\n  interval: {text}\n').to_scpi() == {'TRIG:INT': expected}


def test_voltage_above_fifty_is_allowed_but_warned() -> None:
    cap = parse_capture('pulser:\n  voltage: 100 V\n')
    assert cap.to_scpi() == {'TRAN:PULS': '100 V'}
    [warning] = cap.warnings()
    assert 'pulser.voltage' in warning and '100 V' in warning
    assert parse_capture('pulser:\n  voltage: 50 V\n').warnings() == []


# ── 2. one mistake per minimal YAML string ───────────────────────────────

# (name, yaml, key_path, message_fragment, line). Each snippet holds exactly one mistake, so the
# test also asserts that this is the only problem reported.

MISTAKES: list[tuple[str, str, str, str, int]] = [
    (
        'unknown key with suggestion',
        'pulser:\n  voltge: 20 V\n',
        'pulser.voltge',
        "did you mean 'voltage'",
        2,
    ),
    (
        'unknown group with suggestion',
        'pulsr:\n  voltage: 20 V\n',
        'pulsr',
        "did you mean 'pulser'",
        1,
    ),
    ('unknown key without a close match', 'zzz: 1\n', 'zzz', 'allowed:', 1),
    (
        'bare number where a quantity is expected',
        'pulser:\n  frequency: 2.5\n',
        'pulser.frequency',
        'has no unit; add one of Hz, kHz, MHz',
        2,
    ),
    ('bare number as text', "receiver:\n  gain: '10'\n", 'receiver.gain', 'has no unit', 2),
    ('wrong unit', 'pulser:\n  frequency: 2.5 V\n', 'pulser.frequency', 'wrong unit', 2),
    (
        'time where a gain is expected',
        'receiver:\n  gain: 10 us\n',
        'receiver.gain',
        'wrong unit',
        2,
    ),
    (
        'not a quantity at all',
        'pulser:\n  voltage: lots\n',
        'pulser.voltage',
        'is not a voltage',
        2,
    ),
    (
        'voltage out of range',
        'pulser:\n  voltage: 120 V\n',
        'pulser.voltage',
        'above the maximum of 100 V',
        2,
    ),
    (
        'voltage below the range',
        'pulser:\n  voltage: 0 V\n',
        'pulser.voltage',
        'below the minimum of 5 V',
        2,
    ),
    (
        'voltage not a multiple of 5',
        'pulser:\n  voltage: 12 V\n',
        'pulser.voltage',
        'not a multiple of 5 V',
        2,
    ),
    (
        'periods not a multiple of 0.5',
        'pulser:\n  periods: 0.7\n',
        'pulser.periods',
        'not a multiple of 0.5',
        2,
    ),
    (
        'gap not a multiple of 5',
        'pulser:\n  gap: 7 ns\n',
        'pulser.gap',
        'not a multiple of 5 ns',
        2,
    ),
    (
        'damping gap below its minimum',
        'pulser:\n  damping_gap: 5 ns\n',
        'pulser.damping_gap',
        'below the minimum of 10 ns',
        2,
    ),
    (
        'gain out of range',
        'receiver:\n  gain: 81 dB\n',
        'receiver.gain',
        'above the maximum of 80 dB',
        2,
    ),
    (
        'sampling rate not offered',
        'sampling:\n  rate: 3 MHz\n',
        'sampling.rate',
        'not one of 1, 2, 5, 10, 25, 50, 100 MHz',
        2,
    ),
    (
        'sampling rate in the wrong unit',
        'sampling:\n  rate: 3 ms\n',
        'sampling.rate',
        'wrong unit',
        2,
    ),
    (
        'length too large',
        'sampling:\n  length: 40000\n',
        'sampling.length',
        'above the maximum of 36864',
        2,
    ),
    (
        'length too small',
        'sampling:\n  length: 512\n',
        'sampling.length',
        'below the minimum of 1024',
        2,
    ),
    (
        'length given as text',
        "sampling:\n  length: '8192'\n",
        'sampling.length',
        'expected a whole number',
        2,
    ),
    (
        'length given as a float',
        'sampling:\n  length: 8192.5\n',
        'sampling.length',
        'expected a whole number',
        2,
    ),
    (
        'averaging count too high',
        'averaging:\n  count: 9\n',
        'averaging.count',
        'above the maximum of 8',
        2,
    ),
    (
        'constant delay neither auto nor a time',
        'averaging:\n  constant_delay: soon\n',
        'averaging.constant_delay',
        "or 'auto'",
        2,
    ),
    (
        'random delay too large',
        'averaging:\n  random_delay: 40 us\n',
        'averaging.random_delay',
        'above the maximum of 32.767 us',
        2,
    ),
    (
        'tgc linear mode without the curve',
        'receiver:\n  tgc:\n    mode: linear\n',
        'receiver.tgc.mode',
        "needs a 'linear' curve",
        3,
    ),
    (
        'tgc arbitrary mode without the curve',
        'receiver:\n  tgc:\n    mode: arbitrary\n',
        'receiver.tgc.mode',
        "needs an 'arbitrary' list",
        3,
    ),
    (
        'tgc linear curve without a slope',
        'receiver:\n  tgc:\n    linear:\n      offset: 20 us\n',
        'receiver.tgc.linear.slope',
        'missing',
        3,
    ),
    (
        'arbitrary times not increasing',
        'receiver:\n  tgc:\n    arbitrary:\n      - {time: 10 us, gain: 5 dB}\n'
        '      - {time: 10 us, gain: 7 dB}\n',
        'receiver.tgc.arbitrary[1].time',
        'strictly increase',
        5,
    ),
    (
        'arbitrary curve empty',
        'receiver:\n  tgc:\n    arbitrary: []\n',
        'receiver.tgc.arbitrary',
        'at least one point',
        3,
    ),
    (
        'arbitrary curve is not a list',
        'receiver:\n  tgc:\n    arbitrary: 5\n',
        'receiver.tgc.arbitrary',
        'expected a list of points',
        3,
    ),
    (
        'duplicate key',
        'pulser:\n  voltage: 20 V\n  voltage: 25 V\n',
        'pulser.voltage',
        'duplicate key',
        3,
    ),
    ('duplicate group', 'mode: master\nmode: slave\n', 'mode', 'duplicate key', 2),
    (
        'unquoted off for a choice',
        'receiver:\n  tgc:\n    mode: off\n',
        'receiver.tgc.mode',
        "quote it: 'off'",
        3,
    ),
    ('unquoted no for a choice', 'mode: no\n', 'mode', 'quote it', 1),
    (
        'boolean field given text',
        "pulser:\n  enabled: 'yes'\n",
        'pulser.enabled',
        'expected true or false',
        2,
    ),
    (
        'boolean field given a number',
        'pulser:\n  damping: 1\n',
        'pulser.damping',
        'expected true or false',
        2,
    ),
    (
        'impedance given as a number',
        'pulser:\n  impedance: 200\n',
        'pulser.impedance',
        "quote it: '200'",
        2,
    ),
    (
        'impedance not offered',
        "pulser:\n  impedance: '50'\n",
        'pulser.impedance',
        "not one of 'high', '200', '1000'",
        2,
    ),
    ('unknown trigger mode', 'trigger:\n  mode: external\n', 'trigger.mode', 'not one of', 2),
    (
        'trigger mode typo suggests',
        'trigger:\n  mode: interal\n',
        'trigger.mode',
        "did you mean 'internal'",
        2,
    ),
    (
        'high-pass not offered',
        'receiver:\n  high_pass: 2 kHz\n',
        'receiver.high_pass',
        'not one of',
        2,
    ),
    (
        'negative delay',
        'trigger:\n  delay: -5 ns\n',
        'trigger.delay',
        'below the minimum of 0 ns',
        2,
    ),
    (
        'interval below 100 us',
        'trigger:\n  interval: 50 us\n',
        'trigger.interval',
        'below the minimum of 100 us',
        2,
    ),
    (
        'interval above 10 s',
        'trigger:\n  interval: 11 s\n',
        'trigger.interval',
        'above the maximum of 10 s',
        2,
    ),
    (
        'pulser frequency too low',
        'pulser:\n  frequency: 5 kHz\n',
        'pulser.frequency',
        'below the minimum of 10 kHz',
        2,
    ),
    ('group is not a block', 'pulser: 5\n', 'pulser', 'expected a block of settings', 1),
    ('value is empty', 'pulser:\n  voltage:\n', 'pulser.voltage', 'has no value', 2),
    ('top level is a list', '- 1\n- 2\n', 'capture', 'expected a mapping', 1),
    ('extra header with a space', 'extra:\n  FOO BAR: x\n', 'extra.FOO BAR', 'one word', 2),
    ('extra value is a YAML boolean', 'extra:\n  FOO: on\n', 'extra.FOO', "quote it: 'on'", 2),
    ('extra is not a mapping', 'extra: [1]\n', 'extra', 'expected a mapping', 1),
]


def test_there_are_enough_mistake_cases() -> None:
    assert len(MISTAKES) >= 15
    assert len({m[0] for m in MISTAKES}) == len(MISTAKES)


@pytest.mark.parametrize('name,text,path,fragment,line', MISTAKES, ids=[m[0] for m in MISTAKES])
def test_mistake(tmp_path: Path, name: str, text: str, path: str, fragment: str, line: int) -> None:
    file = _write(tmp_path, text)
    with pytest.raises(CaptureError) as exc_info:
        load_capture(file)
    problems = exc_info.value.problems
    assert len(problems) == 1, problems
    assert problems[0].startswith(f'{file}:{line}: {path}: '), problems[0]
    assert fragment in problems[0], problems[0]


def test_capture_error_is_a_value_error_and_str_lists_every_problem() -> None:
    with pytest.raises(ValueError) as exc_info:
        parse_capture('mode: x\nsampling:\n  length: 1\n', source='demo.yaml')
    assert isinstance(exc_info.value, CaptureError)
    assert str(exc_info.value) == '\n'.join(exc_info.value.problems)
    assert [p.split(':')[1] for p in exc_info.value.problems] == ['1', '3']
    assert all(p.startswith('demo.yaml:') for p in exc_info.value.problems)


def test_yaml_syntax_error_is_a_problem_with_a_line() -> None:
    with pytest.raises(CaptureError) as exc_info:
        parse_capture('pulser:\n  voltage: [20\n  gap: 5 ns\n', source='bad.yaml')
    [problem] = exc_info.value.problems
    assert problem.startswith('bad.yaml:') and 'syntax error' in problem


def test_two_documents_in_one_file_are_a_problem() -> None:
    with pytest.raises(CaptureError):
        parse_capture('mode: master\n---\nmode: slave\n')


def test_unquoted_off_message_shows_what_was_written() -> None:
    with pytest.raises(CaptureError) as exc_info:
        parse_capture('receiver:\n  tgc:\n    mode: Off\n')
    assert 'unquoted Off' in exc_info.value.problems[0]


@pytest.mark.parametrize('text', ['Master', 'MASTER', ' master '])
def test_choices_are_case_insensitive(text: str) -> None:
    assert parse_capture(f"mode: '{text}'\n").mode == 'master'


def test_quoted_off_is_a_choice() -> None:
    cap = parse_capture("receiver:\n  tgc:\n    mode: 'off'\n")
    assert cap.to_scpi() == {'GAIN:TGC:MODE': 'OFF'}


def test_high_pass_index_follows_the_documented_order() -> None:
    for index, text in enumerate(['1 kHz', '100 kHz', '500 kHz', '1 MHz']):
        cap = parse_capture(f"receiver:\n  high_pass: '{text}'\n")
        assert cap.to_scpi() == {'FILT:HPAS:IND': str(index)}
    # spacing and case are forgiven
    assert parse_capture("receiver:\n  high_pass: '100khz'\n").to_scpi() == {'FILT:HPAS:IND': '1'}


@pytest.mark.parametrize('rate', [1, 2, 5, 10, 25, 50, 100])
def test_every_listed_sampling_rate_is_accepted(rate: int) -> None:
    assert parse_capture(f'sampling:\n  rate: {rate} MHz\n').to_scpi() == {'FREQ': f'{rate} MHZ'}
    assert parse_capture(f'sampling:\n  rate: {rate * 1000} kHz\n').to_scpi() == {
        'FREQ': f'{rate} MHZ'
    }


@pytest.mark.parametrize(
    'field_yaml,header,expected',
    [
        ('sampling: {length: 1024}', 'DATA:LENG', '1024'),
        ('sampling: {length: 36864}', 'DATA:LENG', '36864'),
        ('pulser: {voltage: 5 V}', 'TRAN:PULS', '5 V'),
        ('pulser: {frequency: 20 MHz}', 'TRAN:FREQ', '20000 KHZ'),
        ('pulser: {frequency: 10 kHz}', 'TRAN:FREQ', '10 KHZ'),
        ('pulser: {periods: 0.5}', 'TRAN:DUR', '0.5'),
        ('pulser: {periods: 16}', 'TRAN:DUR', '16'),
        ('pulser: {gap: 0 ns}', 'TRAN:GAP', '0 NS'),
        ('pulser: {gap: 500 ns}', 'TRAN:GAP', '500 NS'),
        ('pulser: {damping_gap: 10 ns}', 'TRAN:DAMP:GAP', '10 NS'),
        ('receiver: {gain: 0 dB}', 'GAIN', '0'),
        ('receiver: {gain: 80 dB}', 'GAIN', '80'),
        ('receiver: {gain: 12.5 dB}', 'GAIN', '12.5'),
        ('trigger: {interval: 100 us}', 'TRIG:INT', '100 US'),
        ('trigger: {interval: 10 s}', 'TRIG:INT', '10000000 US'),
        ('trigger: {delay: 2147483647 ns}', 'TRIG:DEL', '2147483647 NS'),
        ('averaging: {count: 8}', 'AVER:COUN', '8'),
        ('averaging: {random_delay: 32767 ns}', 'AVER:DEL:RAND', '32767 NS'),
        ('averaging: {constant_delay: 0 ns}', 'AVER:DEL:CONS', '0 NS'),
    ],
)
def test_range_limits_are_inclusive(field_yaml: str, header: str, expected: str) -> None:
    assert parse_capture(field_yaml + '\n').to_scpi()[header] == expected


def test_unvalidated_fields_accept_any_value_of_the_right_type() -> None:
    """tgc offset, slope and arbitrary points have no documented range (PROTOCOL.md UNKNOWN)."""
    cap = parse_capture(
        'receiver:\n  tgc:\n    linear: {offset: 500 ms, slope: -3 dB/us}\n'
        '    arbitrary:\n      - {time: 0 us, gain: -20 dB}\n      - {time: 1 s, gain: 200 dB}\n'
    )
    assert cap.to_scpi() == {
        'GAIN:TGC:LIN': '500000.0, -3.0',
        'GAIN:TGC:ARB': '0,-20,1000000,200',
    }


# ── 3. broken.yaml: all problems in one CaptureError, in file order ──────

BROKEN_EXPECTED = [
    (2, 'sampling.rate', 'not one of 1, 2, 5, 10, 25, 50, 100 MHz'),
    (3, 'sampling.length', 'above the maximum of 36864'),
    (4, 'mode', "did you mean 'master'"),
    (6, 'pulser.voltage', 'not a multiple of 5 V'),
    (7, 'pulser.frequency', 'has no unit'),
    (8, 'pulser.periods', 'above the maximum of 16'),
    (9, 'pulser.impedance', "quote it: '200'"),
    (11, 'pulser.gap', 'duplicate key'),
    (13, 'receiver.gain', 'above the maximum of 80 dB'),
    (14, 'receiver.high_pass', 'not one of'),
    (16, 'receiver.tgc.mode', "needs a 'linear' curve"),
    (18, 'trigger.mdoe', "did you mean 'mode'"),
    (20, 'trigger.delay', 'below the minimum of 0 ns'),
    (22, 'averaging.count', 'above the maximum of 8'),
    (23, 'averaging.constant_delay', "or 'auto'"),
    (25, 'extra.FOO BAR', 'one word'),
]


def test_broken_yaml_reports_all_problems_in_file_order_with_lines() -> None:
    with pytest.raises(CaptureError) as exc_info:
        load_capture(BROKEN_YAML)
    problems = exc_info.value.problems
    assert len(problems) == len(BROKEN_EXPECTED) >= 9
    for problem, (line, path, fragment) in zip(problems, BROKEN_EXPECTED, strict=True):
        assert problem.startswith(f'{BROKEN_YAML}:{line}: {path}: '), problem
        assert fragment in problem, problem
    lines = [int(p.removeprefix(f'{BROKEN_YAML}:').split(':', 1)[0]) for p in problems]
    assert lines == sorted(lines)


def test_the_comments_in_broken_yaml_point_at_their_own_lines() -> None:
    """Each deliberate mistake has a comment on its line; keep the two in step."""
    text = BROKEN_YAML.read_text(encoding='utf-8').splitlines()
    for line, _path, _fragment in BROKEN_EXPECTED:
        assert '#' in text[line - 1], f'line {line}: {text[line - 1]!r}'


# ── 4. the schema ────────────────────────────────────────────────────────


def test_committed_schema_equals_the_generated_schema() -> None:
    assert json.loads(SCHEMA_JSON.read_text(encoding='utf-8')) == capture_schema()


def test_schema_is_valid_draft_2020_12() -> None:
    jsonschema.Draft202012Validator.check_schema(capture_schema())


def test_schema_validates_the_vendor_example_and_a_full_file() -> None:
    validator = jsonschema.Draft202012Validator(capture_schema())
    validator.validate(yaml.safe_load(VENDOR_YAML.read_text(encoding='utf-8')))
    validator.validate(yaml.safe_load(FULL_YAML))


def test_schema_rejects_broken_yaml_and_the_obvious_mistakes() -> None:
    validator = jsonschema.Draft202012Validator(capture_schema())
    errors = list(validator.iter_errors(yaml.safe_load(BROKEN_YAML.read_text(encoding='utf-8'))))
    assert errors
    for text in [
        'pulsr: {voltage: 20 V}',
        'pulser: {voltage: 20}',
        'pulser: {impedance: 200}',
        'sampling: {length: 100000}',
        'receiver: {tgc: {mode: false}}',
        'receiver: {tgc: {arbitrary: []}}',
        'averaging: {count: 9}',
        'pulser: {periods: 17}',
    ]:
        assert not validator.is_valid(yaml.safe_load(text)), text


def test_schema_accepts_the_case_variants_the_loader_accepts() -> None:
    validator = jsonschema.Draft202012Validator(capture_schema())
    for text in ['2.5 mhz', '2500 KHZ', '2.5MHz', '1e3 kHz']:
        assert validator.is_valid({'pulser': {'frequency': text}}), text
    for text in ['100 MS', '5 µs', '3 ns', '1 s']:
        assert validator.is_valid({'trigger': {'delay': text}}), text


def _enum_of(schema: dict[str, Any], *path: str) -> list[Any]:
    node = schema
    for key in path:
        node = node['properties'][key]
    return list(node['enum'])


def test_schema_enums_equal_the_literal_choice_lists() -> None:
    module = capture_module
    schema = capture_schema()
    assert _enum_of(schema, 'mode') == list(get_args(module.Mode))
    assert _enum_of(schema, 'pulser', 'type') == list(get_args(module.PulserType))
    assert _enum_of(schema, 'pulser', 'impedance') == list(get_args(module.Impedance))
    assert _enum_of(schema, 'receiver', 'high_pass') == list(get_args(module.HighPass))
    assert _enum_of(schema, 'receiver', 'tgc', 'mode') == list(get_args(module.TgcMode))
    assert _enum_of(schema, 'trigger', 'mode') == list(get_args(module.TriggerMode))
    rate = schema['properties']['sampling']['properties']['rate']
    assert rate['examples'] == [f'{c} MHz' for c in get_args(module.SampleRate)]


def test_every_literal_field_is_in_the_schema_with_its_choices() -> None:
    """Walk the dataclasses: any field annotated with a Literal must be an enum in the schema."""
    module = capture_module
    schema = capture_schema()

    def walk(cls: type, node: dict[str, Any]) -> None:
        for f in module._fields(cls):
            prop = node['properties'][f.name]
            choices = module._choices(f.hint)
            if isinstance(f.spec, module._Choice):
                assert prop['enum'] == list(choices), f.name
            elif isinstance(f.spec, module._Group):
                walk(module._group_class(f.hint), prop)
            elif isinstance(f.spec, module._Curve):
                walk(f.spec.item, prop['items'])
            elif choices and not f.spec.auto:  # type: ignore[union-attr]
                assert prop['examples'] == [f'{c} {f.spec.unit}' for c in choices]  # type: ignore[union-attr]

    walk(Capture, schema)


def test_schema_properties_match_the_dataclass_fields() -> None:
    schema = capture_schema()
    assert set(schema['properties']) == {
        'sampling', 'mode', 'pulser', 'receiver', 'trigger', 'averaging', 'extra',
    }  # fmt: skip
    assert set(schema['properties']['pulser']['properties']) == {
        'enabled', 'type', 'reverse_polarity', 'voltage', 'frequency', 'periods', 'gap',
        'damping', 'damping_gap', 'impedance',
    }  # fmt: skip
    assert schema['additionalProperties'] is False


def test_schema_describes_ranges_and_scpi_headers() -> None:
    props = capture_schema()['properties']
    voltage = props['pulser']['properties']['voltage']['description']
    assert '5 V to 100 V' in voltage and 'multiples of 5 V' in voltage and 'TRAN:PULS' in voltage
    assert props['sampling']['properties']['length']['minimum'] == 1024
    assert props['sampling']['properties']['length']['maximum'] == 36864
    assert 'UNKNOWN' in props['averaging']['properties']['count']['description']


# ── 5. python-side validation ────────────────────────────────────────────


def test_python_built_objects_are_validated_too() -> None:
    with pytest.raises(CaptureError, match='voltage'):
        Pulser(voltage=Decimal(120))
    with pytest.raises(CaptureError, match='length'):
        Sampling(length=10)
    with pytest.raises(CaptureError, match='rate'):
        Sampling(rate=3)  # type: ignore[arg-type]
    with pytest.raises(CaptureError, match='linear'):
        Tgc(mode='linear')
    with pytest.raises(CaptureError, match='increase'):
        Tgc(
            arbitrary=(
                TgcPoint(time=Decimal(2), gain=Decimal(0)),
                TgcPoint(time=Decimal(1), gain=Decimal(0)),
            )
        )
    with pytest.raises(CaptureError, match='expected true or false'):
        Pulser(enabled='yes')  # type: ignore[arg-type]
    with pytest.raises(CaptureError, match='whole number'):
        Averaging(count=True)


def test_python_numbers_are_coerced_to_decimal() -> None:
    pulser = Pulser(voltage=20, periods=1.5)  # type: ignore[arg-type]
    assert pulser.voltage == Decimal(20) and pulser.periods == Decimal('1.5')
    assert isinstance(pulser.voltage, Decimal) and isinstance(pulser.periods, Decimal)
    assert Receiver(gain=12.5).gain == Decimal('12.5')  # type: ignore[arg-type]


def test_model_objects_are_frozen() -> None:
    cap = Capture(mode='master')
    with pytest.raises((AttributeError, TypeError)):
        cap.mode = 'slave'  # type: ignore[misc]


# ── 6. apply_capture ─────────────────────────────────────────────────────


class _StubPlug:
    """Stand-in used when `a1580_openhtf.plug` / `fake_resource` do not import."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def apply_setup(self, setup: dict[str, str], /) -> None:
        self.events.extend(f'{header} {argument}' for header, argument in setup.items())

    def reset(self) -> None:
        self.events.append('*RST')


try:
    from a1580_openhtf.fake_resource import FakeA1580Resource
    from a1580_openhtf.plug import A1580Plug

    _HAVE_PLUG = True
except Exception:  # noqa: BLE001 - any import problem means: use the stub
    _HAVE_PLUG = False


class _Rig:
    """One plug (real on the fake, or the stub) and a way to see what it wrote."""

    def __init__(self) -> None:
        self.plug: Any
        if _HAVE_PLUG:
            self.fake = FakeA1580Resource()
            self.plug = A1580Plug(resource=self.fake, restore_state=False)
            self._baseline = len(self.fake.log)
        else:
            self.plug = _StubPlug()
            self._baseline = 0

    def writes(self) -> list[str]:
        """Everything written since construction, queries left out."""
        if _HAVE_PLUG:
            return [c for c in self.fake.log[self._baseline :] if not c.rstrip().endswith('?')]
        return list(self.plug.events)

    def everything(self) -> list[str]:
        if _HAVE_PLUG:
            return list(self.fake.log[self._baseline :])
        return list(self.plug.events)


def test_apply_capture_of_a_broken_file_writes_nothing() -> None:
    rig = _Rig()
    with pytest.raises(CaptureError):
        apply_capture(rig.plug, BROKEN_YAML)
    with pytest.raises(CaptureError):
        apply_capture(rig.plug, BROKEN_YAML, reset=True)
    assert rig.everything() == []


def test_apply_capture_of_a_missing_file_writes_nothing(tmp_path: Path) -> None:
    rig = _Rig()
    with pytest.raises(OSError):
        apply_capture(rig.plug, tmp_path / 'nope.yaml')
    assert rig.everything() == []


def test_apply_capture_of_the_vendor_example_writes_the_expected_sequence() -> None:
    rig = _Rig()
    result = apply_capture(rig.plug, VENDOR_YAML)
    assert result == load_capture(VENDOR_YAML)
    assert rig.writes() == [f'{h} {a}' for h, a in VENDOR_SCPI.items()]
    assert '*RST' not in rig.everything()  # reset defaults to False
    assert rig.writes()[-1] == 'TRAN:ENAB ON'


def test_apply_capture_accepts_a_capture_object_and_a_string_path() -> None:
    rig = _Rig()
    cap = load_capture(VENDOR_YAML)
    assert apply_capture(rig.plug, cap) is cap
    assert apply_capture(rig.plug, str(VENDOR_YAML)) == cap
    assert rig.writes() == [f'{h} {a}' for h, a in VENDOR_SCPI.items()] * 2


def test_apply_capture_reset_comes_first() -> None:
    rig = _Rig()
    apply_capture(rig.plug, VENDOR_YAML, reset=True)
    writes = rig.writes()
    assert writes[0] == '*RST'
    assert writes[1:] == [f'{h} {a}' for h, a in VENDOR_SCPI.items()]


def test_apply_capture_logs_the_high_voltage_warning(caplog: pytest.LogCaptureFixture) -> None:
    rig = _Rig()
    with caplog.at_level('WARNING', logger='a1580_openhtf.capture'):
        apply_capture(rig.plug, parse_capture('pulser:\n  voltage: 100 V\n'))
    assert any('pulser.voltage' in r.getMessage() for r in caplog.records)


def test_which_plug_the_apply_tests_used() -> None:
    """Not an assertion about behaviour: it records in the test output whether the stub ran."""
    print('apply_capture tests ran against', 'A1580Plug + fake' if _HAVE_PLUG else 'a stub plug')


# ── 7. command line ──────────────────────────────────────────────────────


def _run(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, '-m', 'a1580_openhtf.capture', *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_check_on_a_good_file() -> None:
    result = _run('check', 'captures/vendor_example.yaml')
    assert result.returncode == 0
    assert result.stdout.strip() == 'OK captures/vendor_example.yaml'
    assert result.stderr == ''


def test_cli_check_on_a_broken_file_reports_on_stderr_and_exits_1() -> None:
    result = _run('check', 'tests/data/broken.yaml')
    assert result.returncode == 1
    assert result.stdout == ''
    assert 'tests/data/broken.yaml:2: sampling.rate:' in result.stderr
    assert len(result.stderr.strip().splitlines()) == len(BROKEN_EXPECTED)


def test_cli_check_several_files_reports_each_and_fails_if_any_fails() -> None:
    result = _run('check', 'captures/vendor_example.yaml', 'tests/data/broken.yaml')
    assert result.returncode == 1
    assert 'OK captures/vendor_example.yaml' in result.stdout
    assert 'broken.yaml:3: sampling.length:' in result.stderr


def test_cli_check_a_missing_file_exits_1() -> None:
    result = _run('check', 'captures/does_not_exist.yaml')
    assert result.returncode == 1
    assert 'does_not_exist.yaml' in result.stderr


def test_cli_check_warns_about_high_voltage(tmp_path: Path) -> None:
    file = _write(tmp_path, 'pulser:\n  voltage: 80 V\n')
    result = _run('check', str(file))
    assert result.returncode == 0
    assert result.stdout.strip() == f'OK {file}'
    assert 'warning' in result.stderr and 'pulser.voltage' in result.stderr


def test_cli_without_arguments_is_a_usage_error() -> None:
    assert _run().returncode == 2
    assert _run('check').returncode == 2


def test_cli_schema_prints_the_valid_json_schema() -> None:
    result = _run('schema')
    assert result.returncode == 0
    assert json.loads(result.stdout) == capture_schema()


def test_cli_schema_write_round_trip(tmp_path: Path) -> None:
    result = _run('schema', '--write', cwd=tmp_path)
    assert result.returncode == 0
    written = tmp_path / 'capture.schema.json'
    assert json.loads(written.read_text(encoding='utf-8')) == capture_schema()
    assert written.read_text(encoding='utf-8') == SCHEMA_JSON.read_text(encoding='utf-8')


def test_an_over_long_tgc_arbitrary_line_is_a_problem_naming_length_and_limit() -> None:
    from a1580_openhtf.plug import MAX_LINE_BYTES

    assert capture_module._MAX_LINE_BYTES == MAX_LINE_BYTES

    def text(points: int) -> str:
        rows = ''.join(f'      - {{time: {10 + i} us, gain: 5 dB}}\n' for i in range(points))
        return 'receiver:\n  tgc:\n    arbitrary:\n' + rows

    ok = parse_capture(text(30))  # 'GAIN:TGC:ARB ' + 30 points of 'tt,5' is well under the limit
    assert ok.to_scpi()['GAIN:TGC:ARB'].count(',') == 59
    with pytest.raises(CaptureError) as exc:
        parse_capture(text(60))
    (problem,) = exc.value.problems
    assert 'receiver.tgc.arbitrary' in problem
    assert 'bytes including CRLF' in problem and 'limit is 255' in problem
