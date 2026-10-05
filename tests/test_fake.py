"""Tests for fake_resource.py (SPEC.md section 5, test_fake.py). No hardware."""

from __future__ import annotations

import socket
import struct

import numpy as np
import pytest

from a1580_openhtf.fake_resource import DEFAULTS, POWER_ON, FakeA1580Resource
from a1580_openhtf.plug import STATE_HEADERS, normalize_header
from a1580_openhtf.stream import (
    HEADER_SIZE,
    FrameReader,
    build_packet,
    packet_size,
    parse_header,
    split_packets,
)

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
    assert fake.query('*IDN?') == 'ACS-Solutions GmbH,A1580-HF,100500,1.16 (861f022a)'
    assert fake.query('FREQ?') == '100000000'
    assert fake.query('DATA:PORT?') == '2758'
    assert fake.query('TRIG:INT?') == '0.01'
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
        ('TRIG:INT 100000 US', 'TRIG:INT?', '0.1'),
        ('TRIG:INT 2 MS', 'TRIG:INT?', '0.002'),
        ('TRIG:DEL 15 US', 'TRIG:DEL?', '15000'),
        ('TRIG:DEL 0 NS', 'TRIG:DEL?', '0'),
        ('AVERage:DELay:RANDom 2000 NS', 'AVER:DEL:RAND?', '2e-06'),
        ('TRAN:PULS 100 V', 'TRAN:PULS?', '100'),
        ('TRANsmitter:PULSe:LEVel 35 V', 'TRAN:PULS?', '35'),
        ('GAIN 10', 'GAIN?', '10'),
        ('TRAN:ENAB 1', 'TRAN:ENAB?', '1'),
        ('TRAN:REVerse OFF', 'TRAN:REV?', '0'),
        ('TRAN:DAMP:ENAB ON', 'TRAN:DAMP?', '1'),
        ('GAIN:PRE:COMB 0', 'GAIN:PRE:COMB?', '0'),
        ('TRIG:MODE INTERNAL', 'TRIG:MODE?', 'INTernal'),
        ('MODE MASTer', 'MODE?', 'MASTer'),
        ('MODE SLAVe', 'MODE?', 'SLAVe'),
        ('TRAN:TYPE DUAL', 'TRAN:TYPE?', 'DUAL'),
        ('GAIN:TGC:MODE LINear', 'GAIN:TGC:MODE?', 'LINear'),
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


def test_device_reply_forms_measured_2026_10_05_fw_1_16() -> None:
    fake = FakeA1580Resource()
    assert fake.query('SYST:VERS?') == '1999.0'
    assert fake.query('SYSTem:VERSion?') == '1999.0'
    assert fake.query('MODE?') == 'MASTer'
    assert fake.query('TRIG:MODE?') == 'INTernal'
    assert fake.query('TRAN:TYPE?') == 'SINGle'  # as found on the device: DUAL
    assert fake.query('TRAN:IMP?') == 'HIGH'
    assert fake.query('GAIN:TGC:MODE?') == 'OFF'
    for header in ('TRAN:ENAB', 'TRAN:REV', 'TRAN:DAMP', 'GAIN:PRE:COMB', 'GAIN:PRE:SPLIT'):
        assert fake.query(f'{header}?') in ('0', '1'), header
    assert fake.query('AVER:DEL:CONS:AUTO?') == '1'
    fake.write('SYST:VERS 1')
    assert _drain(fake) == ['-113,"Undefined header;SYST:VERS 1"']


@pytest.mark.parametrize('sent', ['ON', 'on', '1'])
@pytest.mark.parametrize(
    'header',
    ['TRAN:ENAB', 'TRAN:REV', 'TRAN:DAMP', 'GAIN:PRE:COMB', 'GAIN:PRE:SPLIT', 'AVER:DEL:CONS:AUTO'],
)
def test_booleans_accept_on_off_and_answer_zero_one(header: str, sent: str) -> None:
    fake = FakeA1580Resource()
    fake.write(f'{header} {sent}')
    assert fake.query(f'{header}?') == '1'
    fake.write(f'{header} {"OFF" if sent != "1" else "0"}')
    assert fake.query(f'{header}?') == '0'
    assert _drain(fake) == []


@pytest.mark.parametrize(
    ('header', 'sent', 'reply'),
    [
        ('MODE', 'MASTER', 'MASTer'),
        ('MODE', 'master', 'MASTer'),
        ('MODE', 'MAST', 'MASTer'),
        ('MODE', 'MASTer', 'MASTer'),
        ('MODE', 'slave', 'SLAVe'),
        ('TRIG:MODE', 'INT', 'INTernal'),
        ('TRIG:MODE', 'INTERNAL', 'INTernal'),
        ('TRIG:MODE', 'internal', 'INTernal'),
        ('TRIG:MODE', 'ctp', 'CTP'),
        ('TRIG:MODE', 'ENCODER', 'ENCoder'),
        ('TRIG:MODE', 'ttl', 'TTL'),
        ('TRAN:TYPE', 'SING', 'SINGle'),
        ('TRAN:TYPE', 'dual', 'DUAL'),
        ('GAIN:TGC:MODE', 'LINEAR', 'LINear'),
        ('GAIN:TGC:MODE', 'arb', 'ARBitrary'),
        ('GAIN:TGC:MODE', 'off', 'OFF'),
        ('TRAN:IMP', 'high', 'HIGH'),
        ('TRAN:IMP', '1000', '1000'),
    ],
)
def test_enumerations_answer_in_the_fixed_notation_not_an_echo(
    header: str, sent: str, reply: str
) -> None:
    fake = FakeA1580Resource()
    fake.write(f'{header} {sent}')
    assert _drain(fake) == []
    assert fake.query(f'{header}?') == reply


def test_an_unknown_enumeration_word_is_an_error() -> None:
    fake = FakeA1580Resource()
    fake.write('MODE BANANA')
    assert len(_drain(fake)) == 1
    assert fake.query('MODE?') == 'MASTer'


def test_log_is_verbatim() -> None:
    fake = FakeA1580Resource()
    fake.write('FREQ 50 MHZ')
    fake.query('FREQ?')
    assert fake.log == ['FREQ 50 MHZ', 'FREQ?']


def test_unknown_header_queues_113_and_query_raises() -> None:
    fake = FakeA1580Resource()
    fake.write('FOO:BAR 1')
    assert fake.query(ERR) == '-113,"Undefined header;FOO:BAR 1"'
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


def test_rst_changes_no_setting_and_keeps_the_error_queue() -> None:
    # measured 2026-10-05, fw 1.16: six settings moved away, `*RST`, all read the same after
    fake = FakeA1580Resource(length=2048)
    moved = {'GAIN': '6', 'TRIG:INT': '0.01', 'AVER:COUN': '2', 'TRAN:FREQ': '50000'}
    fake.write('GAIN 6')
    fake.write('TRIG:INT 10 MS')
    fake.write('AVER:COUN 2')
    fake.write('TRAN:FREQ 50 KHZ')
    fake.write('FILT:HPAS:IND 2')
    fake.write('TRIG:DEL 0 NS')
    fake.write('NOPE 1')
    before = dict(fake._values)
    fake.write('*RST')
    assert fake._values == before
    for header, reply in moved.items():
        assert fake.query(f'{header}?') == reply
    assert fake.query('FILT:HPAS:IND?') == '2'
    assert fake.query('TRIG:DEL?') == '0'
    assert fake.query('TRAN:ENAB?') == '0'
    assert len(_drain(fake)) == 1  # *RST does not clear the queue (assumed), *CLS does
    fake.write('NOPE 1')
    fake.write('*CLS')
    assert _drain(fake) == []


def test_constant_delay_while_auto_is_221() -> None:
    fake = FakeA1580Resource()
    assert fake.query('AVER:DEL:CONS:AUTO?') == '1'
    fake.write('AVER:DEL:CONS 50 US')
    assert _drain(fake) == ['-221,"Settings conflict"']
    assert fake.query('AVER:DEL:CONS?') == '0.00992925'  # automatic: TRIG:INT 10 ms - 70.75 us
    fake.write('AVER:DEL:CONS:AUTO OFF')
    fake.write('AVER:DEL:CONS 50 US')
    assert _drain(fake) == []
    assert fake.query('AVER:DEL:CONS?') == '5e-05'


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
    for _ in range(2):  # the two packets in flight (measured 2026-10-05, fw 1.16)
        assert len(sock.recv(65536)) == packet_size(1024)
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
    assert parse_header(p0).ascan_count == 1
    fake.write('GAIN 20')
    fake.write('AVER:COUN 3')
    (p1,) = _read(fake, 1)
    raw1 = np.frombuffer(p1, dtype='<i2', offset=HEADER_SIZE)
    assert int(np.abs(raw1).max()) > 5 * int(np.abs(raw0).max())  # 20 dB is a factor 10
    assert parse_header(p1).ascan_count == 1  # measured: AVER:COUN does not show in the header


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
    assert numbers == [(2 + i) % 256 for i in range(300)]  # first one is 2: measured once
    # ctp[0] is the same counter, not wrapped (past 255 is not measured); ctp[1], ctp[2] are 0
    assert [parse_header(p).ctp for p in packets] == [(2 + i, 0, 0) for i in range(300)]


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
    first = parse_header(packets[0]).packet_number  # the counter belongs to the resource
    assert [parse_header(p).packet_number for p in packets] == [first, first + 1, first + 2]


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


# ── measured 2026-10-05, fw 1.16 (phase A and B) ─────────────────────────────


def test_error_queue_holds_16_then_queue_overflow_and_drops_the_rest() -> None:
    fake = FakeA1580Resource()
    counts = []
    for i in range(40):
        fake.write(f'ZZZ:X{i:02d}')
        counts.append(fake.query('SYST:ERR:COUN?'))
    assert counts == [str(min(i, 17)) for i in range(1, 41)]
    errors = _drain(fake)
    assert len(errors) == 17
    assert errors[0] == '-113,"Undefined header;ZZZ:X00"'
    assert errors[15] == '-113,"Undefined header;ZZZ:X15"'
    assert errors[16] == '-350,"Queue overflow"'
    assert fake.query('SYST:ERR:COUN?') == '0'


def test_cls_clears_a_full_queue() -> None:
    fake = FakeA1580Resource()
    for _ in range(20):
        fake.write('ZZZ:X')
    fake.write('*CLS')
    assert fake.query('SYST:ERR:COUN?') == '0'
    fake.write('ZZZ:X')
    assert fake.query('SYST:ERR:COUN?') == '1'


@pytest.mark.parametrize(
    'cmd', ['DATA:LENG 114688', 'FREQ banana', 'MODE BANANA', 'TRAN:ENAB MAYBE']
)
def test_an_illegal_parameter_is_224_and_keeps_the_old_value(cmd: str) -> None:
    fake = FakeA1580Resource()
    header = cmd.split()[0]
    before = fake.query(f'{header}?')
    fake.write(cmd)
    assert _drain(fake) == ['-224,"Illegal parameter value"']
    assert fake.query(f'{header}?') == before


def test_data_leng_1024_is_accepted_after_power_on_but_its_own_value_is_refused() -> None:
    fake = FakeA1580Resource(power_on=True)
    assert fake.query('DATA:LENG?') == '114688'
    fake.write('DATA:LENG 1024')
    assert _drain(fake) == []
    fake.write('DATA:LENG 114688')
    assert _drain(fake) == ['-224,"Illegal parameter value"']
    assert fake.query('DATA:LENG?') == '1024'


def test_power_on_state_has_the_pulser_on_and_rst_leaves_it_as_it_is() -> None:
    fake = FakeA1580Resource(power_on=True)
    for header in ('TRAN:ENAB', 'TRAN:TYPE', 'TRIG:INT', 'AVER:DEL:CONS', 'GAIN:TGC:LIN'):
        assert fake.query(f'{header}?') == POWER_ON[header]
    assert (fake.query('TRAN:ENAB?'), fake.query('TRAN:TYPE?')) == ('1', 'DUAL')
    assert (fake.query('TRIG:INT?'), fake.query('AVER:DEL:CONS?')) == ('1', '0.99992925')
    fake.write('*RST')  # measured 2026-10-05, fw 1.16: no setting changes
    assert (fake.query('TRAN:ENAB?'), fake.query('DATA:LENG?')) == ('1', '114688')
    assert FakeA1580Resource().query('TRAN:ENAB?') == '0'  # opt-in: the default fake is as before


@pytest.mark.parametrize(
    ('interval', 'count', 'reply'),
    [  # measured 2026-10-05, fw 1.16, FREQ 100 MHz: TRIG:INT / 2^AVER:COUN - 70.75 us
        ('1 S', '0', '0.99992925'),
        ('10 MS', '0', '0.00992925'),
        ('10 MS', '2', '0.00242925'),
    ],
)
def test_automatic_averaging_delay_follows_the_interval_and_the_count(
    interval: str, count: str, reply: str
) -> None:
    fake = FakeA1580Resource()
    fake.write(f'TRIG:INT {interval}')
    fake.write(f'AVER:COUN {count}')
    assert fake.query('AVER:DEL:CONS?') == reply


@pytest.mark.parametrize(
    ('cmd', 'query', 'reply'),
    [
        ('TRIG:INT 1000000 US', 'TRIG:INT?', '1'),
        ('TRIG:INT 10 MS', 'TRIG:INT?', '0.01'),  # measured 2026-10-05, fw 1.16
        ('TRIG:INT 10 US', 'TRIG:INT?', '1e-05'),
        ('AVER:DEL:RAND 2000 NS', 'AVER:DEL:RAND?', '2e-06'),
        ('TRIG:INT 2 S', 'TRIG:INT?', '2'),
    ],
)
def test_seconds_replies_are_the_shortest_decimal(cmd: str, query: str, reply: str) -> None:
    fake = FakeA1580Resource()
    fake.write(cmd)
    assert fake.query(query) == reply


def test_a_line_over_256_bytes_is_an_input_buffer_overrun_and_discarded() -> None:
    fake = FakeA1580Resource()
    fake.write('GAIN:TGC:ARB ' + ','.join(['1'] * 130))
    assert fake.query('GAIN:TGC:ARB?') == DEFAULTS['GAIN:TGC:ARB']
    assert _drain(fake) == ['-363,"Input buffer overrun"']
    fake.write('GAIN 3')  # the link stays usable
    assert fake.query('GAIN?') == '3'


def _noise_std(fake: FakeA1580Resource) -> float:
    fake.write('STAR AUTO')
    stds = [np.frombuffer(p, dtype='<i2', offset=HEADER_SIZE).std() for p in _read(fake, 10)]
    return float(np.mean(stds))


def test_averaging_is_a_mean_over_2_to_the_n_and_the_header_does_not_show_it() -> None:
    fake = FakeA1580Resource(signal='noise', length=4096)
    base = _noise_std(fake)
    (p,) = _read(fake, 1)
    raw = np.frombuffer(p, dtype='<i2', offset=HEADER_SIZE)
    assert 8 < raw.mean() < 14  # offset about +11 counts
    assert 3.0 < base < 4.2  # std about 3.6 counts
    fake.write('AVER:COUN 4')
    assert 0.2 < _noise_std(fake) / base < 0.32  # 1/sqrt(16) = 0.25 (measured 0.26)
    assert {parse_header(p).ascan_count for p in _read(fake, 3)} == {1}


def test_header_length_fields_and_telemetry_as_measured() -> None:
    fake = FakeA1580Resource(length=1024)
    fake.write('STAR AUTO')
    (packet,) = _read(fake, 1)
    header = parse_header(packet)
    assert len(packet) == 2076
    assert (header.length_lo, header.length_hi) == (1040, 0)
    assert (header.telemetry_a, header.telemetry_b, header.telemetry_c) == (120, 86, 52)
    assert (header.buffer_fill, header.is_full, header.ascan_count) == (0, 0, 1)


@pytest.mark.parametrize(
    ('length', 'size', 'length_lo', 'length_hi'),
    [
        (1024, 2076, 1040, 0),
        (2048, 4124, 2064, 0),
        (8192, 16412, 8208, 0),
        (36864, 73756, 36880, 0),
        (114688, 229404, 49168, 1),  # the 24-bit value 114704 = 114688 + 16
    ],
)
def test_header_length_field_is_a_24_bit_sample_count_plus_16(
    length: int, size: int, length_lo: int, length_hi: int
) -> None:
    fake = FakeA1580Resource(length=length)
    fake.write('STAR AUTO')
    (packet,) = _read(fake, 1)
    header = parse_header(packet)
    assert len(packet) == size
    assert (header.length_lo, header.length_hi) == (length_lo, length_hi)
    assert (header.reserved_b, header.reserved_c) == (0, 0)


def test_a_real_packet_header_parses_and_frames_whatever_length_lo_says() -> None:
    # the exact header values of a packet measured on the device (2076 bytes)
    packet = build_packet(
        [11] * 1024,
        packet_number=2,
        length_lo=1040,
        length_hi=0,
        telemetry_a=120,
        telemetry_b=86,
        telemetry_c=52,
        ascan_count=1,
    )
    assert len(packet) == 2076
    assert parse_header(packet).length_lo == 1040
    buffer = bytearray(packet + packet[:100])
    assert split_packets(buffer, 1024) == [packet]  # framing uses DATA:LENG, not length_lo


def test_stop_delivers_the_two_packets_in_flight_then_idles_without_closing() -> None:
    fake = FakeA1580Resource(length=16)
    sock = fake.data_socket_factory('h', 1)
    fake.write('STAR AUTO')
    sock.recv(65536)
    fake.write('STOP')
    assert len(sock.recv(65536)) == packet_size(16)
    assert len(sock.recv(65536)) == packet_size(16)
    for _ in range(3):
        with pytest.raises(socket.timeout):
            sock.recv(65536)
    assert not sock.closed
    fake.write('STAR AUTO')  # and the stream can be started again
    assert len(sock.recv(65536)) == packet_size(16)


def test_a_socket_connected_after_star_auto_receives_data_and_numbers_do_not_restart() -> None:
    fake = FakeA1580Resource(length=16)
    fake.write('STAR AUTO')
    first = FrameReader(fake.data_socket_factory('h', 1), 16).read(3, 1.0)
    fake.write('STOP')
    fake.write('STAR AUTO')
    second = FrameReader(fake.data_socket_factory('h', 1), 16).read(3, 1.0)
    numbers = [parse_header(p).packet_number for p in first + second]
    assert numbers[0] == 2  # measured once after power-on: nothing may rely on the value
    assert numbers == list(range(numbers[0], numbers[0] + 6))


# ── unknown nodes, visa errors, flush ────────────────────────────────────────


@pytest.mark.parametrize('cmd', ['GAINX 10', 'FREQUE 50 MHZ', 'TRAN:PULSX 20 V', 'NOPE 1'])
def test_a_write_with_an_unknown_node_queues_113_and_stores_nothing(cmd: str) -> None:
    fake = FakeA1580Resource()
    before = dict(fake._values)
    fake.write(cmd)
    errors = _drain(fake)
    assert [e.split(',')[0] for e in errors] == ['-113']
    assert fake._values == before


@pytest.mark.parametrize('cmd', ['GAINX?', 'FREQUE?', 'NOPE:HDR?'])
def test_a_query_with_an_unknown_node_queues_113_and_raises(cmd: str) -> None:
    fake = FakeA1580Resource()
    with pytest.raises(TimeoutError):
        fake.query(cmd)
    assert [e.split(',')[0] for e in _drain(fake)] == ['-113']


def test_visa_errors_raise_visaioerror_for_unanswered_queries() -> None:
    pyvisa = pytest.importorskip('pyvisa')
    fake = FakeA1580Resource(visa_errors=True)
    with pytest.raises(pyvisa.errors.VisaIOError) as exc:
        fake.query('FILT:HPAS?')  # a known node the fake has no value for
    assert exc.value.error_code == pyvisa.constants.StatusCode.error_timeout
    assert not isinstance(exc.value, OSError)
    with pytest.raises(pyvisa.errors.VisaIOError):
        fake.query('GAINX?')
    assert [e.split(',')[0] for e in _drain(fake)] == ['-113', '-113']
    assert fake.query('GAIN?') == '0'  # answered queries are unaffected


def test_without_visa_errors_the_timeout_is_a_timeout_error() -> None:
    fake = FakeA1580Resource()
    with pytest.raises(TimeoutError):
        fake.query('FILT:HPAS?')


def test_the_data_socket_keeps_raising_timeout_error_with_visa_errors() -> None:
    fake = FakeA1580Resource(visa_errors=True, signal='none')
    sock = fake.data_socket_factory('x', 1)
    fake.write('STAR AUTO')
    with pytest.raises(TimeoutError):
        sock.recv(10)


def test_flush_and_clear_record_themselves() -> None:
    fake = FakeA1580Resource()
    fake.flush()
    fake.flush(object())
    fake.clear()
    assert fake.log == ['flush', 'flush', 'clear']


@pytest.mark.parametrize(
    ('value', 'reply'), [('MAX', '36864'), ('MIN', '1024'), ('DEF', '1024'), ('36864', '36864')]
)
def test_data_leng_keywords_and_the_upper_limit_are_accepted(value: str, reply: str) -> None:
    fake = FakeA1580Resource(length=2048)
    fake.write(f'DATA:LENG {value}')
    assert _drain(fake) == []
    assert fake.query('DATA:LENG?') == reply


@pytest.mark.parametrize('value', ['36865', '65536', '114688', '1023'])
def test_data_leng_outside_1024_to_36864_is_224_and_keeps_the_old_value(value: str) -> None:
    fake = FakeA1580Resource(length=2048)
    fake.write(f'DATA:LENG {value}')
    assert _drain(fake) == ['-224,"Illegal parameter value"']
    assert fake.query('DATA:LENG?') == '2048'  # no clamping


def test_data_leng_1025_is_accepted_so_there_is_no_block_size_rule() -> None:
    fake = FakeA1580Resource()
    fake.write('DATA:LENG 1025')
    assert _drain(fake) == []
    assert fake.query('DATA:LENG?') == '1025'


def test_automatic_constant_delay_is_the_trigger_interval_minus_70_75_us() -> None:
    fake = FakeA1580Resource(power_on=True)  # TRIG:INT 1 s, AVER:DEL:CONS:AUTO on
    assert fake.query('AVER:DEL:CONS?') == '0.99992925'
    fake.write('TRIG:INT 10 MS')
    assert fake.query('AVER:DEL:CONS?') == '0.00992925'
    fake.write('AVER:DEL:CONS:AUTO OFF')
    fake.write('AVER:DEL:CONS 50 US')
    assert fake.query('AVER:DEL:CONS?') == '5e-05'  # no longer computed


def test_packets_at_the_power_on_length_are_zero_from_sample_81906() -> None:
    fake = FakeA1580Resource(power_on=True, signal='noise')
    fake.write('STAR AUTO')
    (packet,) = _read(fake, 1)
    samples = np.frombuffer(packet[HEADER_SIZE:], dtype='<i2')
    assert len(packet) == 229404
    assert len(samples) == 114688
    assert not samples[81906:].any()
    assert samples[81906 - 1000 : 81906].any()
    fake.write('DATA:LENG 36864')
    (packet,) = _read(fake, 1)
    assert np.frombuffer(packet[HEADER_SIZE:], dtype='<i2')[-1000:].any()  # legal length: all real


# ── signal='transmission' (measured 2026-10-05, fw 1.16, 50 kHz pair face to face) ──


def _transmission_record(fake: FakeA1580Resource) -> np.ndarray:
    fake.write('STAR AUTO')
    sock = fake.data_socket_factory('fake', 2758)
    length = int(fake.query('DATA:LENG?'))
    packet = FrameReader(sock, length).read(1, 2.0)[0]
    fake.write('STOP')
    return np.frombuffer(packet, dtype='<i2', offset=HEADER_SIZE)


def _transmission_fake(**settings: str) -> FakeA1580Resource:
    fake = FakeA1580Resource(signal='transmission')
    for header, value in {
        'DATA:LENG': '8192',
        'FREQ': '1 MHZ',
        'TRAN:FREQ': '50 KHZ',
        'TRIG:DEL': '0 NS',
        **settings,
    }.items():
        fake.write(f'{header} {value}')
    return fake


def _dev(record: np.ndarray) -> np.ndarray:
    return np.abs(record.astype(float) - 4.5)


def test_transmission_pulser_off_is_noise_only_with_the_measured_std() -> None:
    fake = _transmission_fake()
    fake.write('TRAN:ENAB OFF')
    record = _transmission_record(fake).astype(float)
    assert record.std() == pytest.approx(3.6, rel=0.05)  # measured 3.63 at 0 dB
    assert record.mean() == pytest.approx(4.5, abs=0.3)
    fake.write('GAIN 40')
    assert _transmission_record(fake).astype(float).std() == pytest.approx(25.7, rel=0.05)


def test_transmission_pulser_on_burst_starts_3_us_after_sample_0_with_the_measured_amplitude() -> (
    None
):
    fake = _transmission_fake(GAIN='40')
    fake.write('TRAN:ENAB ON')
    dev = _dev(_transmission_record(fake))
    assert int(np.flatnonzero(dev > 200)[0]) in (4, 5, 6)  # onset at 3 us, the sine starts at 0
    assert 1200 < dev.max() < 1450  # measured 1190 to 1393
    assert dev[300:].max() < 150  # measured: only noise from 200 us (std 25.7 at 40 dB)


def test_transmission_amplitude_is_about_18_counts_at_0_db() -> None:
    fake = _transmission_fake()
    fake.write('TRAN:ENAB ON')
    mean = np.mean([_transmission_record(fake) for _ in range(10)], axis=0)
    peak = np.abs(mean - np.median(mean)).max()
    assert 14 < peak < 20  # measured +-15 to 18 counts in the mean of 10 packets


def test_transmission_rings_at_the_transducer_frequency_for_about_5_periods() -> None:
    fake = _transmission_fake(GAIN='40')
    fake.write('TRAN:ENAB ON')
    signal = _transmission_record(fake).astype(float) - 4.5
    spectrum = np.abs(np.fft.rfft(signal[:200]))
    assert np.fft.rfftfreq(200, 1e-6)[spectrum.argmax()] == pytest.approx(50e3, rel=0.1)
    assert np.abs(signal[3:103]).max() > 10 * np.abs(signal[200:]).std()


@pytest.mark.parametrize('delay', ['1000 US', '2000 US'])
def test_transmission_burst_does_not_move_with_trig_del(delay: str) -> None:
    fake = _transmission_fake(GAIN='40')
    fake.write('TRAN:ENAB ON')
    base = _dev(_transmission_record(fake))
    fake.write(f'TRIG:DEL {delay}')
    shifted = _dev(_transmission_record(fake))
    assert int(np.flatnonzero(shifted > 200)[0]) == int(np.flatnonzero(base > 200)[0])
    assert abs(int(shifted[:100].argmax()) - int(base[:100].argmax())) <= 2  # noise only


def test_transmission_amplitude_follows_pulser_voltage_and_gain() -> None:
    fake = _transmission_fake(GAIN='40')
    fake.write('TRAN:ENAB ON')
    full = _dev(_transmission_record(fake)).max()
    fake.write('TRAN:PULS 10 V')  # the voltage scaling is a guess, only 20 V was measured
    half = _dev(_transmission_record(fake)).max()
    assert 0.4 < half / full < 0.6
    fake.write('GAIN 20')
    assert _dev(_transmission_record(fake)).max() < 0.3 * full


def test_transmission_averaging_lowers_the_noise_not_the_burst() -> None:
    fake = _transmission_fake(GAIN='40')
    fake.write('TRAN:ENAB ON')
    plain = _transmission_record(fake).astype(float)
    fake.write('AVER:COUN 4')
    averaged = _transmission_record(fake).astype(float)
    assert averaged[300:].std() == pytest.approx(plain[300:].std() / 4.0, rel=0.1)
    assert abs(_dev(averaged).max() / _dev(plain).max() - 1.0) < 0.05  # measured ratio 0.999


def test_default_signal_ignores_the_pulser() -> None:
    off, on = FakeA1580Resource(), FakeA1580Resource()
    on.write('TRAN:ENAB ON')
    assert _transmission_record(off).tolist() == _transmission_record(on).tolist()
