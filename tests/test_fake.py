"""Tests for fake_resource.py (SPEC.md section 5, test_fake.py). No hardware."""

from __future__ import annotations

import socket
import struct

import numpy as np
import pytest

from a1580_openhtf.fake_resource import DEFAULTS, FakeA1580Resource
from a1580_openhtf.plug import STATE_HEADERS, normalize_header
from a1580_openhtf.stream import HEADER_SIZE, FrameReader, packet_size, parse_header

ERR = 'SYSTem:ERRor?'


def _drain(fake: FakeA1580Resource) -> list[str]:
    out = []
    while (reply := fake.query(ERR)) != '0,"No error"':
        out.append(reply)
    return out


# ── command handling ─────────────────────────────────────────────────────────


def test_defaults_cover_every_state_header_and_are_normalised() -> None:
    for header in STATE_HEADERS:
        assert normalize_header(header) == header
        assert header in DEFAULTS, header
    assert set(DEFAULTS) - set(STATE_HEADERS) == {'DATA:PORT'}
    assert all(normalize_header(h) == h for h in DEFAULTS)


def test_vendor_reply_formats() -> None:
    fake = FakeA1580Resource()
    assert fake.query('*IDN?') == 'ACS-Solutions GmbH,A1580-HF,100500,1.6.b41'
    assert fake.query('FREQ?') == '100000000'
    assert fake.query('DATA:PORT?') == '2758'
    assert fake.query('TRIG:INT?') == '10.0E-3'
    assert fake.query('TRIG:DEL?') == '15000'
    assert fake.query('*OPC?') == '1'
    assert fake.query('*TST?') == '0'
    assert fake.query(ERR) == '0,"No error"'


@pytest.mark.parametrize(
    ('cmd', 'query', 'reply'),
    [
        ('FREQ 50 MHZ', 'FREQ?', '50000000'),
        ('SOURce:FREQuency 2500 KHz', 'FREQ?', '2500000'),
        ('TRAN:FREQ 2500 KHz', 'TRAN:FREQ?', '2500000'),
        ('TRIG:INT 100000 US', 'TRIG:INT?', '100.0E-3'),
        ('TRIG:INT 2 MS', 'TRIG:INT?', '2.0E-3'),
        ('TRIG:DEL 15 US', 'TRIG:DEL?', '15000'),
        ('TRIG:DEL 0 NS', 'TRIG:DEL?', '0'),
        ('AVERage:DELay:RANDom 2000 NS', 'AVER:DEL:RAND?', '2.0E-6'),
        ('TRAN:PULS 100 V', 'TRAN:PULS?', '100'),
        ('TRANsmitter:PULSe:LEVel 35 V', 'TRAN:PULS?', '35'),
        ('GAIN 10', 'GAIN?', '10'),
        ('TRAN:ENAB 1', 'TRAN:ENAB?', 'ON'),
        ('TRAN:REVerse OFF', 'TRAN:REV?', 'OFF'),
        ('TRAN:DAMP:ENAB ON', 'TRAN:DAMP?', '1'),
        ('GAIN:PRE:COMB 0', 'GAIN:PRE:COMB?', '0'),
        ('TRIG:MODE INTERNAL', 'TRIG:MODE?', 'INT'),
        ('MODE MASTer', 'MODE?', 'MASTER'),
        ('MODE SLAVe', 'MODE?', 'SLAVE'),
        ('TRAN:TYPE DUAL', 'TRAN:TYPE?', 'DUAL'),
        ('GAIN:TGC:MODE LINear', 'GAIN:TGC:MODE?', 'LIN'),
        ('TRAN:IMP 200', 'TRAN:IMP?', '200'),
        ('TRAN:IMP HIGH', 'TRAN:IMP?', 'HIGH'),
        ('GAIN:TGC:LIN 20, 0.1', 'GAIN:TGC:LIN?', '20.0, 0.1'),
        ('GAIN:TGC:ARB 0,5,2,20', 'GAIN:TGC:ARB?', '0,5,2,20'),
        ('DATA:LENG 8192', 'DATA:LENG?', '8192'),
    ],
)
def test_write_converts_to_the_reply_format(cmd: str, query: str, reply: str) -> None:
    fake = FakeA1580Resource()
    fake.write(cmd)
    assert _drain(fake) == []
    assert fake.query(query) == reply


def test_log_is_verbatim() -> None:
    fake = FakeA1580Resource()
    fake.write('FREQ 50 MHZ')
    fake.query('FREQ?')
    assert fake.log == ['FREQ 50 MHZ', 'FREQ?']


def test_unknown_header_queues_113_and_query_raises() -> None:
    fake = FakeA1580Resource()
    fake.write('FOO:BAR 1')
    assert fake.query(ERR) == '-113,"Undefined header;Command: FOO:BAR 1"'
    assert fake.query(ERR) == '0,"No error"'
    with pytest.raises(TimeoutError):
        fake.query('FOO:BAR?')
    assert fake.query(ERR).startswith('-113,')


def test_bad_value_is_an_error_not_a_crash() -> None:
    fake = FakeA1580Resource()
    fake.write('FREQ banana')
    assert len(_drain(fake)) == 1
    assert fake.query('FREQ?') == '100000000'


def test_min_max_def_are_accepted_and_change_nothing() -> None:
    fake = FakeA1580Resource()
    fake.write('FREQ MAX')
    assert _drain(fake) == []
    assert fake.query('FREQ?') == '100000000'


def test_data_port_is_read_only() -> None:
    fake = FakeA1580Resource()
    fake.write('DATA:PORT 1')
    assert len(_drain(fake)) == 1
    assert fake.query('DATA:PORT?') == '2758'


def test_rst_restores_defaults_and_keeps_the_error_queue() -> None:
    fake = FakeA1580Resource(length=2048)
    fake.write('FREQ 10 MHZ')
    fake.write('AVER:COUN 4')
    fake.write('STAR AUTO')
    fake.write('NOPE 1')
    fake.write('*RST')
    assert fake.query('FREQ?') == DEFAULTS['FREQ']
    assert fake.query('AVER:COUN?') == DEFAULTS['AVER:COUN']
    assert fake.query('DATA:LENG?') == DEFAULTS['DATA:LENG']
    assert not fake.started
    assert len(_drain(fake)) == 1  # *RST does not clear the queue, *CLS does
    fake.write('NOPE 1')
    fake.write('*CLS')
    assert _drain(fake) == []


def test_constant_delay_while_auto_is_221() -> None:
    fake = FakeA1580Resource()
    assert fake.query('AVER:DEL:CONS:AUTO?') == 'ON'
    fake.write('AVER:DEL:CONS 50 US')
    assert _drain(fake) == ['-221,"Settings conflict"']
    assert fake.query('AVER:DEL:CONS?') == '10.0E-6'  # old value kept
    fake.write('AVER:DEL:CONS:AUTO OFF')
    fake.write('AVER:DEL:CONS 50 US')
    assert _drain(fake) == []
    assert fake.query('AVER:DEL:CONS?') == '50.0E-6'


def test_reject_queues_the_error_and_keeps_the_value() -> None:
    fake = FakeA1580Resource(reject={'TRANsmitter:PULSe': '-222,"Data out of range"'})
    fake.write('TRAN:PULS 50 V')
    assert fake.query(ERR) == '-222,"Data out of range"'
    assert fake.query('TRAN:PULS?') == '20'
    fake.write('GAIN 5')  # others unaffected
    assert fake.query('GAIN?') == '5'


def test_close_and_attributes() -> None:
    fake = FakeA1580Resource()
    assert not fake.closed
    assert (fake.encoding, fake.read_termination, fake.write_termination) == (
        'iso-8859-1',
        '\r\n',
        '\r\n',
    )
    fake.close()
    assert fake.closed


def test_constructor_validates() -> None:
    with pytest.raises(ValueError):
        FakeA1580Resource(signal='sine')
    with pytest.raises(ValueError):
        FakeA1580Resource(chunk=0)


def test_custom_idn() -> None:
    assert FakeA1580Resource(idn='a,b').query('*IDN?') == 'a,b'


# ── data channel ─────────────────────────────────────────────────────────────


def _read(fake: FakeA1580Resource, n: int) -> list[bytes]:
    sock = fake.data_socket_factory('h', 2758)
    return FrameReader(sock, int(fake.query('DATA:LENG?'))).read(n, 1.0)


def test_socket_is_silent_until_start_and_after_stop() -> None:
    fake = FakeA1580Resource()
    sock = fake.data_socket_factory('somehost', 2758)
    assert fake.connections == [('somehost', 2758)]
    assert fake.connect_count == 1
    with pytest.raises(socket.timeout):
        sock.recv(100)
    fake.write('STAR AUTO')
    assert fake.started and fake.start_count == 1
    assert len(sock.recv(65536)) == packet_size(1024)
    fake.write('STOP')
    assert not fake.started and fake.stop_count == 1
    with pytest.raises(socket.timeout):
        sock.recv(100)


def test_socket_methods() -> None:
    fake = FakeA1580Resource()
    sock = fake.data_socket_factory('h', 1)
    sock.settimeout(1.5)
    assert sock.timeout == 1.5
    sock.shutdown(socket.SHUT_RDWR)
    fake.write('STAR AUTO')
    assert sock.recv(10) == b''
    sock.close()
    with pytest.raises(OSError):
        sock.recv(10)


@pytest.mark.parametrize('length', [1024, 2048, 5000])
def test_generator_honours_data_leng(length: int) -> None:
    fake = FakeA1580Resource(length=length)
    fake.write('STAR AUTO')
    (packet,) = _read(fake, 1)
    assert len(packet) == packet_size(length)
    assert len(np.frombuffer(packet, dtype='<i2', offset=HEADER_SIZE)) == length
    fake.write('DATA:LENG 3000')
    (packet,) = _read(fake, 1)
    assert len(packet) == packet_size(3000)


def test_generator_burst_shape_gain_and_header() -> None:
    fake = FakeA1580Resource()
    fake.write('STAR AUTO')
    (p0,) = _read(fake, 1)
    raw0 = np.frombuffer(p0, dtype='<i2', offset=HEADER_SIZE)
    start = 102  # 10 % of 1024, truncated
    assert not raw0[: start + 1].any()  # silent before the burst
    assert raw0[start + 1 : start + 40].any()
    assert abs(int(raw0[start + 5 :].min())) < 2000  # 0 dB: amplitude 1000 counts
    assert parse_header(p0).ascan_count == 0
    fake.write('GAIN 20')
    fake.write('AVER:COUN 3')
    (p1,) = _read(fake, 1)
    raw1 = np.frombuffer(p1, dtype='<i2', offset=HEADER_SIZE)
    assert int(np.abs(raw1).max()) > 5 * int(np.abs(raw0).max())  # 20 dB is a factor 10
    assert parse_header(p1).ascan_count == 3


def test_generator_is_deterministic_and_clips() -> None:
    a, b = FakeA1580Resource(), FakeA1580Resource()
    for fake in (a, b):
        fake.write('GAIN 80')
        fake.write('STAR AUTO')
    (pa,), (pb,) = _read(a, 1), _read(b, 1)
    assert pa == pb
    raw = np.frombuffer(pa, dtype='<i2', offset=HEADER_SIZE)
    assert int(raw.max()) == 32767 or int(raw.min()) == -32768


def test_zeros_signal() -> None:
    fake = FakeA1580Resource(signal='zeros')
    fake.write('STAR AUTO')
    (packet,) = _read(fake, 1)
    assert not np.frombuffer(packet, dtype='<i2', offset=HEADER_SIZE).any()


def test_none_signal_never_produces() -> None:
    fake = FakeA1580Resource(signal='none')
    fake.write('STAR AUTO')
    with pytest.raises(socket.timeout):
        fake.data_socket_factory('h', 1).recv(10)


def test_packet_numbers_increment_and_wrap_at_256() -> None:
    fake = FakeA1580Resource(length=16)
    fake.write('STAR AUTO')
    packets = _read(fake, 300)
    numbers = [parse_header(p).packet_number for p in packets]
    assert numbers[:3] == [0, 1, 2]
    assert numbers[255:258] == [255, 0, 1]
    assert numbers == [i % 256 for i in range(300)]


@pytest.mark.parametrize('chunk', [1, 7, 25])
def test_chunk_limits_recv(chunk: int) -> None:
    fake = FakeA1580Resource(length=16, chunk=chunk)
    fake.write('STAR AUTO')
    sock = fake.data_socket_factory('h', 1)
    sizes = [len(sock.recv(65536)) for _ in range(30)]
    assert max(sizes) == chunk
    assert sizes[0] == chunk
    # whole packets still come out in order through the reader
    packets = FrameReader(fake.data_socket_factory('h', 1), 16).read(3, 5.0)
    assert [parse_header(p).packet_number for p in packets] == [0, 1, 2]


def test_garbage_prefix_comes_once_before_the_first_packet() -> None:
    fake = FakeA1580Resource(length=16, garbage_prefix=b'xxxx')
    fake.write('STAR AUTO')
    sock = fake.data_socket_factory('h', 1)
    first = sock.recv(65536)
    assert first.startswith(b'xxxx' + b'FtH1')
    assert b'xxxx' not in sock.recv(65536)
    reader = FrameReader(fake.data_socket_factory('h', 1), 16)
    assert len(reader.read(2, 1.0)) == 2
    assert reader.stats.dropped_bytes == 4
    assert reader.stats.resyncs == 1


def test_packet_layout_is_the_documented_one() -> None:
    fake = FakeA1580Resource(length=4)
    fake.write('STAR AUTO')
    (packet,) = _read(fake, 1)
    assert packet[:4] == b'FtH1'
    assert len(packet) == struct.calcsize('<4s3IH2B6B2B') + 8
