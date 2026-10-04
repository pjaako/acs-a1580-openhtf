# Adapted from rigol-dho-openhtf tests/test_plug.py (the `_plug()` helper, the numbered
# section banners, the OpenHTF integration and example-subprocess tests).
"""Tests for plug.py (SPEC.md section 5, test_plug.py). No hardware."""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import numpy as np
import openhtf as htf
import pytest

from a1580_openhtf import A1580Plug, AScan, AScanHeader
from a1580_openhtf.fake_resource import FakeA1580Resource
from a1580_openhtf.plug import (
    CONF,
    STATE_HEADERS,
    Identity,
    normalize_header,
    values_match,
)
from a1580_openhtf.stream import build_packet

ROOT = Path(__file__).resolve().parent.parent
ERR = 'SYSTem:ERRor?'

# The vendor example's settings in its order (PROTOCOL.md "Vendor example call sequence"),
# with the pulser at 20 V instead of 100 V.
VENDOR_SETUP: dict[str, object] = {
    'FREQ': '100 MHZ',
    'DATA:LENG': 8192,
    'TRAN:FREQ': '2500 KHz',
    'TRAN:PULS': '20 V',
    'TRAN:ENAB': 'ON',
    'TRAN:DUR': 1,
    'TRAN:REVerse': 'OFF',
    'TRAN:DAMP:ENAB': 'ON',
    'TRAN:TYPE': 'SINGle',
    'GAIN': 10,
    'GAIN:TGC:MODE': 'OFF',
    'AVER:COUN': 0,
    'TRIG:MODE': 'INTERNAL',
    'TRIG:INT': '100000 US',
    'TRAN:IMP': 200,
    'MODE': 'MASTer',
    'TRIG:DEL': '0 NS',
    'AVERage:DELay:CONStant:AUTO': 'ON',
    'AVERage:DELay:RANDom': '2000 NS',
    'TRAN:GAP': '5 NS',
    'TRAN:DAMP:GAP': '30 NS',
}


def _plug(restore_state: bool = False, **kwargs: Any) -> A1580Plug:
    """An A1580Plug on a FakeA1580Resource (kwargs go to the fake).

    restore_state defaults to False so that tests which are not about the snapshot do not
    see its 27 extra queries in the fake's log.
    """
    host = kwargs.pop('host', None)
    return A1580Plug(resource=FakeA1580Resource(**kwargs), restore_state=restore_state, host=host)


def _fake(plug: A1580Plug) -> FakeA1580Resource:
    fake = plug._resource
    assert isinstance(fake, FakeA1580Resource)
    return fake


def _stream_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == 'a1580-stream' and t.is_alive()]


@pytest.fixture(autouse=True)
def _no_threads_left() -> Iterator[None]:
    yield
    deadline = time.monotonic() + 2.0
    while _stream_threads() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not _stream_threads(), 'a test left a stream thread running'


# ── 1. construction ──────────────────────────────────────────────────────────


def test_construction_parses_identity_and_drains_errors() -> None:
    fake = FakeA1580Resource()
    fake.write('NOPE 1')  # stale error from an earlier session
    plug = A1580Plug(resource=fake, restore_state=False)
    assert plug.identity == Identity('ACS-Solutions GmbH', 'A1580-HF', '100500', '1.6.b41')
    assert plug.identity.firmware == '1.6.b41'
    assert fake.query(ERR) == '0,"No error"'  # the stale entry was drained
    assert not any(entry.startswith('*RST') for entry in fake.log)
    assert fake.log.index(ERR) < fake.log.index('*IDN?')


def test_construction_sets_the_session_attributes() -> None:
    fake = _fake(_plug())
    assert (fake.encoding, fake.timeout) == ('iso-8859-1', 5000)
    assert (fake.read_termination, fake.write_termination) == ('\r\n', '\r\n')


@pytest.mark.parametrize('reply', ['ACS,A1580,100500', 'a,b,c,d,e', ''])
def test_malformed_idn_raises(reply: str) -> None:
    with pytest.raises(RuntimeError, match='4 comma'):
        A1580Plug(resource=FakeA1580Resource(idn=reply), restore_state=False)


def test_malformed_idn_names_the_reply() -> None:
    with pytest.raises(RuntimeError, match='ACS,A1580,100500'):
        A1580Plug(resource=FakeA1580Resource(idn='ACS,A1580,100500'), restore_state=False)


def test_auto_placeholder() -> None:
    assert A1580Plug.auto_placeholder is True


def test_error_queue_helpers() -> None:
    plug = _plug()
    fake = _fake(plug)
    assert plug.check_errors() == []
    fake.write('NOPE 1')
    fake.write('NOPE 2')
    errors = plug.check_errors()
    assert [e.split(',')[0] for e in errors] == ['-113', '-113']
    assert plug.check_errors() == []
    with pytest.raises(RuntimeError, match=r"'NOPE 3'"):
        plug.write_checked('NOPE 3')
    plug.write_checked('GAIN 3')
    assert plug.query('GAIN?') == '3'


def test_check_errors_reads_at_most_20_times() -> None:
    plug = _plug()
    fake = _fake(plug)
    fake._errors.extend(['-100,"x"'] * 30)
    assert len(plug.check_errors()) == 20
    assert len(fake._errors) == 10


def test_check_errors_rejects_a_reply_without_number() -> None:
    plug = _plug()
    _fake(plug)._errors.append('oops')
    with pytest.raises(RuntimeError, match='oops'):
        plug.check_errors()


def test_reset_sends_rst_then_drains() -> None:
    plug = _plug()
    fake = _fake(plug)
    plug.write('GAIN 7')
    fake.write('NOPE 1')
    fake.log.clear()
    plug.reset()
    assert fake.log == ['*RST', ERR, ERR]
    assert plug.query('GAIN?') == '0'


# ── 2. apply_setup, vendor example ───────────────────────────────────────────


def test_apply_setup_vendor_example_sequence() -> None:
    plug = _plug()
    fake = _fake(plug)
    fake.log.clear()
    plug.apply_setup(VENDOR_SETUP)
    expected: list[str] = []
    for key, value in VENDOR_SETUP.items():
        expected += [f'{key} {value}', ERR, f'{key}?']
    assert fake.log == expected
    assert plug.check_errors() == []


def test_apply_setup_takes_values_of_other_types() -> None:
    plug = _plug()
    fake = _fake(plug)
    fake.log.clear()
    plug.apply_setup({'TRAN:ENAB': True, 'TRAN:DAMP': False, 'GAIN:TGC:LIN': [20.0, 0.1]})
    assert fake.log[0::3] == ['TRAN:ENAB ON', 'TRAN:DAMP OFF', 'GAIN:TGC:LIN 20,0.1']


def test_apply_setup_logs_that_min_max_cannot_be_verified(
    caplog: pytest.LogCaptureFixture,
) -> None:
    plug = _plug()
    with caplog.at_level(logging.WARNING):
        plug.apply_setup({'FREQ': 'MAX'})
    assert 'cannot be verified' in caplog.text


# ── 3. apply_setup failures ──────────────────────────────────────────────────


def test_apply_setup_rejected_header() -> None:
    plug = _plug(reject={'TRAN:DUR': '-222,"Data out of range"'})
    with pytest.raises(RuntimeError, match=r'TRAN:DUR.*-222') as exc:
        plug.apply_setup({'GAIN': 12, 'TRAN:DUR': 99, 'FREQ': '50 MHZ'})
    assert 'GAIN' not in str(exc.value)
    assert plug.query('GAIN?') == '12'  # the others were still applied
    assert plug.query('FREQ?') == '50000000'
    assert plug.query('TRAN:DUR?') == '1'
    assert plug.check_errors() == []


def test_apply_setup_clamped_value_and_rejected_header_in_one_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plug = _plug(reject={'TRAN:DUR': '-222,"Data out of range"'})
    fake = _fake(plug)
    original = fake._convert

    def clamp(header: str, arg: str) -> str:
        text = original(header, arg)
        if header == 'TRAN:PULS' and float(text) > 100:
            return '100'
        return text

    monkeypatch.setattr(fake, '_convert', clamp)
    with pytest.raises(RuntimeError) as exc:
        plug.apply_setup({'TRAN:PULS': '150 V', 'TRAN:DUR': 99, 'GAIN': 5})
    message = str(exc.value)
    assert message.startswith('apply_setup failed')
    assert 'TRAN:PULS' in message and "readback='100'" in message
    assert 'TRAN:DUR' in message and '-222' in message
    assert 'GAIN' not in message
    assert message.count('apply_setup failed') == 1


def test_apply_setup_unknown_header_is_reported_not_a_crash() -> None:
    plug = _plug()
    with pytest.raises(RuntimeError, match='NOPE:HDR'):
        plug.apply_setup({'NOPE:HDR': 1, 'GAIN': 4})
    assert plug.query('GAIN?') == '4'
    assert plug.check_errors() == []  # the errors were drained


# ── 4. values_match ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ('header', 'sent', 'reply', 'expected'),
    [
        ('FREQ', '100 MHZ', '100000000', True),
        ('TRIG:INT', '100000 US', '100.0E-3', True),
        ('TRAN:ENAB', 'ON', '1', True),
        ('TRIG:MODE', 'INTERNAL', 'INT', True),
        ('GAIN:TGC:MODE', 'LINear', 'LINEAR', True),
        ('TRAN:PULS', '100 V', '95', False),
        ('GAIN:TGC:LIN', '20.0, 0.1', '20,0.1', True),
        # booleans in any position
        ('TRAN:DAMP', 'ON', '1', True),
        ('TRAN:DAMP', '0', 'OFF', True),
        ('TRAN:REV', 'off', 'ON', False),
        ('GAIN:PRE:COMB', '1', '1', True),
        # enumerations
        ('TRIG:MODE', 'INTernal', 'INT', True),
        ('TRIG:MODE', 'INT', 'INTERNAL', True),
        ('MODE', 'MASTer', 'MASTER', True),
        ('MODE', 'MASTer', 'MAST', True),
        ('MODE', 'MASTer', 'SLAVE', False),
        ('TRAN:TYPE', 'SINGle', 'DUAL', False),
        ('TRAN:IMP', 'HIGH', 'HIGH', True),
        ('TRAN:IMP', '200', '200', True),
        ('TRAN:IMP', '200', '1000', False),
        # numbers with units, long and short header forms, optional roots
        ('SOURce:FREQuency', '100 MHZ', '100000000', True),
        ('TRANsmitter:FREQuency', '2500 KHz', '2500000', True),
        ('TRAN:FREQ', '2500 KHz', '2500', False),
        ('TRIG:DEL', '15 US', '15000', True),
        ('TRIG:DEL', '0 NS', '0', True),
        ('TRIGgering:DELay', '1 MS', '1000000', True),
        ('AVER:DEL:RAND', '2000 NS', '2.0E-6', True),
        ('SENSe:AVERage:DELay:RANDom', '2000 NS', '2.0E-6', True),
        ('AVER:DEL:CONS', '50 US', '50.0E-6', True),
        ('TRAN:GAP', '5 NS', '5', True),
        ('TRAN:DAMP:GAP', '30 NS', '30', True),
        ('TRAN:PULS', '20 V', '20', True),
        ('TRANsmitter:PULSe:LEVel', '20 V', '20.0', True),
        ('GAIN', '10 DB', '10', True),
        ('GAIN', '10', '10.0', True),
        ('GAIN', '10', '11', False),
        ('FREQ', '100 NS', '100000000', False),  # wrong dimension
        ('DATA:LENG', '8192', '8192', True),
        ('AVER:COUN', '0', '0', True),
        # lists
        ('GAIN:TGC:LIN', '20, 0.1', '20.0, 0.1', True),
        ('GAIN:TGC:LIN', '20, 0.1', '20.0, 0.2', False),
        ('GAIN:TGC:LIN', '20, 0.1', '20.0', False),
        ('GAIN:TGC:ARB', '0,5,2,20', '0.0, 5.0, 2.0, 20.0', True),
        # keywords cannot be verified
        ('FREQ', 'MAX', '100000000', True),
        ('TRAN:PULS', 'minimum', '5', True),
        ('GAIN', 'UP', '11', True),
        # garbage
        ('FREQ', '100 MHZ', 'banana', False),
        ('MODE', 'MASTer', '', False),
    ],
)
def test_values_match(header: str, sent: str, reply: str, expected: bool) -> None:
    assert values_match(header, sent, reply) is expected


@pytest.mark.parametrize(
    ('header', 'normalised'),
    [
        ('FREQ', 'FREQ'),
        ('freq?', 'FREQ'),
        ('SOURce:FREQuency', 'FREQ'),
        (':SENSe:AVERage:COUNt', 'AVER:COUN'),
        ('AVERage:DELay:CONStant:VALue', 'AVER:DEL:CONS'),
        ('AVERage:DELay:CONStant:AUTO', 'AVER:DEL:CONS:AUTO'),
        ('TRANsmitter:DAMP:ENABle', 'TRAN:DAMP'),
        ('TRAN:DAMP:ENAB', 'TRAN:DAMP'),
        ('TRANsmitter:DAMP:GAP', 'TRAN:DAMP:GAP'),
        ('TRANsmitter:ENABle', 'TRAN:ENAB'),
        ('TRANsmitter:PULSe:LEVel', 'TRAN:PULS'),
        ('GAIN:LEVel', 'GAIN'),
        ('TRIGgering:MODe', 'TRIG:MODE'),
        ('TRIG:MODE', 'TRIG:MODE'),
        ('MODe', 'MODE'),
        ('GAIN:PREamp:SPLIT', 'GAIN:PRE:SPLIT'),
        ('GAIN:TGC:LINear', 'GAIN:TGC:LIN'),
        ('FILTer:HPASs:INDex', 'FILT:HPAS:IND'),
        ('SYSTem:ERRor', 'SYST:ERR'),
        ('SYSTem:ERRor:NEXT?', 'SYST:ERR'),
        ('SYSTem:ERRor:COUNt?', 'SYST:ERR:COUN'),
        ('MEMory:CLEar', 'MEM:CLE'),
        ('STARt', 'STAR'),
        ('*IDN?', '*IDN'),
        ('*RST', '*RST'),
        ('FREQUENCY', 'FREQ'),
        ('transmitter:duration', 'TRAN:DUR'),
    ],
)
def test_normalize_header(header: str, normalised: str) -> None:
    assert normalize_header(header) == normalised


def test_state_headers_are_their_own_normal_form() -> None:
    assert len(STATE_HEADERS) == len(set(STATE_HEADERS)) == 27
    assert all(normalize_header(h) == h for h in STATE_HEADERS)
    assert STATE_HEADERS[0] == 'FREQ' and STATE_HEADERS[-1] == 'FILT:HPAS:IND'


# ── 5. get_state / set_state / restore ───────────────────────────────────────


def _change_things(plug: A1580Plug) -> None:
    plug.apply_setup(
        {
            'FREQ': '50 MHZ',
            'DATA:LENG': 4096,
            'TRAN:FREQ': '1 MHZ',
            'TRAN:PULS': '55 V',
            'TRAN:DAMP': 'ON',
            'TRAN:ENAB': 'ON',
            'TRIG:INT': '2 MS',
            'TRIG:DEL': '7 US',
            'GAIN': 33,
            'GAIN:TGC:MODE': 'LINear',
            'GAIN:TGC:LIN': '10, 0.22',
            'AVER:COUN': 5,
            'AVER:DEL:RAND': '3000 NS',
            'MODE': 'SLAVe',
        }
    )


def test_get_state_queries_every_header_in_order() -> None:
    plug = _plug()
    fake = _fake(plug)
    fake.log.clear()
    state = plug.get_state()
    assert list(state) == list(STATE_HEADERS)
    assert fake.log == [f'{h}?' for h in STATE_HEADERS]
    assert state['FREQ'] == '100000000'


def test_get_set_state_round_trip() -> None:
    plug = _plug()
    initial = plug.get_state()
    _change_things(plug)
    changed = plug.get_state()
    assert changed != initial
    plug.set_state(initial)
    assert plug.get_state() == initial
    assert plug.check_errors() == []


def test_set_state_writes_in_order_with_units_and_checks_each() -> None:
    plug = _plug()
    fake = _fake(plug)
    state = plug.get_state()
    fake.log.clear()
    plug.set_state(state)
    writes = [e for e in fake.log if e != ERR]
    # TRAN:ENAB OFF unchecked first, one drain (stale entries), then one drain per write
    assert fake.log[:2] == ['TRAN:ENAB OFF', ERR]
    assert fake.log[2::2] == writes[1:]
    assert fake.log[3::2] == [ERR] * len(writes[1:])
    heads = [w.split(' ', 1)[0] for w in writes]
    # TRAN:ENAB OFF first, the rest in STATE_HEADERS order, TRAN:ENAB OFF again at the end
    expected = ['TRAN:ENAB'] + [h for h in STATE_HEADERS if h not in ('TRAN:ENAB', 'AVER:DEL:CONS')]
    expected.append('TRAN:ENAB')
    assert heads == expected
    assert writes[0] == writes[-1] == 'TRAN:ENAB OFF'
    assert 'FREQ 100 MHZ' in writes
    assert 'TRAN:FREQ 5000 KHZ' in writes
    assert 'TRIG:INT 10000 US' in writes
    assert 'TRIG:DEL 15000 NS' in writes
    assert 'TRAN:PULS 20 V' in writes
    assert 'GAIN 0' in writes
    assert 'AVER:DEL:RAND 2000 NS' in writes
    # the TGC curves are in place before the TGC mode is written
    assert writes.index('GAIN:TGC:LIN 20.0, 0.1') < writes.index('GAIN:TGC:MODE OFF')
    assert STATE_HEADERS.index('GAIN:TGC:ARB') < STATE_HEADERS.index('GAIN:TGC:MODE')


def test_set_state_never_writes_pulser_on_from_a_snapshot(
    caplog: pytest.LogCaptureFixture,
) -> None:
    plug = _plug()
    fake = _fake(plug)
    plug.apply_setup({'TRAN:ENAB': 'ON'})
    state = plug.get_state()
    assert state['TRAN:ENAB'] == 'ON'
    fake.log.clear()
    with caplog.at_level(logging.WARNING):
        plug.set_state(state)
    writes = [e for e in fake.log if e != ERR]
    assert writes[0] == writes[-1] == 'TRAN:ENAB OFF'
    assert writes.count('TRAN:ENAB OFF') == 2
    assert not any(w.startswith('TRAN:ENAB ON') for w in writes)
    assert fake.log[:2] == ['TRAN:ENAB OFF', ERR]  # unchecked, before the drain
    assert plug.query('TRAN:ENAB?') == 'OFF'
    assert 'snapshot had the pulser ON' in caplog.text


@pytest.mark.parametrize('value', ['ON', '1'])
def test_construction_warns_when_the_pulser_is_already_on(
    value: str, caplog: pytest.LogCaptureFixture
) -> None:
    fake = FakeA1580Resource()
    fake._values['TRAN:ENAB'] = value
    with caplog.at_level(logging.WARNING):
        A1580Plug(resource=fake, restore_state=True)
    assert 'pulser was already enabled when the plug was created' in caplog.text
    assert 'switched off in tearDown' in caplog.text


def test_construction_does_not_warn_when_the_pulser_is_off(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        A1580Plug(resource=FakeA1580Resource(), restore_state=True)
    assert 'already enabled' not in caplog.text


def test_teardown_leaves_the_pulser_off_when_it_was_on_at_construction() -> None:
    fake = FakeA1580Resource()
    fake._values['TRAN:ENAB'] = 'ON'
    plug = A1580Plug(resource=fake, restore_state=True)
    plug.tearDown()
    assert fake._values['TRAN:ENAB'] == 'OFF'
    assert fake.closed


def test_set_state_skips_constant_delay_while_auto_is_on() -> None:
    plug = _plug()
    fake = _fake(plug)
    state = plug.get_state()
    assert state['AVER:DEL:CONS:AUTO'] == 'ON'
    fake.log.clear()
    plug.set_state(state)
    assert not any(e.startswith('AVER:DEL:CONS ') for e in fake.log)
    assert plug.check_errors() == []  # and so no -221


@pytest.mark.parametrize('auto', ['OFF', '0'])
def test_set_state_writes_constant_delay_when_auto_is_off(auto: str) -> None:
    plug = _plug()
    fake = _fake(plug)
    plug.apply_setup({'AVER:DEL:CONS:AUTO': 'OFF', 'AVER:DEL:CONS': '50 US'})
    state = plug.get_state()
    state['AVER:DEL:CONS:AUTO'] = auto
    plug.apply_setup({'AVER:DEL:CONS': '70 US'})
    fake.log.clear()
    plug.set_state(state)
    assert 'AVER:DEL:CONS 50000 NS' in fake.log
    assert plug.query('AVER:DEL:CONS?') == '50.0E-6'


def test_set_state_ignores_errors_queued_before_it() -> None:
    plug = _plug()
    state = plug.get_state()
    _fake(plug).write('NOPE 1')
    plug.set_state(state)  # must not blame the restore for the stale error
    assert plug.check_errors() == []


def test_set_state_raises_when_a_write_is_rejected() -> None:
    plug = _plug()
    state = plug.get_state()
    _fake(plug)._reject['TRAN:PULS'] = '-222,"Data out of range"'
    with pytest.raises(RuntimeError, match='TRAN:PULS'):
        plug.set_state(state)


def test_restore_state_true_snapshots_and_restores() -> None:
    fake = FakeA1580Resource()
    plug = A1580Plug(resource=fake, restore_state=True)
    assert [e for e in fake.log if e.endswith('?') and e != ERR][1:] == [
        f'{h}?' for h in STATE_HEADERS
    ]
    initial = dict(fake._values)
    _change_things(plug)
    fake.log.clear()
    plug.tearDown()
    assert fake.closed
    assert fake._values == initial
    assert fake.log[:3] == ['TRAN:ENAB OFF', 'STOP', 'TRAN:ENAB OFF']  # pulser off first
    assert fake.log[3] == ERR  # set_state: unchecked OFF, then the drain
    assert fake.stop_count == 1
    assert any(e.startswith('FREQ ') for e in fake.log)


def test_restore_state_default_comes_from_config() -> None:
    assert CONF.a1580_restore_state is True
    fake = FakeA1580Resource()
    A1580Plug(resource=fake)  # restore_state=None -> CONF
    assert 'DATA:LENG?' in fake.log


def test_restore_state_false_stops_and_closes_only() -> None:
    plug = _plug(restore_state=False)
    fake = _fake(plug)
    fake.log.clear()
    _change_things(plug)
    changed = dict(fake._values)
    fake.log.clear()
    plug.tearDown()
    # unchecked OFF, STOP, then the pulser off again (checked: one drain), nothing restored
    assert fake.log == ['TRAN:ENAB OFF', 'STOP', 'TRAN:ENAB OFF', ERR]
    assert fake._values == {**changed, 'TRAN:ENAB': 'OFF'}
    assert fake.closed


def test_teardown_without_restore_switches_the_pulser_off_after_stop() -> None:
    plug = _plug(restore_state=False)
    fake = _fake(plug)
    plug.apply_setup({'TRAN:ENAB': 'ON'})
    assert fake.query('TRAN:ENAB?') == 'ON'
    fake.log.clear()
    plug.tearDown()
    assert fake.log[:3] == ['TRAN:ENAB OFF', 'STOP', 'TRAN:ENAB OFF']
    assert fake._values['TRAN:ENAB'] == 'OFF'
    assert fake.closed


def test_teardown_without_restore_logs_a_failed_pulser_off(
    caplog: pytest.LogCaptureFixture,
) -> None:
    plug = _plug(restore_state=False)
    fake = _fake(plug)
    fake._reject['TRAN:ENAB'] = '-222,"Data out of range"'
    with caplog.at_level(logging.WARNING):
        plug.tearDown()  # must not raise
    assert 'pulser off' in caplog.text
    assert fake.closed


def test_failing_restore_is_logged_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    fake = FakeA1580Resource()
    plug = A1580Plug(resource=fake, restore_state=True)
    fake._reject['TRAN:PULS'] = '-222,"Data out of range"'
    with caplog.at_level(logging.WARNING):
        plug.tearDown()
    assert 'Failed to restore' in caplog.text
    assert fake.closed


def test_teardown_survives_a_dead_resource(caplog: pytest.LogCaptureFixture) -> None:
    fake = FakeA1580Resource()
    plug = A1580Plug(resource=fake, restore_state=True)

    def broken(*_: object) -> None:
        raise OSError('link down')

    fake.write = broken  # type: ignore[method-assign]
    fake.query = broken  # type: ignore[assignment]
    with caplog.at_level(logging.WARNING):
        plug.tearDown()
    assert fake.closed


# ── 6. acquire ───────────────────────────────────────────────────────────────


def _logging_plug(**kwargs: Any) -> A1580Plug:
    """A plug whose data socket factory also writes into the fake's log."""
    fake = FakeA1580Resource(**kwargs)

    def factory(host: str, port: int) -> Any:
        fake.log.append(f'connect {host}:{port}')
        return fake.data_socket_factory(host, port)

    return A1580Plug(resource=fake, data_socket_factory=factory, host='x', restore_state=False)


def test_acquire_three() -> None:
    plug = _logging_plug(length=2048)
    fake = _fake(plug)
    fake.log.clear()
    scans = plug.acquire(3)
    assert fake.log == [
        'TRIG:INT?',  # the default timeout is derived from the trigger interval
        'DATA:LENG?',
        'FREQ?',
        'TRIG:DEL?',
        'DATA:PORT?',
        'connect x:2758',
        'STAR AUTO',
        'STOP',
        ERR,
    ]
    assert fake.connections == [('x', 2758)]
    assert len(scans) == 3
    assert all(isinstance(s, AScan) and isinstance(s.header, AScanHeader) for s in scans)
    for s in scans:
        assert s.raw.dtype == np.int16
        assert len(s.raw) == 2048
        assert s.fs_hz == 1e8
        assert s.trigger_delay_ns == 15000
        assert s.t[1] == 1 / s.fs_hz
        assert s.t.dtype == np.float64
    assert [s.header.packet_number for s in scans] == [0, 1, 2]
    assert not fake.started
    assert fake.sockets[0].closed
    assert plug._data_sock is None


@pytest.mark.parametrize(
    'kwargs', [{'chunk': 7}, {'garbage_prefix': b'xx'}, {'chunk': 7, 'garbage_prefix': b'xx'}]
)
def test_acquire_survives_chunking_and_garbage(kwargs: dict[str, Any]) -> None:
    plug = _plug(**kwargs)
    reference = _plug()
    scans = plug.acquire(3)
    expected = reference.acquire(3)
    assert [s.header.packet_number for s in scans] == [0, 1, 2]
    for got, want in zip(scans, expected, strict=True):
        np.testing.assert_array_equal(got.raw, want.raw)


def test_acquire_uses_the_current_settings() -> None:
    plug = _plug()
    plug.apply_setup({'DATA:LENG': 3000, 'FREQ': '50 MHZ', 'TRIG:DEL': '2 US', 'AVER:COUN': 2})
    (scan,) = plug.acquire(1)
    assert len(scan.raw) == 3000
    assert scan.fs_hz == 5e7
    assert scan.trigger_delay_ns == 2000
    assert scan.header.ascan_count == 2
    assert scan.t[-1] == pytest.approx(2999 / 5e7)


def test_ascan_burst_content_and_no_voltage_array() -> None:
    (scan,) = _plug().acquire(1)
    assert not hasattr(scan, 'v')
    assert not scan.raw[:100].any()
    assert scan.raw.any()
    scan.t[0] = 5.0  # t is recomputed, not cached
    assert scan.t[0] == 0.0


def test_acquire_rejects_bad_arguments() -> None:
    plug = _plug()
    fake = _fake(plug)
    fake.log.clear()
    with pytest.raises(ValueError):
        plug.acquire(0)
    with pytest.raises(ValueError):
        plug.acquire(1, timeout_s=0)
    assert fake.log == []


def test_acquire_logs_instrument_errors_but_returns_the_packets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    plug = _plug()
    fake = _fake(plug)
    fake._reject['STAR'] = '-200,"Execution error"'
    with pytest.raises(TimeoutError):  # the rejected start leaves nothing to read
        plug.acquire(1, timeout_s=0.05)
    fake._reject.clear()
    original = fake.write

    def noisy(cmd: str) -> None:
        original(cmd)
        if cmd == 'STAR AUTO':
            fake._errors.append('-200,"Execution error"')

    fake.write = noisy  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING):
        scans = plug.acquire(1)  # the data arrived: the late device error is a warning
    assert len(scans) == 1
    assert '-200' in caplog.text
    assert plug._data_sock is None
    assert not fake.started


def test_acquire_closes_socket_when_connect_fails() -> None:
    fake = FakeA1580Resource()

    def refuse(host: str, port: int) -> Any:
        raise ConnectionRefusedError(f'{host}:{port}')

    plug = A1580Plug(resource=fake, data_socket_factory=refuse, restore_state=False)
    fake.log.clear()
    with pytest.raises(ConnectionRefusedError):
        plug.acquire(1)
    assert 'STAR AUTO' not in fake.log  # never started


def test_ascan_from_packet_validates() -> None:
    packet = build_packet([1, 2, 3], packet_number=9)
    scan = AScan.from_packet(packet, 1e6, 5)
    assert scan.raw.tolist() == [1, 2, 3]
    assert scan.header.packet_number == 9
    assert scan.t.tolist() == pytest.approx([0.0, 1e-6, 2e-6])
    with pytest.raises(ValueError):
        AScan.from_packet(packet[:-1], 1e6, 5)
    with pytest.raises(ValueError):
        AScan.from_packet(packet[:28], 1e6, 5)
    with pytest.raises(ValueError):
        AScan.from_packet(b'XXXX' + packet[4:], 1e6, 5)


# ── 7. acquire timeout ───────────────────────────────────────────────────────


def test_acquire_timeout_still_sends_stop() -> None:
    plug = _plug(signal='none')
    fake = _fake(plug)
    fake.log.clear()
    with pytest.raises(TimeoutError, match=r'DATA:LENG=1024, port=2758'):
        plug.acquire(2, timeout_s=0.1)
    assert fake.log[-2:] == ['STOP', ERR]
    assert fake.log.index('STAR AUTO') < fake.log.index('STOP')
    assert not fake.started
    assert fake.sockets[0].closed
    assert plug._data_sock is None


# ── 8. streaming ─────────────────────────────────────────────────────────────


def _wait_for(predicate: Callable[[], bool], timeout_s: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def test_stream_delivers_and_stops() -> None:
    plug = _plug(length=1024)
    fake = _fake(plug)
    got: list[AScan] = []
    plug.start_stream(got.append)
    thread = plug._stream.thread if plug._stream else None
    assert thread is not None and thread.daemon
    assert _wait_for(lambda: len(got) >= 5, 1.0)
    count = plug.stop_stream()
    assert count == len(got) >= 5
    assert not thread.is_alive()
    assert not fake.started
    assert fake.sockets[0].closed
    assert plug._data_sock is None and plug._stream is None
    assert fake.log[-2:] == ['STOP', ERR]
    assert [s.header.packet_number for s in got[:3]] == [0, 1, 2]
    assert plug.stop_stream() == 0  # nothing running any more
    assert len(plug.acquire(1)) == 1  # and the plug is usable again


def test_stream_guards() -> None:
    plug = _plug()
    plug.start_stream(lambda s: None)
    try:
        with pytest.raises(RuntimeError, match='stream'):
            plug.acquire(1)
        with pytest.raises(RuntimeError, match='already running'):
            plug.start_stream(lambda s: None)
    finally:
        plug.stop_stream()
    with pytest.raises(ValueError):
        plug.start_stream(lambda s: None, timeout_s=0)


def test_stream_callback_exception_surfaces_from_stop_stream(
    caplog: pytest.LogCaptureFixture,
) -> None:
    plug = _plug()
    fake = _fake(plug)
    calls: list[int] = []

    def boom(scan: AScan) -> None:
        calls.append(1)
        raise ValueError('callback exploded')

    with caplog.at_level(logging.ERROR):
        plug.start_stream(boom)
        thread = plug._stream.thread if plug._stream else None
        assert thread is not None
        thread.join(2.0)
        assert not thread.is_alive()  # the failure stopped the stream by itself
    assert 'callback raised' in caplog.text
    with pytest.raises(ValueError, match='callback exploded'):
        plug.stop_stream()
    assert calls == [1]
    assert not fake.started  # cleanup still ran
    assert plug._data_sock is None and plug._stream is None
    assert plug.stop_stream() == 0  # the failure is reported once
    assert len(plug.acquire(1)) == 1


def test_stream_stall_surfaces_as_timeout() -> None:
    plug = _plug(signal='none')
    plug.start_stream(lambda s: None, timeout_s=0.1)
    thread = plug._stream.thread if plug._stream else None
    assert thread is not None
    thread.join(2.0)
    assert not thread.is_alive()
    with pytest.raises(TimeoutError, match='no A-scan'):
        plug.stop_stream()


def test_stream_connection_loss_surfaces() -> None:
    plug = _plug()
    plug.start_stream(lambda s: time.sleep(0.001))
    fake = _fake(plug)
    assert _wait_for(lambda: plug._stream is not None and plug._stream.count >= 1)
    fake.sockets[0].shut = True  # the peer closes: recv returns b''
    thread = plug._stream.thread if plug._stream else None
    assert thread is not None
    thread.join(2.0)
    with pytest.raises(ConnectionError):
        plug.stop_stream()


def test_teardown_stops_a_running_stream() -> None:
    fake = FakeA1580Resource()
    plug = A1580Plug(resource=fake, restore_state=True)
    plug.start_stream(lambda s: None)
    thread = plug._stream.thread if plug._stream else None
    assert thread is not None
    plug.tearDown()
    assert not thread.is_alive()
    assert fake.closed and not fake.started


# ── 9. OpenHTF integration ───────────────────────────────────────────────────


class FakePlug(A1580Plug):
    def __init__(self) -> None:
        super().__init__(resource=FakeA1580Resource())


def test_openhtf_integration() -> None:
    seen: dict[str, Any] = {}

    @htf.measures(htf.Measurement('num_points'), htf.Measurement('peak_counts'))
    @htf.plug(pr=A1580Plug)
    def acquire_phase(test: Any, pr: A1580Plug) -> None:
        (scan,) = pr.acquire(1)
        test.measurements.num_points = len(scan.raw)
        test.measurements.peak_counts = int(np.abs(scan.raw.astype(int)).max())
        seen['plug'] = pr

    result = htf.Test(acquire_phase.with_plugs(pr=FakePlug)).execute(test_start=lambda: 'dut1')
    assert result is True
    assert isinstance(seen['plug'], FakePlug)
    assert seen['plug']._resource.closed  # OpenHTF called tearDown


# ── 10. configuration and wiring ─────────────────────────────────────────────


def test_config_keys_and_defaults() -> None:
    assert CONF.a1580_host == '192.168.200.18'
    assert CONF.a1580_scpi_port == 5025
    assert CONF.a1580_restore_state is True
    declarations = CONF._declarations
    for key in ('a1580_host', 'a1580_scpi_port', 'a1580_restore_state'):
        assert declarations[key].description


def test_host_is_passed_to_the_data_socket_factory() -> None:
    fake = FakeA1580Resource()
    calls: list[tuple[str, int]] = []

    def factory(host: str, port: int) -> Any:
        calls.append((host, port))
        return fake.data_socket_factory(host, port)

    plug = A1580Plug(resource=fake, host='x', data_socket_factory=factory, restore_state=False)
    plug.acquire(1)
    assert calls == [('x', 2758)]
    assert plug.host == 'x'


def test_fake_resource_factory_is_used_by_default() -> None:
    fake = FakeA1580Resource()
    plug = A1580Plug(resource=fake, host='x', restore_state=False)
    plug.acquire(1)
    assert fake.connections == [('x', 2758)]


def test_host_defaults_to_config() -> None:
    fake = FakeA1580Resource()
    A1580Plug(resource=fake, restore_state=False).acquire(1)
    assert fake.connections == [(CONF.a1580_host, 2758)]


def test_data_port_comes_from_the_reply_not_a_constant() -> None:
    fake = FakeA1580Resource()
    fake._values['DATA:PORT'] = '2800'
    A1580Plug(resource=fake, restore_state=False).acquire(1)
    assert fake.connections[0][1] == 2800


class _StubRM:
    def __init__(self, backend: str, resource: FakeA1580Resource) -> None:
        self.backend, self.resource = backend, resource
        self.opened: str | None = None
        self.closed = False

    def open_resource(self, name: str) -> FakeA1580Resource:
        self.opened = name
        return self.resource

    def close(self) -> None:
        self.closed = True


def test_real_resource_path_uses_pyvisa_py_socket_resource(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pyvisa

    fake = FakeA1580Resource()
    made: list[_StubRM] = []

    def resource_manager(backend: str) -> _StubRM:
        made.append(_StubRM(backend, fake))
        return made[0]

    monkeypatch.setattr(pyvisa, 'ResourceManager', resource_manager)
    plug = A1580Plug(host='10.1.2.3', restore_state=False)
    rm = made[0]
    assert rm.backend == '@py'
    assert rm.opened == 'TCPIP::10.1.2.3::5025::SOCKET'
    assert (fake.encoding, fake.timeout) == ('iso-8859-1', 5000)
    assert (fake.read_termination, fake.write_termination) == ('\r\n', '\r\n')
    assert plug.identity.model == 'A1580-HF'
    plug.tearDown()
    assert fake.closed and rm.closed


def test_real_resource_path_cleans_up_when_construction_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pyvisa

    fake = FakeA1580Resource(idn='broken')
    rm = _StubRM('@py', fake)
    monkeypatch.setattr(pyvisa, 'ResourceManager', lambda backend: rm)
    with pytest.raises(RuntimeError, match='4 comma'):
        A1580Plug(restore_state=False)
    assert fake.closed and rm.closed


# ── 11. apply_capture ────────────────────────────────────────────────────────


def test_apply_capture_applies_the_vendor_example_file() -> None:
    from a1580_openhtf.capture import Capture, load_capture

    plug = _plug()
    fake = _fake(plug)
    fake.log.clear()
    capture = plug.apply_capture(ROOT / 'captures' / 'vendor_example.yaml')
    assert isinstance(capture, Capture)
    assert capture == load_capture(ROOT / 'captures' / 'vendor_example.yaml')
    assert fake.log[0] != '*RST'  # reset defaults to False
    assert '*RST' not in fake.log
    # TRAN:ENAB is the last header: its write, the error drain, then the read-back
    assert fake.log[-3:] == ['TRAN:ENAB ON', ERR, 'TRAN:ENAB?']
    assert fake._values['TRAN:ENAB'] == 'ON'
    assert fake._values['TRAN:PULS'] == '20'


def test_apply_capture_reset_sends_rst_first() -> None:
    plug = _plug()
    fake = _fake(plug)
    fake.log.clear()
    plug.apply_capture(ROOT / 'captures' / 'vendor_example.yaml', reset=True)
    assert fake.log[0] == '*RST'


def test_apply_capture_broken_file_raises_and_writes_nothing() -> None:
    from a1580_openhtf.capture import CaptureError

    plug = _plug()
    fake = _fake(plug)
    fake.log.clear()
    with pytest.raises(CaptureError):
        plug.apply_capture(ROOT / 'tests' / 'data' / 'broken.yaml')
    assert fake.log == []


# ── importing and running without hardware or pyvisa ─────────────────────────


def test_package_imports_and_runs_without_pyvisa() -> None:
    code = (
        'import sys\n'
        "sys.modules['pyvisa'] = None\n"
        'import a1580_openhtf\n'
        'from a1580_openhtf.fake_resource import FakeA1580Resource\n'
        'plug = a1580_openhtf.A1580Plug(resource=FakeA1580Resource())\n'
        'assert len(plug.acquire(2)) == 2\n'
        'plug.tearDown()\n'
        "assert sys.modules['pyvisa'] is None\n"
        "print('ok', a1580_openhtf.__version__)\n"
    )
    result = subprocess.run(
        [sys.executable, '-c', code], capture_output=True, text=True, cwd=ROOT, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'ok 0.1.0'


def test_pyvisa_is_required_only_for_the_real_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, 'pyvisa', None)
    with pytest.raises(ImportError):
        A1580Plug(restore_state=False)


def test_public_names() -> None:
    import a1580_openhtf

    assert a1580_openhtf.__version__ == '0.1.0'
    assert set(a1580_openhtf.__all__) >= {
        'A1580Plug',
        'AScan',
        'AScanHeader',
        'Capture',
        'CaptureError',
        'FakeA1580Resource',
        'load_capture',
    }


def test_example_runs_in_fake_mode() -> None:
    result = subprocess.run(
        [sys.executable, 'examples/example_test.py', '--fake'],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'num_points=8192' in result.stdout
    assert 'peak_counts=' in result.stdout


def test_example_host_option_only_sets_the_config_in_fake_mode() -> None:
    result = subprocess.run(
        [sys.executable, 'examples/example_test.py', '--fake', '--host', 'example.invalid'],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'num_points=' in result.stdout


# ══ review fixes ═════════════════════════════════════════════════════════════
# Numbered as in the review: 1 teardown, 2-3 set_state, 4 keywords, 5 example, 6 get_state,
# 7 apply_setup, 8 set_state failures, 9-10 acquire, 11 framing, 12-13 matching,
# 14 streams, 15 lock, 16 visa errors.


def _flaky(
    fake: FakeA1580Resource, method: str, bad: Callable[[str], bool], exc: Exception
) -> None:
    """Make `fake.<method>(cmd)` raise `exc` whenever `bad(cmd)`."""
    original = getattr(fake, method)

    def wrapper(cmd: str) -> Any:
        if bad(cmd):
            fake.log.append(cmd)
            raise exc
        return original(cmd)

    setattr(fake, method, wrapper)


# ── 1. tearDown: the first action is an unchecked TRAN:ENAB OFF ──────────────


@pytest.mark.parametrize('restore', [True, False])
def test_teardown_first_action_is_an_unchecked_pulser_off(restore: bool) -> None:
    fake = FakeA1580Resource()
    plug = A1580Plug(resource=fake, restore_state=restore)
    plug.apply_setup({'TRAN:ENAB': 'ON'})
    fake.log.clear()
    plug.tearDown()
    assert fake.log[0] == 'TRAN:ENAB OFF'  # before STOP, before any error-queue read
    assert fake.log[1] == 'STOP'
    assert fake._values['TRAN:ENAB'] == 'OFF'


def test_teardown_pulser_off_comes_before_stopping_a_running_stream() -> None:
    fake = FakeA1580Resource()
    plug = A1580Plug(resource=fake, restore_state=False)
    plug.start_stream(lambda s: None)
    mark = len(fake.log)
    plug.tearDown()
    assert fake.log[mark] == 'TRAN:ENAB OFF'
    assert fake.log.index('STOP', mark) > mark


def test_teardown_goes_on_when_the_first_pulser_off_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = FakeA1580Resource()
    plug = A1580Plug(resource=fake, restore_state=False)
    calls: list[str] = []
    original = fake.write

    def write(cmd: str) -> None:
        calls.append(cmd)
        if len(calls) == 1:
            raise OSError('link hiccup')
        original(cmd)

    fake.write = write  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING):
        plug.tearDown()
    assert calls == ['TRAN:ENAB OFF', 'STOP', 'TRAN:ENAB OFF']  # the checked one still follows
    assert 'pulser off first' in caplog.text
    assert fake._values['TRAN:ENAB'] == 'OFF'
    assert fake.closed


# ── 2. set_state: OFF before the drain ───────────────────────────────────────


def test_set_state_goes_on_when_the_first_drain_raises(caplog: pytest.LogCaptureFixture) -> None:
    plug = _plug()
    fake = _fake(plug)
    state = plug.get_state()
    plug.apply_setup({'FREQ': '50 MHZ'})
    seen: list[int] = []
    original = fake.query

    def query(cmd: str) -> str:
        if cmd == ERR and not seen:
            seen.append(1)
            raise OSError('drain failed')
        return original(cmd)

    fake.query = query  # type: ignore[method-assign]
    fake.log.clear()
    with caplog.at_level(logging.WARNING):
        plug.set_state(state)
    assert fake.log[0] == 'TRAN:ENAB OFF'  # the unchecked write came before the failing drain
    assert 'draining the error queue failed' in caplog.text
    assert fake._values['FREQ'] == '100000000'  # the restore happened
    assert fake._values['TRAN:ENAB'] == 'OFF'


def test_set_state_sends_nothing_else_when_the_first_off_cannot_be_written() -> None:
    plug = _plug()
    fake = _fake(plug)
    state = plug.get_state()
    _flaky(fake, 'write', lambda c: c == 'TRAN:ENAB OFF', OSError('down'))
    fake.log.clear()
    with pytest.raises(RuntimeError, match='nothing restored'):
        plug.set_state(state)
    assert fake.log == ['TRAN:ENAB OFF']


# ── 3. never TRAN:ENAB ON from a snapshot ────────────────────────────────────


def test_set_state_without_a_pulser_entry_still_ends_off() -> None:
    plug = _plug()
    fake = _fake(plug)
    state = plug.get_state()
    del state['TRAN:ENAB']
    plug.apply_setup({'TRAN:ENAB': 'ON'})
    fake.log.clear()
    plug.set_state(state)
    assert fake.log[-2:] == ['TRAN:ENAB OFF', ERR]
    assert fake._values['TRAN:ENAB'] == 'OFF'


def test_docstrings_describe_the_pulser_rule() -> None:
    assert 'pulser off' in (A1580Plug.__doc__ or '')
    assert 'never writes' in (A1580Plug.set_state.__doc__ or '') or 'never' in (
        A1580Plug.set_state.__doc__ or ''
    )


# ── 4. keywords on TRAN:PULS ─────────────────────────────────────────────────


@pytest.mark.parametrize('keyword', ['MIN', 'MAX', 'DEF', 'UP', 'DOWN', 'maximum', 'Default'])
@pytest.mark.parametrize('header', ['TRAN:PULS', 'TRANsmitter:PULSe:LEVel', 'SOUR:TRAN:PULS'])
def test_pulser_voltage_by_keyword_is_refused_before_anything_is_written(
    header: str, keyword: str
) -> None:
    plug = _plug()
    fake = _fake(plug)
    fake.log.clear()
    with pytest.raises(ValueError, match='refuse to set the pulser voltage by keyword'):
        plug.apply_setup({'GAIN': 5, header: keyword, 'FREQ': '50 MHZ'})
    assert fake.log == []  # not even the GAIN before it
    assert plug.query('GAIN?') == '0'


def test_other_headers_keep_the_warn_and_pass_behaviour_for_keywords(
    caplog: pytest.LogCaptureFixture,
) -> None:
    plug = _plug()
    with caplog.at_level(logging.WARNING):
        plug.apply_setup({'GAIN': 'MAX', 'TRAN:FREQ': 'DEF'})
    assert caplog.text.count('cannot be verified') == 2


# ── 5. the example sets the pulser last ──────────────────────────────────────


def test_example_setup_enables_the_pulser_last() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        'example_under_test', ROOT / 'examples' / 'example_test.py'
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    keys = list(module.EXAMPLE_SETUP)
    assert keys[-1] == 'TRAN:ENAB'
    assert keys.count('TRAN:ENAB') == 1
    assert module.EXAMPLE_SETUP['TRAN:ENAB'] == 'ON'


# ── 6. get_state survives a header that raises ───────────────────────────────


class _NoAnswerFake(FakeA1580Resource):
    """No reply (timeout) to one header, like a firmware that lacks it."""

    def __init__(self, header: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.header = header

    def query(self, cmd: str) -> str:
        if cmd == f'{self.header}?':
            self.log.append(cmd)
            self._errors.append('-113,"Undefined header"')
            raise TimeoutError('no reply')
        return super().query(cmd)


def test_get_state_omits_a_header_that_raises_and_drains_the_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = _NoAnswerFake('FILT:HPAS:IND')
    with caplog.at_level(logging.WARNING):
        plug = A1580Plug(resource=fake, restore_state=True)  # the constructor survives
    assert plug._initial_state is not None
    assert 'FILT:HPAS:IND' not in plug._initial_state
    assert len(plug._initial_state) == len(STATE_HEADERS) - 1
    assert 'FILT:HPAS:IND' in caplog.text
    assert 'flush' in fake.log  # the late reply was discarded
    assert fake._errors == []  # the -113 was drained
    # and the restore skips it
    fake.log.clear()
    plug.tearDown()
    assert not any(e.startswith('FILT:HPAS:IND ') for e in fake.log)


def test_get_state_gives_up_after_three_consecutive_transport_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = FakeA1580Resource()
    plug = A1580Plug(resource=fake, restore_state=False)
    original = fake.query
    asked: list[str] = []

    def query(cmd: str) -> str:
        if cmd != ERR:
            asked.append(cmd)
        if len(asked) > 2:  # everything after the second header times out, the drain too
            raise TimeoutError('no reply')
        return original(cmd)

    fake.query = query  # type: ignore[method-assign]
    started = time.monotonic()
    with caplog.at_level(logging.WARNING):
        state = plug.get_state()
    assert time.monotonic() - started < 0.5
    assert list(state) == list(STATE_HEADERS[:2])
    assert asked == [f'{h}?' for h in STATE_HEADERS[:5]]  # two good ones, then three failures
    remaining = len(STATE_HEADERS) - 5
    assert (
        f'link appears dead after 3 consecutive transport errors; '
        f'skipping the remaining {remaining} headers'
    ) in caplog.text


def test_get_state_survives_a_failing_drain(caplog: pytest.LogCaptureFixture) -> None:
    fake = FakeA1580Resource()
    plug = A1580Plug(resource=fake, restore_state=False)
    failed = {'on': False}
    original = fake.query

    def query(cmd: str) -> str:
        if cmd == 'FILT:HPAS:IND?':
            failed['on'] = True
            raise TimeoutError('no reply')
        if cmd == ERR and failed['on']:
            failed['on'] = False
            raise OSError('drain broke')
        return original(cmd)

    fake.query = query  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING):
        state = plug.get_state()
    assert 'FILT:HPAS:IND' not in state
    assert len(state) == len(STATE_HEADERS) - 1
    assert 'draining the error queue after FILT:HPAS:IND? failed' in caplog.text


# ── 7. apply_setup: any exception becomes a failure ──────────────────────────


def test_apply_setup_collects_an_exception_and_goes_on() -> None:
    plug = _plug()
    fake = _fake(plug)
    _flaky(fake, 'write', lambda c: c.startswith('GAIN '), OSError('link hiccup'))
    with pytest.raises(RuntimeError, match=r'GAIN.*OSError.*link hiccup') as exc:
        plug.apply_setup({'FREQ': '50 MHZ', 'GAIN': 5, 'TRAN:DUR': 2})
    assert 'FREQ' not in str(exc.value).replace('apply_setup', '')
    assert plug.query('FREQ?') == '50000000'  # before it
    assert plug.query('TRAN:DUR?') == '2'  # after it
    assert 'flush' in fake.log  # a late reply is discarded after the exception


def test_apply_setup_collects_an_exception_from_the_drain() -> None:
    plug = _plug()
    fake = _fake(plug)
    original = fake.query
    count = {'n': 0}

    def query(cmd: str) -> str:
        if cmd == ERR:
            count['n'] += 1
            if count['n'] == 1:
                raise ValueError('garbled')
        return original(cmd)

    fake.query = query  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match='ValueError: garbled'):
        plug.apply_setup({'GAIN': 5, 'TRAN:DUR': 2})
    assert plug.query('TRAN:DUR?') == '2'


def test_apply_setup_without_flush_support_is_fine() -> None:
    plug = _plug()
    fake = _fake(plug)
    fake.flush = None  # type: ignore[assignment,method-assign]
    fake.clear = None  # type: ignore[assignment,method-assign]
    _flaky(fake, 'write', lambda c: c.startswith('GAIN '), OSError('x'))
    with pytest.raises(RuntimeError, match='GAIN'):
        plug.apply_setup({'GAIN': 5, 'TRAN:DUR': 2})
    assert plug.query('TRAN:DUR?') == '2'


def test_apply_setup_falls_back_to_clear_when_flush_fails() -> None:
    plug = _plug()
    fake = _fake(plug)

    def broken_flush(mask: object = None) -> None:
        raise OSError('not supported')

    fake.flush = broken_flush  # type: ignore[method-assign]
    _flaky(fake, 'write', lambda c: c.startswith('GAIN '), OSError('x'))
    with pytest.raises(RuntimeError):
        plug.apply_setup({'GAIN': 5})
    assert 'clear' in fake.log


# ── 8. set_state collects failures ───────────────────────────────────────────


def test_set_state_continues_after_a_failed_header_and_raises_at_the_end() -> None:
    plug = _plug()
    fake = _fake(plug)
    state = plug.get_state()
    _change_things(plug)
    fake._reject['TRAN:PULS'] = '-222,"Data out of range"'
    fake._reject['FREQ'] = '-222,"Data out of range"'
    fake.log.clear()
    with pytest.raises(RuntimeError, match='set_state failed') as exc:
        plug.set_state(state)
    message = str(exc.value)
    assert 'TRAN:PULS' in message and 'FREQ' in message
    assert message.count('set_state failed') == 1
    writes = [e for e in fake.log if e != ERR]
    assert 'GAIN 0' in writes and 'AVER:DEL:RAND 2000 NS' in writes  # the later headers
    assert writes[-1] == 'TRAN:ENAB OFF'  # the final OFF came before the raise
    assert fake._values['GAIN'] == '0'
    assert fake._values['TRAN:ENAB'] == 'OFF'


def test_set_state_collects_a_transport_error_and_discards_the_late_reply() -> None:
    plug = _plug()
    fake = _fake(plug)
    state = plug.get_state()
    _flaky(fake, 'write', lambda c: c.startswith('TRAN:DUR '), OSError('link hiccup'))
    fake.log.clear()
    with pytest.raises(RuntimeError, match=r'TRAN:DUR.*link hiccup'):
        plug.set_state(state)
    assert 'flush' in fake.log
    assert 'GAIN 0' in fake.log  # went on
    assert fake.log[-2:] == ['TRAN:ENAB OFF', ERR]


def test_set_state_gives_up_after_three_consecutive_transport_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    plug = _plug()
    fake = _fake(plug)
    state = plug.get_state()
    todo = [h for h in STATE_HEADERS if h != 'TRAN:ENAB' and state.get(h, '').strip()]
    todo = [h for h in todo if h != 'AVER:DEL:CONS']  # skipped while AVER:DEL:CONS:AUTO is ON
    seen: list[str] = []
    original = fake.write

    def write(cmd: str) -> None:
        seen.append(cmd)
        if len(seen) > 3:  # the OFF, then the first two headers; everything later times out
            fake.log.append(cmd)
            raise TimeoutError('no reply')
        original(cmd)

    fake.write = write  # type: ignore[method-assign]
    fake.log.clear()
    started = time.monotonic()
    with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError, match='set_state failed'):
        plug.set_state(state)
    assert time.monotonic() - started < 0.5
    assert len(seen) == 3 + 3 + 1  # OFF + two good + three failing headers + the final OFF
    assert seen[-1] == 'TRAN:ENAB OFF'  # the final OFF was still attempted
    remaining = len(todo) - 5
    assert (
        f'link appears dead after 3 consecutive transport errors; '
        f'skipping the remaining {remaining} headers'
    ) in caplog.text


def test_set_state_skips_empty_snapshot_values() -> None:
    plug = _plug()
    fake = _fake(plug)
    state = plug.get_state()
    state['FILT:HPAS:IND'] = ''
    state['TRAN:IMP'] = '  '
    fake.log.clear()
    plug.set_state(state)
    assert not any(e.startswith(('FILT:HPAS:IND ', 'TRAN:IMP ')) for e in fake.log)
    assert plug.check_errors() == []


def test_state_headers_order() -> None:
    assert STATE_HEADERS == (
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


# ── 9. parsing the geometry replies ──────────────────────────────────────────


@pytest.mark.parametrize('reply', ['1024.5', 'abc', '', 'nan', 'inf'])
@pytest.mark.parametrize('method', ['acquire', 'start_stream'])
def test_data_length_must_be_an_integer(reply: str, method: str) -> None:
    plug = _plug()
    fake = _fake(plug)
    fake._values['DATA:LENG'] = reply
    fake.log.clear()
    call = plug.acquire if method == 'acquire' else (lambda: plug.start_stream(lambda s: None))
    with pytest.raises(RuntimeError, match=r'DATA:LENG\? reply') as exc:
        call()
    assert repr(reply) in str(exc.value)
    assert 'STAR AUTO' not in fake.log
    assert fake.connections == []


@pytest.mark.parametrize('reply', ['1024.0', '+1024', '1.024E3'])
def test_data_length_accepts_an_integral_float_text(reply: str) -> None:
    plug = _plug()
    _fake(plug)._values['DATA:LENG'] = reply
    (scan,) = plug.acquire(1)
    assert len(scan.raw) == 1024


@pytest.mark.parametrize(
    ('reply', 'expected'), [('15000', 15000), ('15000.4', 15000), ('15000.6', 15001)]
)
def test_trigger_delay_is_rounded_and_a_fraction_is_flagged(
    reply: str, expected: int, caplog: pytest.LogCaptureFixture
) -> None:
    plug = _plug()
    _fake(plug)._values['TRIG:DEL'] = reply
    with caplog.at_level(logging.WARNING):
        (scan,) = plug.acquire(1)
    assert scan.trigger_delay_ns == expected
    assert ('CONFLICT 4' in caplog.text) is (reply != '15000')


def test_trigger_delay_that_is_not_a_number_raises() -> None:
    plug = _plug()
    _fake(plug)._values['TRIG:DEL'] = 'soon'
    with pytest.raises(RuntimeError, match="TRIG:DEL\\? reply 'soon'"):
        plug.acquire(1)


@pytest.mark.parametrize('method', ['acquire', 'start_stream'])
def test_data_port_equal_to_the_scpi_port_is_refused(method: str) -> None:
    plug = _plug()
    fake = _fake(plug)
    fake._values['DATA:PORT'] = '5025'
    fake.log.clear()
    call = plug.acquire if method == 'acquire' else (lambda: plug.start_stream(lambda s: None))
    with pytest.raises(RuntimeError, match=r'DATA:PORT\? returned the SCPI port 5025.*CONFLICT 1'):
        call()
    assert fake.connections == []
    assert 'STAR AUTO' not in fake.log
    assert plug._stream is None and plug._data_sock is None


def test_data_port_check_uses_the_configured_scpi_port() -> None:
    plug = _plug()
    plug._scpi_port = 2758  # the port the resource was opened with
    with pytest.raises(RuntimeError, match='SCPI port 2758'):
        plug.acquire(1)


# ── 10. acquire: default timeout, error decoration, late device errors ───────


def _record_read_timeouts(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    from a1580_openhtf.stream import FrameReader

    seen: list[float] = []
    original = FrameReader.read

    def read(self: FrameReader, n: int, timeout_s: float) -> list[bytes]:
        seen.append(timeout_s)
        return original(self, n, timeout_s)

    monkeypatch.setattr(FrameReader, 'read', read)
    return seen


@pytest.mark.parametrize(
    ('interval', 'n', 'expected'),
    [('10 MS', 1, 5.0), ('2 S', 3, 14.0), ('100 US', 100, 5.0), ('1 S', 10, 22.0)],
)
def test_acquire_default_timeout_follows_the_trigger_interval(
    monkeypatch: pytest.MonkeyPatch, interval: str, n: int, expected: float
) -> None:
    plug = _plug()
    plug.apply_setup({'TRIG:INT': interval})
    seen = _record_read_timeouts(monkeypatch)
    plug.acquire(n)
    assert seen == [pytest.approx(expected)]


def test_acquire_explicit_timeout_does_not_query_the_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plug = _plug()
    fake = _fake(plug)
    seen = _record_read_timeouts(monkeypatch)
    fake.log.clear()
    plug.acquire(1, timeout_s=3.0)
    assert seen == [3.0]
    assert 'TRIG:INT?' not in fake.log


def test_acquire_default_timeout_falls_back_when_the_interval_is_unreadable(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    plug = _plug()
    _fake(plug)._values['TRIG:INT'] = 'garbage'
    seen = _record_read_timeouts(monkeypatch)
    with caplog.at_level(logging.WARNING):
        plug.acquire(1)
    assert seen == [5.0]
    assert 'TRIG:INT? failed' in caplog.text


def test_acquire_connection_error_carries_the_same_suffix() -> None:
    fake = FakeA1580Resource()

    def factory(host: str, port: int) -> Any:
        sock = fake.data_socket_factory(host, port)
        sock.shut = True  # recv returns b'': the device closed the connection
        return sock

    plug = A1580Plug(resource=fake, data_socket_factory=factory, restore_state=False)
    with pytest.raises(ConnectionError, match=r'\(DATA:LENG=1024, port=2758\)'):
        plug.acquire(1, timeout_s=1.0)
    assert plug._data_sock is None


def test_acquire_failed_stop_still_raises() -> None:
    plug = _plug()
    fake = _fake(plug)
    _flaky(fake, 'write', lambda c: c == 'STOP', OSError('link down'))
    with pytest.raises(RuntimeError, match='STOP failed'):
        plug.acquire(1)


# ── 11. framing statistics in the log ────────────────────────────────────────


def test_acquire_logs_frame_stats_at_info(caplog: pytest.LogCaptureFixture) -> None:
    plug = _plug()
    with caplog.at_level(logging.INFO):
        plug.acquire(2)
    assert 'acquire: frame stats' in caplog.text
    assert 'framing problems' not in caplog.text


def test_acquire_warns_about_dropped_bytes(caplog: pytest.LogCaptureFixture) -> None:
    plug = _plug(garbage_prefix=b'xx')
    with caplog.at_level(logging.INFO):
        plug.acquire(1)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        'framing problems' in r.getMessage() and 'dropped_bytes=2' in r.getMessage()
        for r in warnings
    )


class _BytesSocket:
    """Serves `data` once, then times out."""

    def __init__(self, data: bytes) -> None:
        self.data = data

    def settimeout(self, value: float | None) -> None:
        pass

    def recv(self, n: int) -> bytes:
        if self.data:
            out, self.data = self.data, b''
            return out
        time.sleep(0.001)
        raise TimeoutError('nothing more')

    def shutdown(self, how: int) -> None:
        pass

    def close(self) -> None:
        pass


def test_acquire_warns_about_misaligned_packets(caplog: pytest.LogCaptureFixture) -> None:
    fake = FakeA1580Resource()  # DATA:LENG 1024 ...
    wide = build_packet([0] * 2048)  # ... but the stream carries 2048 samples per packet

    plug = A1580Plug(
        resource=fake, data_socket_factory=lambda h, p: _BytesSocket(wide), restore_state=False
    )
    with caplog.at_level(logging.INFO):
        plug.acquire(1)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any('framing problems' in m and 'misaligned=1' in m for m in warnings)


def test_stop_stream_logs_frame_stats_and_warns(caplog: pytest.LogCaptureFixture) -> None:
    plug = _plug(garbage_prefix=b'xx')
    got: list[AScan] = []
    plug.start_stream(got.append)
    assert _wait_for(lambda: len(got) >= 1)
    with caplog.at_level(logging.INFO):
        plug.stop_stream()
    assert 'stop_stream: frame stats' in caplog.text
    assert any(
        r.levelno == logging.WARNING and 'framing problems' in r.getMessage()
        for r in caplog.records
    )


# ── 12. values_match false positives ─────────────────────────────────────────


@pytest.mark.parametrize(
    ('header', 'sent', 'reply', 'expected'),
    [
        ('FILT:HPAS:IND', '1', 'ON', False),
        ('FILT:HPAS:IND', '0', 'OFF', False),
        ('TRAN:IMP', '200', '2000 OHM', False),
        ('GAIN:TGC:MODE', 'LINear', 'LINEARX', False),
        ('TRIG:DEL', '2147483647 NS', '2147483000', False),
        ('FREQ', '', '', False),
        ('MODE', '', 'MASTER', False),
        ('GAIN', ' ', '10', False),
        # the existing positives still hold
        ('TRAN:ENAB', 'ON', '1', True),
        ('TRAN:DAMP', 'OFF', '0', True),
        ('GAIN:TGC:MODE', 'LINear', 'LINEAR', True),
        ('GAIN:TGC:MODE', 'LINear', 'LIN', True),
        ('TRIG:MODE', 'INTERNAL', 'INT', True),
        ('TRIG:MODE', 'INT', 'INTERNAL', True),
        ('TRIG:DEL', '2147483647 NS', '2147483647', True),
        ('FREQ', '100 MHZ', '100000000', True),
        # integer replies: half a unit, in the reply unit
        ('TRIG:DEL', '15 US', '15000.4', True),
        ('TRIG:DEL', '15 US', '15001', False),
        ('FREQ', '100 MHZ', '100000001', False),
        ('TRAN:FREQ', '2500 KHz', '2500000.3', True),
        ('DATA:LENG', '8192', '8193', False),
        ('AVER:COUN', '3', '3.4', True),
        ('FILT:HPAS:IND', '2', '3', False),
        ('TRAN:PULS', '20 V', '20.4', True),
        ('TRAN:PULS', '20 V', '21', False),
        # float replies keep the relative tolerance
        ('GAIN', '10', '10.0000001', True),
        ('TRIG:INT', '100000 US', '100.0000001E-3', True),
        # booleans only for boolean headers
        ('TRAN:IMP', '1', 'ON', False),
        ('GAIN:PRE:SPLIT', 'ON', '1', True),
        ('AVER:DEL:CONS:AUTO', 'OFF', '0', True),
        ('TRAN:REV', 'ON', '0', False),
        # an enumeration needs an alphabetic token of 3+ letters
        ('TRAN:IMP', '200', '2000', False),
        ('MODE', 'MA', 'MASTER', False),
    ],
)
def test_values_match_false_positives_and_tolerances(
    header: str, sent: str, reply: str, expected: bool
) -> None:
    assert values_match(header, sent, reply) is expected


def test_boolean_and_integer_header_sets() -> None:
    from a1580_openhtf.plug import BOOLEAN_HEADERS, INTEGER_REPLY_HEADERS

    assert BOOLEAN_HEADERS == {
        'TRAN:ENAB',
        'TRAN:REV',
        'TRAN:DAMP',
        'GAIN:PRE:COMB',
        'GAIN:PRE:SPLIT',
        'AVER:DEL:CONS:AUTO',
    }
    assert INTEGER_REPLY_HEADERS == {
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


# ── 13. normalize_header: only known nodes ───────────────────────────────────

_ALL_FORMS = {
    'TRAN': 'TRANSMITTER',
    'PULS': 'PULSE',
    'FREQ': 'FREQUENCY',
    'ENAB': 'ENABLE',
    'REV': 'REVERSE',
    'IMP': 'IMPEDANCE',
    'TRIG': 'TRIGGERING',
    'INT': 'INTERVAL',
    'DEL': 'DELAY',
    'LEV': 'LEVEL',
    'PRE': 'PREAMP',
    'COMB': 'COMBINED',
    'LIN': 'LINEAR',
    'ARB': 'ARBITRARY',
    'AVER': 'AVERAGE',
    'COUN': 'COUNT',
    'CONS': 'CONSTANT',
    'VAL': 'VALUE',
    'RAND': 'RANDOM',
    'FILT': 'FILTER',
    'HPAS': 'HPASS',
    'IND': 'INDEX',
    'LENG': 'LENGTH',
    'MEM': 'MEMORY',
    'CLE': 'CLEAR',
    'SYST': 'SYSTEM',
    'ERR': 'ERROR',
    'VERS': 'VERSION',
    'STAR': 'START',
    'DUR': 'DURATION',
}


@pytest.mark.parametrize(('short', 'long'), sorted(_ALL_FORMS.items()))
def test_every_node_has_a_short_and_a_long_form(short: str, long: str) -> None:
    from a1580_openhtf.plug import _NODES

    assert _NODES[short] == short
    assert _NODES[long] == short


@pytest.mark.parametrize(
    'header',
    [
        'GAINX',
        'FREQUE',
        'TRAN:PULSX',
        'TRANSMIT:PULS',
        'TRAN:PULS:LEVELX',
        'NOPE:HDR',
        'ZZZ:NOPE',
        '',
        ':',
        'FRE',
    ],
)
def test_normalize_header_rejects_unknown_nodes(header: str) -> None:
    with pytest.raises(ValueError):
        normalize_header(header)


def test_normalize_header_error_names_the_node() -> None:
    with pytest.raises(ValueError, match="'GAINX'"):
        normalize_header('GAINX')


@pytest.mark.parametrize(
    ('header', 'normal'),
    [
        ('*IDN?', '*IDN'),
        ('*opc?', '*OPC'),
        ('MOD', 'MODE'),
        ('SOURce:GAIN:PREamp:SPL', 'GAIN:PRE:SPLIT'),
        ('Sense:Average:Delay:Constant:Value', 'AVER:DEL:CONS'),
        ('SYSTEM:ERROR:NEXT', 'SYST:ERR'),
        ('TRIGGERING:INTERVAL', 'TRIG:INT'),
    ],
)
def test_normalize_header_more_forms(header: str, normal: str) -> None:
    assert normalize_header(header) == normal


def test_fake_queues_113_for_unknown_nodes_and_stores_nothing() -> None:
    plug = _plug()
    fake = _fake(plug)
    before = dict(fake._values)
    fake.write('GAINX 10')
    fake.write('FREQUE 50 MHZ')
    errors = plug.check_errors()
    assert [e.split(',')[0] for e in errors] == ['-113', '-113']
    assert fake._values == before


def test_apply_setup_with_an_unknown_node_writes_nothing() -> None:
    plug = _plug()
    fake = _fake(plug)
    fake.log.clear()
    with pytest.raises(RuntimeError, match=r"GAINX.*unknown SCPI node 'GAINX'"):
        plug.apply_setup({'GAINX': 10})
    assert fake.log == []


def test_apply_setup_unknown_node_does_not_stop_the_other_keys() -> None:
    plug = _plug()
    fake = _fake(plug)
    with pytest.raises(RuntimeError, match='FREQUE'):
        plug.apply_setup({'GAIN': 4, 'FREQUE': '50 MHZ', 'TRAN:DUR': 2})
    assert not any(e.startswith('FREQUE') for e in fake.log)
    assert plug.query('GAIN?') == '4' and plug.query('TRAN:DUR?') == '2'


# ── 14. per-stream state ─────────────────────────────────────────────────────


def test_stream_state_lives_in_one_object() -> None:
    plug = _plug()
    got: list[AScan] = []
    assert plug._stream is None
    plug.start_stream(got.append)
    stream = plug._stream
    assert stream is not None
    assert stream.thread is not None and stream.thread.name == 'a1580-stream'
    assert stream.socket is _fake(plug).sockets[0]
    assert stream.reader.stats is not None
    assert not stream.stop_event.is_set()
    assert _wait_for(lambda: stream.count >= 2)
    n = plug.stop_stream()
    assert n == stream.count == len(got)
    assert stream.stop_event.is_set()
    assert plug._stream is None
    assert not hasattr(plug, '_stream_thread') and not hasattr(plug, '_stream_count')


def test_a_second_stream_does_not_inherit_the_first_ones_state() -> None:
    plug = _plug()
    plug.start_stream(lambda s: None)
    first = plug._stream
    assert first is not None and _wait_for(lambda: first.count >= 1)
    plug.stop_stream()
    plug.start_stream(lambda s: None)
    second = plug._stream
    assert second is not None and second is not first
    assert second.stop_event is not first.stop_event
    plug.stop_stream()


def test_a_stop_stream_that_cannot_join_leaves_a_zombie() -> None:
    plug = _plug()
    gate = threading.Event()
    entered = threading.Event()

    def stuck(scan: AScan) -> None:
        entered.set()
        gate.wait(10)

    try:
        plug.start_stream(stuck, timeout_s=0.05)  # join timeout = 1.05 s
        assert entered.wait(2.0)
        with pytest.raises(RuntimeError, match='did not stop in time'):
            plug.stop_stream()
        zombie = plug._stream
        assert zombie is not None and zombie.zombie
        with pytest.raises(RuntimeError, match='previous stream thread still alive'):
            plug.start_stream(lambda s: None)
        with pytest.raises(RuntimeError, match='previous stream thread still alive'):
            plug.acquire(1)
        with pytest.raises(RuntimeError, match='previous stream thread still alive'):
            plug.stop_stream()
    finally:
        gate.set()
    assert zombie.thread is not None
    zombie.thread.join(2.0)
    assert not zombie.thread.is_alive()
    plug.start_stream(lambda s: None)  # the dead zombie is cleared
    assert plug._stream is not zombie
    plug.stop_stream()


def test_the_reader_thread_records_any_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    from a1580_openhtf.stream import FrameReader

    def explode(self: FrameReader, n: int, timeout_s: float) -> list[bytes]:
        raise KeyError('reader exploded')

    monkeypatch.setattr(FrameReader, 'read', explode)
    plug = _plug()
    plug.start_stream(lambda s: None)
    stream = plug._stream
    assert stream is not None and stream.thread is not None
    stream.thread.join(2.0)
    assert not stream.thread.is_alive()
    with pytest.raises(KeyError, match='reader exploded'):
        plug.stop_stream()


def test_a_bad_packet_in_the_stream_is_a_stream_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def bad(cls: Any, packet: bytes, fs: float, delay: int) -> AScan:
        raise ValueError('bad packet')

    monkeypatch.setattr(AScan, 'from_packet', classmethod(bad))
    plug = _plug()
    plug.start_stream(lambda s: None)
    stream = plug._stream
    assert stream is not None and stream.thread is not None
    stream.thread.join(2.0)
    assert stream.callback_failure is None
    with pytest.raises(ValueError, match='bad packet'):
        plug.stop_stream()


def test_callback_failure_is_reraised_after_logging_the_other_problems(
    caplog: pytest.LogCaptureFixture,
) -> None:
    plug = _plug()
    fake = _fake(plug)

    def boom(scan: AScan) -> None:
        raise ValueError('callback exploded')

    plug.start_stream(boom)
    stream = plug._stream
    assert stream is not None and stream.thread is not None
    stream.thread.join(2.0)
    stream.failure = TimeoutError('also stalled')
    _flaky(fake, 'write', lambda c: c == 'STOP', OSError('STOP lost'))
    with caplog.at_level(logging.WARNING):
        with pytest.raises(ValueError, match='callback exploded'):
            plug.stop_stream()
    assert 'besides the callback failure' in caplog.text
    assert 'also stalled' in caplog.text and 'STOP lost' in caplog.text


def test_a_device_side_close_ends_the_stream_and_acquire_says_so() -> None:
    plug = _plug()
    plug.start_stream(lambda s: time.sleep(0.001))
    stream = plug._stream
    assert stream is not None and stream.thread is not None
    assert _wait_for(lambda: stream.count >= 1)
    _fake(plug).sockets[0].shut = True  # the device closes the data socket
    stream.thread.join(2.0)
    assert not stream.thread.is_alive()
    assert stream.peer_closed
    with pytest.raises(RuntimeError, match='stream has ended.*closed the data socket') as exc:
        plug.acquire(1)
    assert 'is running' not in str(exc.value)
    with pytest.raises(RuntimeError, match='stream has ended'):
        plug.start_stream(lambda s: None)
    with pytest.raises(ConnectionError):
        plug.stop_stream()
    assert len(plug.acquire(1)) == 1  # and everything works again


def test_acquire_while_running_still_says_running() -> None:
    plug = _plug()
    plug.start_stream(lambda s: None)
    try:
        with pytest.raises(RuntimeError, match='while a stream is running'):
            plug.acquire(1)
    finally:
        plug.stop_stream()


# ── 15. the SCPI lock ────────────────────────────────────────────────────────


def test_write_and_query_hold_the_scpi_lock() -> None:
    plug = _plug()
    fake = _fake(plug)
    owned: list[bool] = []
    original_write, original_query = fake.write, fake.query

    def write(cmd: str) -> None:
        owned.append(plug._scpi_lock._is_owned())  # type: ignore[attr-defined]
        original_write(cmd)

    def query(cmd: str) -> str:
        owned.append(plug._scpi_lock._is_owned())  # type: ignore[attr-defined]
        return original_query(cmd)

    fake.write = write  # type: ignore[method-assign]
    fake.query = query  # type: ignore[method-assign]
    plug.write('GAIN 3')
    assert plug.query('GAIN?') == '3'
    plug.check_errors()
    assert owned and all(owned)
    assert not plug._scpi_lock._is_owned()  # type: ignore[attr-defined]


def test_a_callback_may_query_while_the_stream_runs() -> None:
    plug = _plug()
    replies: list[str] = []

    def callback(scan: AScan) -> None:
        replies.append(plug.query('GAIN?'))

    plug.start_stream(callback)
    for _ in range(20):
        assert plug.query('FREQ?') == '100000000'  # the main thread uses the resource too
    assert _wait_for(lambda: len(replies) >= 5)
    assert plug.stop_stream() >= 5
    assert set(replies) == {'0'}


def test_class_docstring_mentions_the_shared_resource_and_the_lock() -> None:
    doc = A1580Plug.__doc__ or ''
    assert '_scpi_lock' in doc and 'shared' in doc and 'callback' in doc


# ── 16. visa errors from the fake ────────────────────────────────────────────


def test_apply_setup_collects_a_visa_timeout_and_applies_later_keys() -> None:
    pyvisa = pytest.importorskip('pyvisa')
    plug = _plug(visa_errors=True)
    fake = _fake(plug)
    with pytest.raises(
        RuntimeError, match=r'SYST:VERS.*no answer to SYST:VERS\?: VisaIOError'
    ) as exc:
        # SYST:VERS is a valid node chain the fake has no value for: rejected, query times out
        plug.apply_setup({'GAIN': 6, 'SYST:VERS': 1, 'FREQ': '50 MHZ'})
    assert 'GAIN' not in str(exc.value).split('apply_setup failed:')[1]
    assert plug.query('GAIN?') == '6'
    assert plug.query('FREQ?') == '50000000'  # applied after the failure
    assert 'flush' in fake.log
    assert plug.check_errors() == []
    assert issubclass(pyvisa.errors.VisaIOError, Exception)


def test_get_state_with_visa_errors_omits_the_unanswered_header() -> None:
    pytest.importorskip('pyvisa')

    class Lacking(FakeA1580Resource):
        def query(self, cmd: str) -> str:
            if cmd == 'GAIN:PRE:SPLIT?':
                self._errors.append('-113,"Undefined header"')
                raise self._timeout_error('no reply')
            return super().query(cmd)

    plug = A1580Plug(resource=Lacking(visa_errors=True), restore_state=True)
    assert plug._initial_state is not None
    assert 'GAIN:PRE:SPLIT' not in plug._initial_state
