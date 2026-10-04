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
    # the first drain is the one that discards stale errors, then one drain per write
    assert fake.log[0] == ERR
    assert fake.log[1::2] == writes
    assert fake.log[2::2] == [ERR] * len(writes)
    heads = [w.split(' ', 1)[0] for w in writes]
    expected = [h for h in STATE_HEADERS if h != 'AVER:DEL:CONS']
    assert heads == expected
    assert 'FREQ 100 MHZ' in writes
    assert 'TRAN:FREQ 5000 KHZ' in writes
    assert 'TRIG:INT 10000 US' in writes
    assert 'TRIG:DEL 15000 NS' in writes
    assert 'TRAN:PULS 20 V' in writes
    assert 'GAIN 0' in writes
    assert 'AVER:DEL:RAND 2000 NS' in writes


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
    assert fake.log[0] == 'STOP'
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
    assert fake.log == ['STOP']
    assert fake._values == changed
    assert fake.closed
    assert not any(e.endswith('?') for e in fake.log)


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


def test_acquire_reports_instrument_errors() -> None:
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
    with pytest.raises(RuntimeError, match='-200'):
        plug.acquire(1)
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
    thread = plug._stream_thread
    assert thread is not None and thread.daemon
    assert _wait_for(lambda: len(got) >= 5, 1.0)
    count = plug.stop_stream()
    assert count == len(got) >= 5
    assert not thread.is_alive()
    assert not fake.started
    assert fake.sockets[0].closed
    assert plug._data_sock is None and plug._stream_thread is None
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
        thread = plug._stream_thread
        assert thread is not None
        thread.join(2.0)
        assert not thread.is_alive()  # the failure stopped the stream by itself
    assert 'callback raised' in caplog.text
    with pytest.raises(ValueError, match='callback exploded'):
        plug.stop_stream()
    assert calls == [1]
    assert not fake.started  # cleanup still ran
    assert plug._data_sock is None and plug._stream_thread is None
    assert plug.stop_stream() == 0  # the failure is reported once
    assert len(plug.acquire(1)) == 1


def test_stream_stall_surfaces_as_timeout() -> None:
    plug = _plug(signal='none')
    plug.start_stream(lambda s: None, timeout_s=0.1)
    thread = plug._stream_thread
    assert thread is not None
    thread.join(2.0)
    assert not thread.is_alive()
    with pytest.raises(TimeoutError, match='no A-scan'):
        plug.stop_stream()


def test_stream_connection_loss_surfaces() -> None:
    plug = _plug()
    plug.start_stream(lambda s: time.sleep(0.001))
    fake = _fake(plug)
    assert _wait_for(lambda: plug._stream_count >= 1)
    fake.sockets[0].shut = True  # the peer closes: recv returns b''
    thread = plug._stream_thread
    assert thread is not None
    thread.join(2.0)
    with pytest.raises(ConnectionError):
        plug.stop_stream()


def test_teardown_stops_a_running_stream() -> None:
    fake = FakeA1580Resource()
    plug = A1580Plug(resource=fake, restore_state=True)
    plug.start_stream(lambda s: None)
    thread = plug._stream_thread
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
    fake._values['DATA:PORT'] = '5025'
    A1580Plug(resource=fake, restore_state=False).acquire(1)
    assert fake.connections[0][1] == 5025


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
    assert set(a1580_openhtf.__all__) >= {'A1580Plug', 'AScan', 'AScanHeader'}


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
