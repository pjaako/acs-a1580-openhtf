"""Tests for tools/hw_probe.py, the owner's instrument for the first hardware session.

The tool runs in-process against `FakeA1580Resource` (no hardware, no sockets).
"""

from __future__ import annotations

import json
import socket
import sys
from pathlib import Path
from typing import Any

import pytest

from a1580_openhtf.fake_resource import DEFAULTS, FakeA1580Resource
from a1580_openhtf.plug import STATE_HEADERS, normalize_header

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))

import hw_probe  # noqa: E402


@pytest.fixture(autouse=True)
def _quick(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hw_probe, 'OBSERVE_S', 0.05)
    monkeypatch.delenv('A1580_HOST', raising=False)


def _no_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError('a socket was opened')

    monkeypatch.setattr(socket, 'create_connection', refuse)


def _json_files(out: Path) -> tuple[Path, Path]:
    snapshots = [p for p in out.glob('*.json') if not p.name.startswith('probe-')]
    probes = list(out.glob('probe-*.json'))
    assert len(snapshots) == 1, snapshots
    assert len(probes) == 1, probes
    return snapshots[0], probes[0]


# ── CLI ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ('reply', 'shape'),
    [
        ('', 'empty'),
        ('0', 'integer'),
        ('2e-06', 'exponent notation'),
        ('1999.0', 'decimal'),
        ('0,0', 'list'),
        ('HIGH', 'upper-case word'),
        ('MASTer', 'mixed-case word'),
    ],
)
def test_shape_labels_the_reply_format(reply: str, shape: str) -> None:
    assert hw_probe._shape(reply) == shape


def test_dry_run_lists_every_command_without_connecting(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _no_sockets(monkeypatch)
    assert hw_probe.main(['--dry-run', '--phase', 'AB']) == 0
    out = capsys.readouterr().out
    lines = {line.strip() for line in out.splitlines()}
    for cmd in (
        '*IDN?',
        'SYST:VERS?',
        'SYST:ERR:COUN?',
        'DATA:PORT?',
        'SYSTem:ERRor?',
        'ZZZ:NOPE 1',
        'ZZZ:NOPE25',
        'TRAN:ENAB OFF',
        'TRAN:ENAB?',
        'DATA:LENG 1024',
        'FREQ 100 MHZ',
        'TRIG:MODE INT',
        'TRIG:INT 10 MS',
        'AVER:COUN 4',
        'STAR AUTO',
        'STOP',
        'GAIN?',
        *(f'{header}?' for header in STATE_HEADERS),
    ):
        assert cmd in lines, cmd
    for number in range(1, 10):
        assert f'STEP {number}:' in out
    assert 'FINAL DIFF' in out
    # the only TRAN:PULS the tool can send is a query
    assert not [line for line in lines if line.startswith('TRAN:PULS ') and '?' not in line]
    assert '*RST' not in lines


def test_dry_run_phase_a_has_no_phase_b_commands(capsys: pytest.CaptureFixture[str]) -> None:
    assert hw_probe.main(['--dry-run']) == 0
    out = capsys.readouterr().out
    assert 'STEP 5:' in out
    assert 'STEP 6:' not in out
    assert 'STAR AUTO' not in out


def test_no_host_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert hw_probe.main(['--phase', 'A']) == 2
    assert 'A1580_HOST' in capsys.readouterr().err


def test_host_from_environment_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('A1580_HOST', 'device.invalid')
    args = hw_probe.build_parser().parse_args([])
    assert args.host == 'device.invalid'
    assert args.phase == 'A'
    assert args.max_pulse_v == 20
    assert args.packets == 10
    assert args.out == 'setups/'


def _header_key(cmd: str) -> str:
    """The normal form of a command's header; one that does not normalise is kept verbatim."""
    head = cmd.strip().split(' ', 1)[0]
    try:
        return normalize_header(head)
    except ValueError:
        return head


def test_commands_sent_by_a_full_run_are_all_in_the_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--dry-run must not lie: every header the run sends appears in the plan."""
    _no_sockets(monkeypatch)
    fake = FakeA1580Resource()
    assert hw_probe.main(['--fake', '--phase', 'AB', '--out', str(tmp_path)], fake=fake) == 0
    planned = {
        _header_key(cmd)
        for _heading, commands in hw_probe.plan('AB')
        for cmd in commands
        if not cmd.startswith('raw socket')
    }
    sent = {_header_key(cmd) for cmd in fake.log}
    assert sent <= planned, sent - planned


# ── fake runs ────────────────────────────────────────────────────────────────


def test_fake_phase_ab_runs_all_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _no_sockets(monkeypatch)
    fake = FakeA1580Resource()
    code = hw_probe.main(['--fake', '--phase', 'AB', '--out', str(tmp_path)], fake=fake)
    out = capsys.readouterr().out
    assert code == 0, out
    for number in range(1, 10):
        assert f'STEP {number}:' in out
    assert 'FAILED' not in out
    assert 'FINAL DIFF' in out
    assert 'no differences' in out
    assert 'SKIPPED (fake)' in out

    snapshot_path, probe_path = _json_files(tmp_path)
    snapshot = json.loads(snapshot_path.read_text(encoding='utf-8'))
    assert snapshot['idn'] == 'ACS-Solutions GmbH,A1580-HF,100500,1.16 (861f022a)'
    assert set(snapshot['state']) == set(STATE_HEADERS)
    assert set(snapshot['timing_s']) == set(STATE_HEADERS)
    assert snapshot_path.name.startswith('100500-')
    assert probe_path.name.startswith('probe-100500-')

    report = json.loads(probe_path.read_text(encoding='utf-8'))
    assert report['exit_code'] == 0
    assert report['final_diff']['changed'] == {}
    assert [f'STEP {n}' in report['steps'] for n in range(1, 10)] == [True] * 9
    step6 = report['steps']['STEP 6']['data']
    assert step6['n'] == 10
    assert len(step6['packets']) == 10
    assert step6['packet_number_steps'] == [1]
    assert {'min', 'max', 'std', 'ascan_count', 'buffer_fill', 'is_full'} <= set(
        step6['packets'][0]
    )
    assert report['steps']['STEP 7']['data']['ascan_counts'] == [1]  # measured: stays 1
    step5 = report['steps']['STEP 5']['data']
    assert step5['counts'] == [str(min(i, 17)) for i in range(1, 26)]  # 16, then -350 as 17th
    assert step5['depth'] == 17
    assert step5['last_is_overflow'] is True
    assert step6['readbacks']['TRIG:INT'] == '0.01'  # the exact read-back reply of each setting
    assert report['steps']['STEP 7']['data']['readbacks'] == {'AVER:COUN': '4'}
    assert report['steps']['TEARDOWN']['data']['restore_failures'] == []
    assert report['steps']['STEP 9']['data']['verdict'].startswith('accepted')
    assert '8a' in report['steps']['STEP 8']['data']
    assert 'idle' in report['steps']['STEP 8']['data']['8a']['verdict']
    assert 'data arrived' in report['steps']['STEP 8']['data']['8b']['verdict']


def test_replies_are_printed_verbatim_between_backticks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeA1580Resource()
    assert hw_probe.main(['--fake', '--out', str(tmp_path)], fake=fake) == 0
    out = capsys.readouterr().out
    assert '`ACS-Solutions GmbH,A1580-HF,100500,1.16 (861f022a)`' in out
    assert 'TRIG:INT?' in out
    assert '`0.01`' in out
    assert '`2758`' in out


def test_the_tool_never_sends_the_forbidden_commands(tmp_path: Path) -> None:
    fake = FakeA1580Resource()
    assert hw_probe.main(['--fake', '--phase', 'AB', '--out', str(tmp_path)], fake=fake) == 0
    for cmd in fake.log:
        assert not cmd.upper().startswith('*RST'), cmd
        assert '/config' not in cmd.lower(), cmd
        assert not (_header_key(cmd) == 'TRAN:PULS' and ' ' in cmd), cmd


def test_phase_a_is_read_only_except_bad_headers(tmp_path: Path) -> None:
    fake = FakeA1580Resource()
    assert hw_probe.main(['--fake', '--phase', 'A', '--out', str(tmp_path)], fake=fake) == 0
    writes = [c for c in fake.log if not c.rstrip().endswith('?')]
    first_restore = max(i for i, c in enumerate(fake.log) if c == 'STOP')
    # tearDown opens with an unchecked `TRAN:ENAB OFF` before its STOP: that is the restore
    teardown_start = max(i for i, c in enumerate(fake.log[:first_restore]) if c == 'TRAN:ENAB OFF')
    before_teardown = [c for c in fake.log[:teardown_start] if c in writes]
    assert before_teardown, 'the bad headers of step 5 are expected'
    assert all(c.startswith('ZZZ:NOPE') for c in before_teardown), before_teardown


def _teardown_block(out: str) -> list[str]:
    """Commands of the TEARDOWN block of a dry-run, in order."""
    lines = out.split('\n\n')
    block = next(b for b in lines if b.startswith('TEARDOWN')).splitlines()[1:]
    return [line.strip().split('   (')[0] for line in block]


def _fake_teardown_commands(phase: str, out_dir: Path) -> list[str]:
    fake = FakeA1580Resource()
    assert hw_probe.main(['--fake', '--phase', phase, '--out', str(out_dir)], fake=fake) == 0
    start = max(i for i, c in enumerate(fake.log) if c == 'STOP') - 1
    return fake.log[start:]


def test_phase_a_sends_no_restore_writes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeA1580Resource()
    assert hw_probe.main(['--fake', '--phase', 'A', '--out', str(tmp_path)], fake=fake) == 0
    out = capsys.readouterr().out
    after_stop = fake.log[max(i for i, c in enumerate(fake.log) if c == 'STOP') :]
    writes = [c for c in after_stop if not c.rstrip().endswith('?')]
    assert writes == ['STOP', 'TRAN:ENAB OFF'], writes
    assert 'no restore' in out
    assert 'no differences' in out


@pytest.mark.parametrize('phase', ['B', 'AB'])
def test_phases_b_and_ab_still_restore(tmp_path: Path, phase: str) -> None:
    after_stop = _fake_teardown_commands(phase, tmp_path)
    assert any(c.startswith('FREQ ') for c in after_stop)
    assert any(c.startswith('DATA:LENG ') for c in after_stop)
    assert any(c.startswith('GAIN ') for c in after_stop)


def test_dry_run_snapshot_values_only_when_restoring(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert hw_probe.main(['--dry-run', '--phase', 'A']) == 0
    assert '<snapshot value>' not in capsys.readouterr().out
    assert hw_probe.main(['--dry-run', '--phase', 'AB']) == 0
    out = capsys.readouterr().out
    assert '<snapshot value>' in out
    assert 'TRAN:ENAB <snapshot value>' not in out


@pytest.mark.parametrize('phase', ['A', 'B', 'AB'])
def test_dry_run_teardown_matches_what_the_fake_run_sends(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], phase: str
) -> None:
    assert hw_probe.main(['--dry-run', '--phase', phase]) == 0
    planned = _teardown_block(capsys.readouterr().out)
    sent = _fake_teardown_commands(phase, tmp_path)
    if phase == 'A':
        assert planned == sent
        return

    # the plan has `<header> <snapshot value>` where the run has the value; the fake skips
    # AVER:DEL:CONS (its AUTO is ON), and with it the error-queue read that follows the write
    skipped = planned.index('AVER:DEL:CONS <snapshot value>')
    planned = planned[:skipped] + planned[skipped + 2 :]
    assert [c.split(' ')[0] for c in planned] == [c.split(' ')[0] for c in sent]


def test_pulser_is_switched_off_and_checked_before_any_other_phase_b_write(
    tmp_path: Path,
) -> None:
    fake = FakeA1580Resource()
    fake.write('TRAN:ENAB ON')
    fake.log.clear()
    assert hw_probe.main(['--fake', '--phase', 'B', '--out', str(tmp_path)], fake=fake) == 0
    writes = [c for c in fake.log if not c.rstrip().endswith('?')]
    assert writes[0] == 'TRAN:ENAB OFF'
    off = fake.log.index('TRAN:ENAB OFF')
    assert fake.log[off + 1 :].index('TRAN:ENAB?') < fake.log[off + 1 :].index('DATA:LENG 1024')


def test_pulser_that_stays_on_aborts_the_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class StuckOn(FakeA1580Resource):
        def write(self, cmd: str) -> None:
            if cmd == 'TRAN:ENAB OFF' and not self.stop_count:
                self.log.append(cmd)  # swallowed: the pulser stays on
                return
            super().write(cmd)

    fake = StuckOn()
    fake._values['TRAN:ENAB'] = 'ON'
    code = hw_probe.main(['--fake', '--phase', 'B', '--out', str(tmp_path)], fake=fake)
    out = capsys.readouterr().out
    assert code == hw_probe.EXIT_PULSER
    assert 'PULSER-OFF CHECK ABORTED' in out
    assert 'STEP 6:' not in out
    assert 'STOP' in fake.log  # tearDown still ran
    before_teardown = fake.log[: fake.log.index('STOP')]
    assert 'DATA:LENG 1024' not in before_teardown
    assert 'STAR AUTO' not in fake.log
    assert 'FINAL DIFF' in out


def test_pulser_above_ceiling_exits_before_anything_is_written(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeA1580Resource()
    fake.write('TRAN:PULS 30 V')
    fake.log.clear()
    code = hw_probe.main(['--fake', '--phase', 'AB', '--out', str(tmp_path)], fake=fake)
    captured = capsys.readouterr()
    assert code == hw_probe.EXIT_CEILING
    assert 'above --max-pulse-v' in captured.err
    assert 'STEP 6:' not in captured.out
    writes = [c for c in fake.log if not c.rstrip().endswith('?')]
    # only tearDown's unchecked TRAN:ENAB OFF (its first action) and the STOP; no restore
    assert writes == ['TRAN:ENAB OFF', 'STOP']
    assert fake._values['TRAN:PULS'] == '30'
    assert _json_files(tmp_path)


def test_ceiling_can_be_raised(tmp_path: Path) -> None:
    fake = FakeA1580Resource()
    fake.write('TRAN:PULS 30 V')
    fake.log.clear()
    args = ['--fake', '--phase', 'A', '--max-pulse-v', '30', '--out', str(tmp_path)]
    assert hw_probe.main(args, fake=fake) == 0
    assert fake._values['TRAN:PULS'] == '30'
    assert not [c for c in fake.log if c.startswith('TRAN:PULS ')]  # never written back


def test_rejected_header_in_step_6_is_reported_and_tear_down_still_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(hw_probe, 'STEP6_SETTINGS', {**hw_probe.STEP6_SETTINGS, 'BOGUS:HDR': 1})
    fake = FakeA1580Resource()
    code = hw_probe.main(['--fake', '--phase', 'AB', '--out', str(tmp_path)], fake=fake)
    out = capsys.readouterr().out
    assert code == hw_probe.EXIT_FAILED_STEP
    assert 'STEP 6 FAILED' in out
    assert 'BOGUS:HDR' in out
    assert 'STEP 7:' in out
    assert 'STEP 9:' in out  # the run went on
    last_stop = max(i for i, c in enumerate(fake.log) if c == 'STOP')
    restore = fake.log[last_stop:]
    assert 'TRAN:ENAB OFF' in restore
    assert any(c.startswith('DATA:LENG ') for c in restore)
    assert any(c.startswith('AVER:COUN ') for c in restore)
    assert 'FINAL DIFF' in out
    assert 'no differences' in out  # the restore put the fake back as found
    _snapshot, probe_path = _json_files(tmp_path)
    report = json.loads(probe_path.read_text(encoding='utf-8'))
    assert report['steps']['STEP 6']['status'] == 'failed'
    assert report['steps']['STEP 7']['status'] == 'ok'


def test_device_that_rejects_a_setting_fails_the_step_not_the_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeA1580Resource(reject={'TRIG:INT': '-102,"Syntax error"'})
    code = hw_probe.main(['--fake', '--phase', 'B', '--out', str(tmp_path)], fake=fake)
    out = capsys.readouterr().out
    assert code == hw_probe.EXIT_FAILED_STEP
    assert 'STEP 6 FAILED' in out
    assert 'STEP 8:' in out
    assert fake.log.count('STOP') >= 1


def test_restore_failures_are_in_the_teardown_record_and_printed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeA1580Resource(reject={'TRAN:DUR': '-224,"Illegal parameter value"'})
    hw_probe.main(['--fake', '--phase', 'B', '--out', str(tmp_path)], fake=fake)
    out = capsys.readouterr().out
    _snapshot, probe_path = _json_files(tmp_path)
    report = json.loads(probe_path.read_text(encoding='utf-8'))
    failures = report['steps']['TEARDOWN']['data']['restore_failures']
    assert len(failures) == 1
    assert failures[0].startswith('TRAN:DUR')
    assert f'RESTORE FAILED: {failures[0]}' in out


def test_unanswered_header_is_recorded_and_not_restored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Mute(FakeA1580Resource):
        def query(self, cmd: str) -> str:
            if cmd.startswith('FILT:HPAS:IND'):
                self.log.append(cmd)
                raise TimeoutError('no reply')
            return super().query(cmd)

    fake = Mute()
    assert hw_probe.main(['--fake', '--phase', 'A', '--out', str(tmp_path)], fake=fake) == 0
    snapshot_path, _probe = _json_files(tmp_path)
    snapshot = json.loads(snapshot_path.read_text(encoding='utf-8'))
    assert snapshot['state']['FILT:HPAS:IND'].startswith('<ERROR TimeoutError')
    assert not [c for c in fake.log if c.startswith('FILT:HPAS:IND ')]


def test_guarded_resource_refuses_forbidden_commands() -> None:
    fake = FakeA1580Resource()
    guarded = hw_probe.GuardedResource(fake)
    for cmd in ('TRAN:PULS 50 V', 'TRANsmitter:PULSe:LEVel 5 V', '*RST'):
        with pytest.raises(hw_probe.ForbiddenCommand):
            guarded.write(cmd)
    with pytest.raises(hw_probe.ForbiddenCommand):
        guarded.query('/config/dev.eth?')
    assert guarded.query('TRAN:PULS?') == '20'
    assert fake.log == ['TRAN:PULS?']
    guarded.close()
    assert not fake.closed  # close is deferred to shutdown()
    guarded.shutdown()
    assert fake.closed


def test_packet_times_follow_the_completing_chunk() -> None:
    class Dummy:
        pass

    sock = hw_probe.RecordingSocket(Dummy())
    sock.chunks = [(1.0, 60), (2.0, 60), (3.0, 100)]
    assert hw_probe.packet_times(sock, 100, 1) == [2.0]
    assert hw_probe.packet_times(sock, 100, 5) == [2.0, 3.0]  # 220 bytes: two whole packets


def test_previous_port_is_compared_across_sessions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    earlier = {'steps': {'STEP 3': {'data': {'data_port': '9999'}}}}
    (tmp_path / 'probe-100500-20200101-000000.json').write_text(json.dumps(earlier))
    fake = FakeA1580Resource()
    assert hw_probe.main(['--fake', '--out', str(tmp_path)], fake=fake) == 0
    out = capsys.readouterr().out
    assert 'earlier probe saw `9999`, now `2758`: CHANGED' in out


# ── step 4 last, honest final diff, pulser left off ──────────────────────────


@pytest.mark.parametrize('phase', ['A', 'AB'])
def test_dry_run_step_4_comes_after_the_teardown_block(
    capsys: pytest.CaptureFixture[str], phase: str
) -> None:
    assert hw_probe.main(['--dry-run', '--phase', phase]) == 0
    out = capsys.readouterr().out
    assert out.index('TEARDOWN') < out.index('FINAL DIFF') < out.index('STEP 4:')
    assert out.index('STEP 5:') < out.index('TEARDOWN')
    heading = next(line for line in out.splitlines() if line.startswith('STEP 4:'))
    assert 'own connection' in heading
    assert 'after the main connection is closed' in heading


def test_fake_phase_a_step_4_is_the_last_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_sockets(monkeypatch)
    assert hw_probe.main(['--fake', '--phase', 'A', '--out', str(tmp_path)]) == 0
    _snapshot, probe_path = _json_files(tmp_path)
    steps = list(json.loads(probe_path.read_text(encoding='utf-8'))['steps'])
    assert steps[-1] == 'STEP 4'
    assert steps.index('STEP 5') < steps.index('TEARDOWN') < steps.index('FINAL DIFF')
    assert steps.index('FINAL DIFF') < steps.index('STEP 4')


def test_step_4_runs_after_the_main_connection_is_closed_and_a_failure_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    real_shutdown = hw_probe.GuardedResource.shutdown

    def shutdown(self: hw_probe.GuardedResource) -> None:
        events.append('shutdown')
        real_shutdown(self)

    def step_4(p: Any) -> None:
        events.append('step 4')
        raise OSError('boom')

    monkeypatch.setattr(hw_probe.GuardedResource, 'shutdown', shutdown)
    monkeypatch.setitem(hw_probe.STEPS, 4, ('bare newline', step_4))
    code = hw_probe.main(['--fake', '--phase', 'A', '--out', str(tmp_path)])
    assert code == hw_probe.EXIT_FAILED_STEP
    assert events == ['shutdown', 'step 4']
    _snapshot, probe_path = _json_files(tmp_path)  # the report is still written
    report = json.loads(probe_path.read_text(encoding='utf-8'))
    assert report['steps']['STEP 4']['status'] == 'failed'
    assert report['exit_code'] == hw_probe.EXIT_FAILED_STEP


def test_final_diff_with_a_dead_link_counts_unreadable_headers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class Dead(FakeA1580Resource):
        def query(self, cmd: str) -> str:
            if self.stop_count:  # tearDown sent STOP: the link is gone
                raise BrokenPipeError('gone')
            return super().query(cmd)

    code = hw_probe.main(['--fake', '--phase', 'A', '--out', str(tmp_path)], fake=Dead())
    out = capsys.readouterr().out
    assert code == hw_probe.EXIT_FAILED_STEP
    assert 'CHANGED' not in out
    assert out.count('UNREADABLE') == 1
    n = len(STATE_HEADERS)
    assert f'final diff NOT POSSIBLE: {n} of {n} headers could not be read' in out
    _snapshot, probe_path = _json_files(tmp_path)
    report = json.loads(probe_path.read_text(encoding='utf-8'))
    assert len(report['final_diff']['unreadable']) == n
    assert report['final_diff']['changed'] == {}
    assert 'STEP 4' in report['steps']


def test_pulser_on_as_found_is_left_off_and_is_an_expected_change(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeA1580Resource()
    fake._values['TRAN:ENAB'] = 'ON'
    assert hw_probe.main(['--fake', '--phase', 'A', '--out', str(tmp_path)], fake=fake) == 0
    out = capsys.readouterr().out
    assert 'switches it back on' not in out
    assert 'the tool will leave it OFF' in out
    changed = [line for line in out.splitlines() if line.startswith('CHANGED')]
    assert len(changed) == 1
    assert changed[0].startswith('CHANGED TRAN:ENAB')
    assert '(expected: the tool always leaves the pulser off)' in changed[0]
    _snapshot, probe_path = _json_files(tmp_path)
    report = json.loads(probe_path.read_text(encoding='utf-8'))
    assert report['final_diff']['changed']['TRAN:ENAB']['expected'] is True


# ═══ phase C and phase RST (SPEC-phaseC.md): pulser-on experiments ═══════════
#
# The safety tests come first: they prove that the pulser always ends OFF and that the
# guard refuses what only the one allowed function may send.

import numpy as np  # noqa: E402

PHASES = ['A', 'B', 'AB', 'C', 'RST']


def _policy(phase: str, pulse_v: float | None = None) -> Any:
    return hw_probe.GuardPolicy(phase=phase, pulse_v=pulse_v, max_pulse_v=20.0)


def _c_fake(**kwargs: Any) -> FakeA1580Resource:
    return FakeA1580Resource(signal='transmission', **kwargs)


def _c_args(out: Path, *extra: str) -> list[str]:
    return ['--fake', '--phase', 'C', '--pulse-v', '20', '--out', str(out), *extra]


def _enab_writes(log: list[str]) -> list[str]:
    return [c for c in log if c.startswith('TRAN:ENAB ')]


# ── section 1: command line ──────────────────────────────────────────────────


@pytest.mark.parametrize('dry', [[], ['--dry-run']])
def test_phase_c_without_pulse_v_is_a_usage_error(
    dry: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert hw_probe.main(['--fake', '--phase', 'C', *dry]) == hw_probe.EXIT_USAGE
    assert '--pulse-v' in capsys.readouterr().err


@pytest.mark.parametrize('volts', ['25', '4.9', '0', '-20', 'nan', 'inf'])
@pytest.mark.parametrize('dry', [[], ['--dry-run']])
def test_pulse_v_outside_5_to_max_is_a_usage_error_before_connecting(
    volts: str, dry: list[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _no_sockets(monkeypatch)
    fake = _c_fake()
    args = ['--fake', '--phase', 'C', '--pulse-v', volts, '--out', str(tmp_path), *dry]
    assert hw_probe.main(args, fake=fake) == hw_probe.EXIT_USAGE
    assert fake.log == []  # nothing was sent


def test_pulse_v_can_be_raised_with_the_ceiling_and_bounds_are_inclusive(tmp_path: Path) -> None:
    for volts, ceiling in (('5', '20'), ('20', '20'), ('30', '30')):
        fake = _c_fake()
        args = ['--fake', '--phase', 'C', '--pulse-v', volts, '--max-pulse-v', ceiling]
        code = hw_probe.main([*args, '--dry-run'], fake=fake)
        assert code == 0, (volts, ceiling)


@pytest.mark.parametrize('dry', [[], ['--dry-run']])
def test_phase_rst_without_allow_rst_is_a_usage_error(
    dry: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert hw_probe.main(['--fake', '--phase', 'RST', *dry]) == hw_probe.EXIT_USAGE
    assert '--allow-rst' in capsys.readouterr().err


def test_phase_names_are_case_insensitive_and_defaults_are_the_bench_values() -> None:
    args = hw_probe.build_parser().parse_args(['--phase', 'rst'])
    assert args.phase == 'RST'
    assert args.pulse_v is None
    assert args.allow_rst is False
    assert (args.tran_freq, args.sample_freq, args.length) == ('50 KHZ', '1 MHZ', 8192)
    assert (args.interval, args.gain) == ('100 MS', 0)


@pytest.mark.parametrize('flag', ['--tran-freq', '--sample-freq', '--interval'])
def test_setting_text_that_could_smuggle_a_second_command_is_a_usage_error(flag: str) -> None:
    for bad in ('50 KHZ;TRAN:ENAB ON', '50 KHZ\nTRAN:ENAB ON', '50 KHZ\r\n*RST'):
        argv = ['--phase', 'C', '--pulse-v', '20', '--dry-run', f'{flag}={bad}']
        assert hw_probe.main(argv) == hw_probe.EXIT_USAGE


# ── section 2: the guard ─────────────────────────────────────────────────────


@pytest.mark.parametrize('phase', PHASES)
@pytest.mark.parametrize(
    'cmd', ['TRAN:ENAB ON', 'TRAN:ENAB 1', 'tran:enab on', 'TRANsmitter:ENABle ON']
)
def test_guard_refuses_pulser_on_outside_the_gate_in_every_phase(phase: str, cmd: str) -> None:
    fake = _c_fake()
    guarded = hw_probe.GuardedResource(fake, policy=_policy(phase, 20.0))
    with pytest.raises(hw_probe.ForbiddenCommand):
        guarded.write(cmd)
    assert fake.log == []


@pytest.mark.parametrize('phase', ['A', 'B', 'AB', 'RST'])
def test_guard_refuses_pulser_on_even_inside_the_gate_outside_phase_c(phase: str) -> None:
    fake = _c_fake()
    guarded = hw_probe.GuardedResource(fake, policy=_policy(phase))
    with guarded.pulser_gate(), pytest.raises(hw_probe.ForbiddenCommand):
        guarded.write('TRAN:ENAB ON')
    assert fake.log == []


def test_guard_gate_opens_pulser_on_in_phase_c_only_while_it_is_open() -> None:
    fake = _c_fake()
    guarded = hw_probe.GuardedResource(fake, policy=_policy('C', 20.0))
    with guarded.pulser_gate():
        guarded.write('TRAN:ENAB ON')
        with pytest.raises(hw_probe.ForbiddenCommand):
            guarded.write('TRAN:PULS 25 V')  # the gate opens nothing else
        with pytest.raises(hw_probe.ForbiddenCommand):
            guarded.write('*RST')
    with pytest.raises(hw_probe.ForbiddenCommand):
        guarded.write('TRAN:ENAB ON')  # closed again
    with pytest.raises(hw_probe.ForbiddenCommand):  # closed again after an exception too
        with guarded.pulser_gate():
            raise hw_probe.ForbiddenCommand('inside')
    with pytest.raises(hw_probe.ForbiddenCommand):
        guarded.write('TRAN:ENAB ON')
    assert fake.log == ['TRAN:ENAB ON']


@pytest.mark.parametrize('phase', PHASES)
def test_guard_always_lets_the_pulser_off_through(phase: str) -> None:
    fake = _c_fake()
    guarded = hw_probe.GuardedResource(fake, policy=_policy(phase, 20.0))
    for cmd in ('TRAN:ENAB OFF', 'TRAN:ENAB 0', 'TRAN:ENAB?'):
        guarded.write(cmd) if not cmd.endswith('?') else guarded.query(cmd)
    assert fake.log == ['TRAN:ENAB OFF', 'TRAN:ENAB 0', 'TRAN:ENAB?']


@pytest.mark.parametrize(
    'cmd',
    [
        'TRAN:PULS 25 V',  # above the ceiling
        'TRAN:PULS 15 V',  # below, but not --pulse-v
        'TRAN:PULS 20.5 V',
        'TRAN:PULS MAX',
        'TRAN:PULS',
        'TRAN:PULS 20 MV',
        'TRAN:PULS 20 V;TRAN:ENAB ON',
        'TRAN:PULS 20 V\nTRAN:PULS 50 V',
        'TRANsmitter:PULSe:LEVel 25 V',
    ],
)
def test_guard_refuses_tran_puls_in_phase_c_unless_it_is_pulse_v(cmd: str) -> None:
    fake = _c_fake()
    guarded = hw_probe.GuardedResource(fake, policy=_policy('C', 20.0))
    with pytest.raises(hw_probe.ForbiddenCommand):
        guarded.write(cmd)
    assert fake.log == []


def test_guard_lets_exactly_pulse_v_through_in_phase_c() -> None:
    fake = _c_fake()
    guarded = hw_probe.GuardedResource(fake, policy=hw_probe.GuardPolicy('C', 15.0, 20.0))
    for cmd in ('TRAN:PULS 15 V', 'TRAN:PULS 15', 'TRANsmitter:PULSe:LEVel 15.0 V'):
        guarded.write(cmd)
    assert guarded.query('TRAN:PULS?') == '15'
    with pytest.raises(hw_probe.ForbiddenCommand):
        guarded.write('TRAN:PULS 20 V')


@pytest.mark.parametrize('phase', ['A', 'B', 'AB', 'RST'])
def test_guard_refuses_every_tran_puls_write_outside_phase_c(phase: str) -> None:
    guarded = hw_probe.GuardedResource(_c_fake(), policy=_policy(phase, 20.0))
    with pytest.raises(hw_probe.ForbiddenCommand):
        guarded.write('TRAN:PULS 20 V')


@pytest.mark.parametrize('phase', ['A', 'B', 'AB', 'C'])
def test_guard_refuses_rst_except_in_phase_rst(phase: str) -> None:
    fake = _c_fake()
    guarded = hw_probe.GuardedResource(fake, policy=_policy(phase, 20.0))
    with pytest.raises(hw_probe.ForbiddenCommand):
        guarded.write('*RST')
    assert fake.log == []


def test_guard_lets_rst_through_in_phase_rst_only() -> None:
    fake = _c_fake()
    guarded = hw_probe.GuardedResource(fake, policy=_policy('RST'))
    guarded.write('*RST')
    assert fake.log == ['*RST']
    with pytest.raises(hw_probe.ForbiddenCommand):
        guarded.write('*RST;TRAN:ENAB ON')


@pytest.mark.parametrize('phase', PHASES)
def test_guard_refuses_config_everywhere(phase: str) -> None:
    fake = _c_fake()
    guarded = hw_probe.GuardedResource(fake, policy=_policy(phase, 20.0))
    with guarded.pulser_gate():
        for cmd in ('/config/dev.eth 1', '/CONFIG/dev.wlan', 'FREQ 1 MHZ /config'):
            with pytest.raises(hw_probe.ForbiddenCommand):
                guarded.write(cmd)
        with pytest.raises(hw_probe.ForbiddenCommand):
            guarded.query('/config/dev.eth?')
    assert fake.log == []


@pytest.mark.parametrize(
    'cmd', ['FREQ 1 MHZ;TRAN:ENAB ON', 'FREQ 1 MHZ\r\nTRAN:ENAB ON', 'GAIN 0\n*RST']
)
def test_guard_refuses_chained_commands(cmd: str) -> None:
    fake = _c_fake()
    guarded = hw_probe.GuardedResource(fake, policy=_policy('C', 20.0))
    with pytest.raises(hw_probe.ForbiddenCommand):
        guarded.write(cmd)
    assert fake.log == []


def test_guard_refuses_a_tab_separated_tran_puls() -> None:
    guarded = hw_probe.GuardedResource(_c_fake(), policy=_policy('B'))
    with pytest.raises(hw_probe.ForbiddenCommand):
        guarded.write('TRAN:PULS\t50 V')


def test_guard_announces_pulser_commands_before_sending_them() -> None:
    events: list[str] = []
    fake = _c_fake()
    guarded = hw_probe.GuardedResource(fake, policy=hw_probe.GuardPolicy('C', 15.0, 20.0))
    guarded.set_announce(lambda cmd: events.append(f'announce {cmd}; log has {len(fake.log)}'))
    guarded.write('TRAN:PULS 15 V')
    with guarded.pulser_gate():
        guarded.write('TRAN:ENAB ON')
    guarded.write('TRAN:ENAB OFF')
    guarded.write('FREQ 1 MHZ')
    assert events == [
        'announce TRAN:PULS 15 V; log has 0',
        'announce TRAN:ENAB ON; log has 1',
    ]
    with pytest.raises(hw_probe.ForbiddenCommand):
        guarded.write('TRAN:PULS 25 V')
    assert len(events) == 2  # a refused command is not announced


@pytest.mark.parametrize(('phase', 'step'), [('A', 3), ('B', 6), ('AB', 6), ('RST', 13), ('C', 10)])
def test_a_step_that_tries_pulser_on_outside_the_function_is_refused_and_nothing_is_sent(
    phase: str,
    step: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def rogue(p: Any) -> None:
        p.plug.write('TRAN:ENAB ON')

    if phase == 'C':  # steps 10b and 11 would legitimately pulse
        monkeypatch.setitem(hw_probe.STEPS, '10b', ('noop', lambda p: None))
        monkeypatch.setitem(hw_probe.STEPS, 11, ('noop', lambda p: None))
    monkeypatch.setitem(hw_probe.STEPS, step, ('rogue', rogue))
    extra = {'C': ['--pulse-v', '20'], 'RST': ['--allow-rst']}.get(phase, [])
    fake = _c_fake()
    code = hw_probe.main(['--fake', '--phase', phase, '--out', str(tmp_path), *extra], fake=fake)
    out = capsys.readouterr().out
    assert code != 0
    assert f'STEP {step} FAILED' in out
    assert 'TRAN:ENAB ON' not in fake.log
    assert 'ON' not in [c.split(' ')[-1] for c in _enab_writes(fake.log)]
    assert _enab_writes(fake.log)[-1].endswith('OFF')


def test_a_step_that_writes_another_pulser_voltage_in_phase_c_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def rogue(p: Any) -> None:
        p.plug.write('TRAN:PULS 25 V')

    monkeypatch.setitem(hw_probe.STEPS, 11, ('rogue', rogue))
    fake = _c_fake()
    hw_probe.main(_c_args(tmp_path), fake=fake)
    assert 'STEP 11 FAILED' in capsys.readouterr().out
    assert 'TRAN:PULS 25 V' not in fake.log


# ── section 3.2: pulsed_acquire always ends with the pulser off ──────────────


def _sent_after(log: list[str], start: int) -> list[str]:
    return log[start:]


def test_pulsed_acquire_order_on_only_while_reading_then_off_stop_verify(tmp_path: Path) -> None:
    fake = _c_fake()
    assert hw_probe.main(_c_args(tmp_path), fake=fake) == 0
    log = fake.log
    first_on = log.index('TRAN:ENAB ON')
    # the stream is open (STAR AUTO sent) before the pulser goes on
    assert 'STAR AUTO' in log[:first_on]
    assert log[first_on : first_on + 3] == ['TRAN:ENAB ON', 'SYSTem:ERRor?', 'TRAN:ENAB?']
    off = log.index('TRAN:ENAB OFF', first_on)
    assert log[off : off + 4] == ['TRAN:ENAB OFF', 'STOP', 'TRAN:ENAB?', 'SYSTem:ERRor?']
    # nothing is written between the read-back of ON and the OFF
    between = [c for c in log[first_on + 3 : off] if not c.endswith('?')]
    assert between == []


def test_phase_c_every_pulser_on_is_followed_by_off_and_the_last_enab_write_is_off(
    tmp_path: Path,
) -> None:
    fake = _c_fake()
    assert hw_probe.main(_c_args(tmp_path), fake=fake) == 0
    writes = _enab_writes(fake.log)
    assert writes.count('TRAN:ENAB ON') == 5  # steps 10, 10b and the three delays of 11
    for i, cmd in enumerate(writes):
        if cmd == 'TRAN:ENAB ON':
            assert writes[i + 1] == 'TRAN:ENAB OFF'
    assert writes[-1] == 'TRAN:ENAB OFF'
    assert fake._values['TRAN:ENAB'] == '0'
    # between an ON and its OFF there is exactly one STOP at most
    on = [i for i, c in enumerate(fake.log) if c == 'TRAN:ENAB ON']
    for i in on:
        nxt = fake.log.index('TRAN:ENAB OFF', i)
        assert 'TRAN:ENAB ON' not in fake.log[i + 1 : nxt]


class _RaisingReader(hw_probe.FrameReader):
    def read(self, n: int, timeout_s: float) -> list[bytes]:
        raise TimeoutError('injected: no packets')


def test_an_exception_while_reading_packets_still_sends_off_and_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(hw_probe, 'FrameReader', _RaisingReader)
    fake = _c_fake()
    code = hw_probe.main(_c_args(tmp_path), fake=fake)
    out = capsys.readouterr().out
    assert code == hw_probe.EXIT_FAILED_STEP
    assert 'STEP 10 FAILED' in out
    on = [i for i, c in enumerate(fake.log) if c == 'TRAN:ENAB ON']
    assert on, 'the pulser was switched on'
    for i in on:
        tail = fake.log[i:]
        off = tail.index('TRAN:ENAB OFF')
        assert (
            'STOP'
            in tail[off : tail.index('TRAN:ENAB ON', 1) if 'TRAN:ENAB ON' in tail[1:] else None]
        )
        assert tail.index('STOP') > off
        assert 'TRAN:ENAB?' in tail[off:]
    assert _enab_writes(fake.log)[-1] == 'TRAN:ENAB OFF'
    assert fake.sockets[-1].closed
    assert fake._values['TRAN:ENAB'] == '0'


def test_an_exception_while_switching_on_still_sends_off_and_stop(tmp_path: Path) -> None:
    class RejectsOn(FakeA1580Resource):
        def query(self, cmd: str) -> str:
            enab = [c for c in self.log if c.startswith('TRAN:ENAB ')]
            if cmd == 'TRAN:ENAB?' and enab and enab[-1] == 'TRAN:ENAB ON':
                self.log.append(cmd)
                raise TimeoutError('injected: no answer')
            return super().query(cmd)

    fake = RejectsOn(signal='transmission')
    code = hw_probe.main(_c_args(tmp_path), fake=fake)
    assert code == hw_probe.EXIT_FAILED_STEP
    writes = _enab_writes(fake.log)
    for i, cmd in enumerate(writes):
        if cmd == 'TRAN:ENAB ON':
            assert writes[i + 1] == 'TRAN:ENAB OFF'
    assert writes[-1] == 'TRAN:ENAB OFF'


def test_a_pulser_that_does_not_come_on_is_not_read_from(tmp_path: Path) -> None:
    class NeverOn(FakeA1580Resource):
        def write(self, cmd: str) -> None:
            if cmd == 'TRAN:ENAB ON':
                self.log.append(cmd)  # swallowed: reads back 0
                return
            super().write(cmd)

    fake = NeverOn(signal='transmission')
    code = hw_probe.main(_c_args(tmp_path), fake=fake)
    assert code == hw_probe.EXIT_FAILED_STEP
    assert _enab_writes(fake.log)[-1] == 'TRAN:ENAB OFF'


class _StuckOn(FakeA1580Resource):
    """The pulser ignores the first `swallow` OFF writes after it was switched on."""

    def __init__(self, swallow: int, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.swallow = swallow
        self.switched_on = False

    def write(self, cmd: str) -> None:
        if cmd == 'TRAN:ENAB ON':
            self.switched_on = True
        elif cmd == 'TRAN:ENAB OFF' and self.switched_on and self.swallow > 0:
            self.swallow -= 1
            self.log.append(cmd)
            return
        super().write(cmd)


def test_pulser_read_back_not_off_after_pulsed_acquire_aborts_with_exit_pulser(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _StuckOn(2, signal='transmission')  # the OFF of the finally and its one retry
    code = hw_probe.main(_c_args(tmp_path), fake=fake)
    captured = capsys.readouterr()
    assert code == hw_probe.EXIT_PULSER
    assert 'STEP 10 ABORTED' in captured.out
    assert 'STEP 10b' not in captured.out  # the run stopped
    assert 'RUN STOPPED' in captured.err
    assert fake.log.count('TRAN:ENAB ON') == 1
    assert 'FINAL DIFF' in captured.out  # tearDown and the diff still ran
    assert _enab_writes(fake.log)[-1] == 'TRAN:ENAB OFF'  # tearDown's OFF
    assert fake._values['TRAN:ENAB'] == '0'  # and it worked


def test_pulser_that_needs_the_retry_is_switched_off_with_a_warning_and_the_run_goes_on(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _StuckOn(1, signal='transmission')
    assert hw_probe.main(_c_args(tmp_path), fake=fake) == 0
    out = capsys.readouterr().out
    assert 'retry' in out
    assert 'STEP 10b:' in out


def test_pulser_that_cannot_be_read_back_after_pulsed_acquire_aborts(tmp_path: Path) -> None:
    class Mute(FakeA1580Resource):
        def query(self, cmd: str) -> str:
            if cmd == 'TRAN:ENAB?' and self.stop_count and self.switched_on:
                self.log.append(cmd)
                raise TimeoutError('injected')
            return super().query(cmd)

        switched_on = False

        def write(self, cmd: str) -> None:
            self.switched_on = self.switched_on or cmd == 'TRAN:ENAB ON'
            super().write(cmd)

    fake = Mute(signal='transmission')
    code = hw_probe.main(_c_args(tmp_path), fake=fake)
    assert code == hw_probe.EXIT_PULSER
    assert _enab_writes(fake.log)[-1] == 'TRAN:ENAB OFF'


def test_pulser_on_time_is_recorded(tmp_path: Path) -> None:
    assert hw_probe.main(_c_args(tmp_path), fake=_c_fake()) == 0
    report = _load_probe(tmp_path)
    pulses = report['pulses']
    assert len(pulses) == 5
    assert all(0 < x['on_s'] < 5 for x in pulses)
    assert all(x['verified_off'] is True for x in pulses)


def _load_probe(out: Path) -> dict[str, Any]:
    (path,) = out.glob('probe-*.json')
    return json.loads(path.read_text(encoding='utf-8'))


# ── section 3.1: preparation ─────────────────────────────────────────────────


def test_no_tran_puls_write_when_the_device_already_has_the_voltage(tmp_path: Path) -> None:
    fake = _c_fake()
    assert hw_probe.main(_c_args(tmp_path), fake=fake) == 0
    assert not [c for c in fake.log if c.startswith('TRAN:PULS ')]
    assert 'TRAN:PULS?' in fake.log


def test_exactly_one_tran_puls_write_when_pulse_v_differs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _c_fake()
    argv = ['--fake', '--phase', 'C', '--pulse-v', '15', '--out', str(tmp_path)]
    assert hw_probe.main(argv, fake=fake) == 0
    out = capsys.readouterr().out
    writes = [c for c in fake.log if c.startswith('TRAN:PULS ')]
    assert writes == ['TRAN:PULS 15 V']
    assert fake._values['TRAN:PULS'] == '15'
    assert 'PULSER: about to send' in out
    assert out.index('PULSER: about to send TRAN:PULS 15 V') < out.index(
        'PULSER: about to send TRAN:ENAB ON'
    )
    # before the first pulse, after the pulser-off check
    assert fake.log.index('TRAN:PULS 15 V') < fake.log.index('TRAN:ENAB ON')
    # the changed voltage is not written back, and the diff says so without failing the run
    assert 'CHANGED TRAN:PULS' in out
    assert 'phase C set the pulser voltage' in out


def test_every_pulser_on_is_announced_before_it_is_sent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert hw_probe.main(_c_args(tmp_path), fake=_c_fake()) == 0
    out = capsys.readouterr().out
    assert out.count('PULSER: about to send TRAN:ENAB ON') == 5


def test_voltage_that_does_not_read_back_aborts_with_exit_ceiling(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _c_fake(reject={'TRAN:PULS': '-221,"Settings conflict"'})
    argv = ['--fake', '--phase', 'C', '--pulse-v', '15', '--out', str(tmp_path)]
    code = hw_probe.main(argv, fake=fake)
    assert code == hw_probe.EXIT_CEILING
    assert 'TRAN:ENAB ON' not in fake.log  # never pulsed


def test_a_setting_that_is_refused_in_the_preparation_aborts_before_any_pulse(
    tmp_path: Path,
) -> None:
    fake = _c_fake(reject={'TRAN:FREQ': '-224,"Illegal parameter value"'})
    code = hw_probe.main(_c_args(tmp_path), fake=fake)
    assert code == hw_probe.EXIT_FAILED_STEP
    assert 'TRAN:ENAB ON' not in fake.log
    assert fake.log[-1] != 'TRAN:ENAB ON'


def test_device_pulser_above_the_ceiling_stops_phase_c_before_any_write(
    tmp_path: Path,
) -> None:
    fake = _c_fake()
    fake.write('TRAN:PULS 30 V')
    fake.log.clear()
    assert hw_probe.main(_c_args(tmp_path), fake=fake) == hw_probe.EXIT_CEILING
    assert 'TRAN:ENAB ON' not in fake.log
    assert [c for c in fake.log if c.startswith('DATA:LENG ')] == []
    assert fake._values['TRAN:PULS'] == '30'


def test_phase_c_writes_the_settings_of_the_spec_and_records_read_backs(
    tmp_path: Path,
) -> None:
    fake = _c_fake()
    assert hw_probe.main(_c_args(tmp_path), fake=fake) == 0
    for cmd in (
        'DATA:LENG 8192',
        'FREQ 1 MHZ',
        'TRIG:MODE INT',
        'TRIG:INT 100 MS',
        'TRAN:TYPE DUAL',
        'TRAN:FREQ 50 KHZ',
        'TRAN:DUR 1',
        'TRAN:IMP HIGH',
        'GAIN 0',
        'GAIN:TGC:MODE OFF',
        'AVER:COUN 0',
        'FILT:HPAS:IND 0',
        'TRIG:DEL 0 NS',
    ):
        assert cmd in fake.log, cmd
    report = _load_probe(tmp_path)
    prep = report['steps']['PREPARATION']['data']
    assert prep['readbacks']['TRIG:INT'] == '0.1'
    assert prep['readbacks']['DATA:LENG'] == '8192'
    assert prep['readbacks']['TRAN:FREQ'] == '50000'


def test_settings_follow_the_command_line(tmp_path: Path) -> None:
    fake = _c_fake()
    extra = [
        '--tran-freq', '2 MHZ', '--sample-freq', '2 MHZ', '--length', '4096',
        '--interval', '50 MS', '--gain', '6',
    ]  # fmt: skip
    assert hw_probe.main(_c_args(tmp_path, *extra), fake=fake) == 0
    for cmd in ('TRAN:FREQ 2 MHZ', 'FREQ 2 MHZ', 'DATA:LENG 4096', 'TRIG:INT 50 MS', 'GAIN 6'):
        assert cmd in fake.log, cmd


# ── section 3.3: step 10 analysis (pure functions) ───────────────────────────


def _noise(shape: tuple[int, int], seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return np.rint(rng.normal(11.0, 3.6, shape)).astype(np.int16)


def _burst_records(n: int = 2048, k: int = 4, onset: int = 300) -> np.ndarray:
    """k records: constant 11 plus a burst that peaks at `onset + 2`, gone by `onset + 60`."""
    records = np.full((k, n), 11, dtype=np.int16)
    shape = (500.0 * np.exp(-np.arange(60) / 8.0) * np.where(np.arange(60) == 2, 1.0, 0.5)).astype(
        np.int16
    )
    records[:, onset : onset + 60] += shape
    return records


def _analyse(baseline: np.ndarray, pulsed: np.ndarray) -> dict[str, Any]:
    return hw_probe.analyse_pulsed(baseline, pulsed, 1e6)


def test_analysis_finds_onset_and_peak_index_and_converts_to_microseconds() -> None:
    result = _analyse(_noise((5, 2048)), _burst_records())
    assert result['threshold'] == max(10 * result['baseline_std'], 20)
    assert 10.5 < result['baseline_mean'] < 11.5
    for packet in result['packets']:
        assert packet['onset_index'] == 300
        assert packet['peak_index'] == 302
        assert packet['onset_us'] == pytest.approx(300.0)
        assert packet['peak'] == pytest.approx(packet['max'] - result['baseline_mean'])
        assert packet['max'] > 300
        assert packet['min'] == 11
    assert result['onset_spread_samples'] == 0
    assert result['peak_index_spread_samples'] == 0


def test_analysis_spread_of_onset_and_peak_between_packets() -> None:
    pulsed = np.concatenate([_burst_records(k=1, onset=300), _burst_records(k=1, onset=310)])
    result = _analyse(_noise((5, 2048)), pulsed)
    assert result['onset_spread_samples'] == 10
    assert result['peak_index_spread_samples'] == 10


def test_analysis_clean_record_has_no_warning() -> None:
    result = _analyse(_noise((5, 2048)), _burst_records())
    assert result['warnings'] == []


def test_analysis_warns_about_saturation_by_value() -> None:
    pulsed = _burst_records()
    pulsed[1, 300] = 32767
    pulsed[2, 310] = -32768
    warnings = _analyse(_noise((5, 2048)), pulsed)['warnings']
    assert len(warnings) == 1
    assert 'saturation' in warnings[0]
    assert '2 samples' in warnings[0]


def test_analysis_warns_about_a_flat_top() -> None:
    pulsed = _burst_records()
    pulsed[0, 400:405] = 20000  # the largest value, repeated 5 times in a row
    warnings = _analyse(_noise((5, 2048)), pulsed)['warnings']
    assert len(warnings) == 1
    assert 'saturation' in warnings[0]
    assert 'flat' in warnings[0]
    assert '5 samples' in warnings[0]


def test_analysis_three_equal_maxima_are_not_a_flat_top() -> None:
    pulsed = _burst_records()
    pulsed[0, 400:403] = 20000
    assert _analyse(_noise((5, 2048)), pulsed)['warnings'] == []


def test_analysis_warns_about_no_signal() -> None:
    pulsed = np.full((4, 2048), 11, dtype=np.int16)
    warnings = _analyse(_noise((5, 2048)), pulsed)['warnings']
    assert len(warnings) == 1
    assert 'no signal' in warnings[0]


def test_analysis_one_packet_with_a_signal_is_not_no_signal() -> None:
    pulsed = _burst_records()
    pulsed[1:] = 11
    assert _analyse(_noise((5, 2048)), pulsed)['warnings'] == []


def test_analysis_warns_about_a_signal_in_the_baseline() -> None:
    baseline = _noise((5, 2048))
    baseline[2, 500] = 400
    warnings = _analyse(baseline, _burst_records())['warnings']
    assert len(warnings) == 1
    assert 'baseline' in warnings[0]
    assert 'pulser off' in warnings[0]


def test_analysis_warns_about_a_signal_at_the_end_of_the_record() -> None:
    pulsed = _burst_records()
    pulsed[3, -10:] = 300  # the last 5 % of 2048 samples is 102
    warnings = _analyse(_noise((5, 2048)), pulsed)['warnings']
    assert len(warnings) == 1
    assert 'end of the record' in warnings[0]


def test_analysis_a_signal_just_before_the_last_5_percent_is_not_flagged() -> None:
    pulsed = _burst_records()
    pulsed[3, 1900:1910] = 300
    assert _analyse(_noise((5, 2048)), pulsed)['warnings'] == []


def test_analysis_result_is_json_serialisable() -> None:
    result = _analyse(_noise((5, 2048)), _burst_records())
    json.dumps(result)


def test_cross_lag_is_positive_when_the_signal_comes_later() -> None:
    ref = np.zeros(1000)
    ref[100:140] = np.sin(np.arange(40) * 0.7)
    for lag in (0, 7, -7, 300):
        other = np.roll(ref, lag)
        assert hw_probe.cross_lag(ref, other) == lag


# ── steps 10, 10b, 11 on the fake ────────────────────────────────────────────


def test_phase_c_on_the_fake_finds_the_burst_and_exits_0(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = hw_probe.main(_c_args(tmp_path, '--gain', '40'), fake=_c_fake())  # 18 counts at 0 dB
    out = capsys.readouterr().out
    assert code == 0, out
    assert 'FAILED' not in out
    for label in ('STEP 10:', 'STEP 10b:', 'STEP 11:', 'PULSER-OFF CHECK', 'SAFETY CHECK'):
        assert label in out
    assert out.index('SAFETY CHECK') < out.index('PULSER-OFF CHECK') < out.index('STEP 10:')
    report = _load_probe(tmp_path)
    step10 = report['steps']['STEP 10']['data']
    assert step10['analysis']['warnings'] == []
    packets = step10['analysis']['packets']
    assert len(packets) == 10
    assert {p['onset_index'] for p in packets} == {4}  # the fake's arrival 3 us at 1 MHz
    assert {p['onset_us'] for p in packets} == {4.0}
    assert report['steps']['FINAL DIFF']['data']['changed'] == {}
    assert report['exit_code'] == 0
    assert report['warnings'] == []


def test_step_10b_reports_averaging_with_a_real_signal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _c_fake()
    assert hw_probe.main(_c_args(tmp_path, '--gain', '40'), fake=fake) == 0
    data = _load_probe(tmp_path)['steps']['STEP 10b']['data']
    assert data['peak_ratio'] == pytest.approx(1.0, abs=0.1)  # a mean
    # the burst starts at sample 3: the noise comes from the last quarter of the record
    assert data['noise_ratio'] == pytest.approx(0.25, abs=0.08)
    assert 'last quarter' in data['noise_part']
    assert data['ascan_counts'] == [1]
    assert 'AVER:COUN 4' in fake.log
    assert (
        fake.log[len(fake.log) - 1 - fake.log[::-1].index('AVER:COUN 0') :].count('AVER:COUN 0')
        >= 1
    )
    assert fake._values['AVER:COUN'] == '0'
    out = capsys.readouterr().out
    assert '1.0 means a mean, 16 a sum' in out


def test_step_10b_without_enough_pre_onset_samples_uses_the_last_quarter(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert hw_probe.main(_c_args(tmp_path, '--gain', '40'), fake=_c_fake()) == 0
    data = _load_probe(tmp_path)['steps']['STEP 10b']['data']
    assert data['noise_ratio'] == pytest.approx(0.25, abs=0.08)
    assert 'pre_onset_samples' not in data
    out = capsys.readouterr().out
    assert 'noise std in the last quarter of the record' in out
    assert 'no pre-onset samples' not in out


def test_step_10b_with_enough_pre_onset_samples_uses_them(tmp_path: Path) -> None:
    import a1580_openhtf.fake_resource as fr

    old = fr._TRANSMISSION_ARRIVAL_S
    fr._TRANSMISSION_ARRIVAL_S = 200e-6  # 200 samples of noise before the onset
    try:
        assert hw_probe.main(_c_args(tmp_path, '--gain', '40'), fake=_c_fake()) == 0
    finally:
        fr._TRANSMISSION_ARRIVAL_S = old
    data = _load_probe(tmp_path)['steps']['STEP 10b']['data']
    assert data['pre_onset_samples'] >= 50
    assert data['noise_part'].startswith('the first')
    assert data['noise_ratio'] == pytest.approx(0.25, abs=0.08)


def test_step_11_reports_that_the_fake_signal_does_not_move(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _c_fake()
    assert hw_probe.main(_c_args(tmp_path, '--gain', '40'), fake=fake) == 0
    data = _load_probe(tmp_path)['steps']['STEP 11']['data']
    delays = {d['delay']: d for d in data['delays']}
    assert list(delays) == ['0 NS', '1000 US', '2000 US']
    assert delays['0 NS']['shift_samples'] == 0
    for text, ns in (('1000 US', 1_000_000), ('2000 US', 2_000_000)):
        entry = delays[text]
        assert entry['expected_shift_samples'] == ns * 1e-9 * 1e6
        assert entry['shift_samples'] == 0  # measured: TRIG:DEL does not move the signal
        assert entry['onset_index'] == 4
        assert 'delayed together' in entry['verdict']
    assert 'burst and record are delayed together' in capsys.readouterr().out
    # put back at the end of the step (the restore of the snapshot comes later)
    assert fake.log.index('TRIG:DEL 0 NS', fake.log.index('TRIG:DEL 2000 US')) > 0


def test_step_11_verdicts_for_earlier_other_and_left_the_window() -> None:
    verdict = hw_probe.shift_verdict
    assert 'later by the delay' in verdict(1000, 1000.0, True)
    assert 'later by the delay' in verdict(1001, 1000.0, True)
    assert 'earlier by the delay' in verdict(-999, 1000.0, True)
    assert 'does not move' in verdict(1, 1000.0, True)
    other = verdict(500, 1000.0, True)
    assert 'something else' in other
    assert 'later' in other
    assert '500' in other
    assert 'earlier' in verdict(-400, 1000.0, True)
    assert 'left the window' in verdict(0, 1000.0, False)


def test_step_11_verdicts_without_a_reference_signal_and_for_a_signal_that_does_not_move() -> None:
    verdict = hw_probe.shift_verdict
    assert 'nothing to compare' in verdict(0, 1000.0, False, False)
    assert 'nothing to compare' in verdict(0, 1000.0, True, False)
    assert 'left the window' not in verdict(0, 1000.0, False, False)
    assert 'left the window' in verdict(0, 1000.0, False, True)
    assert 'burst and record are delayed together' in verdict(0, 1000.0, True, True)
    assert 'delayed together' not in verdict(0, 0.0, True, True)


def test_step_11_without_a_signal_at_the_reference_says_nothing_to_compare(
    tmp_path: Path,
) -> None:
    assert hw_probe.main(_c_args(tmp_path), fake=_c_fake()) == 0  # 18 counts at 0 dB: below 20
    data = _load_probe(tmp_path)['steps']['STEP 11']['data']
    for entry in data['delays'][1:]:
        assert entry['verdict'] == 'no signal above the threshold, nothing to compare'
        assert entry['shift_samples'] is None


def test_a_warning_of_step_10_is_printed_stored_repeated_and_does_not_change_the_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _c_fake()
    fake._values['GAIN'] = '0'
    # a pulser that does nothing: no signal in any pulsed packet
    original = fake._packet

    def quiet() -> bytes:
        fake._values['TRAN:ENAB'] = '0'
        return original()

    fake._packet = quiet  # type: ignore[method-assign]
    code = hw_probe.main(_c_args(tmp_path), fake=fake)
    out = capsys.readouterr().out
    assert code == 0
    assert out.count('WARNING: no signal') == 2  # where it was found and in the block
    assert 'WARNINGS' in out
    assert out.index('FINAL DIFF') < out.index('\nWARNINGS')
    report = _load_probe(tmp_path)
    assert len(report['warnings']) == 1
    assert 'no signal' in report['warnings'][0]
    assert report['exit_code'] == 0


def test_no_warnings_block_without_warnings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert hw_probe.main(_c_args(tmp_path, '--gain', '40'), fake=_c_fake()) == 0
    assert 'WARNINGS' not in capsys.readouterr().out


def test_saturation_in_the_fake_signal_is_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = hw_probe.main(_c_args(tmp_path, '--gain', '75'), fake=_c_fake())  # 18 * 10^3.5 clips
    out = capsys.readouterr().out
    assert code == 0
    assert 'WARNING: saturation' in out


def test_npz_files_are_written_to_out_with_int16_arrays(tmp_path: Path) -> None:
    assert hw_probe.main(_c_args(tmp_path), fake=_c_fake()) == 0
    names = sorted(p.name for p in tmp_path.glob('phaseC-*.npz'))
    assert len(names) == 3
    assert [n.rsplit('-', 1)[1] for n in names] == ['step10.npz', 'step10b.npz', 'step11.npz']
    assert all(n.startswith('phaseC-100500-') for n in names)
    with np.load(tmp_path / names[0], allow_pickle=False) as npz:
        assert npz['baseline'].dtype == np.int16
        assert npz['pulsed'].dtype == np.int16
        assert npz['baseline'].shape == (5, 8192)
        assert npz['pulsed'].shape == (10, 8192)
        settings = json.loads(str(npz['settings']))
        assert settings['FREQ'] == '1 MHZ'
        assert settings['pulse_v'] == 20
    with np.load(tmp_path / names[1], allow_pickle=False) as npz:
        assert npz['pulsed'].dtype == np.int16
        assert npz['pulsed'].shape == (5, 8192)
    with np.load(tmp_path / names[2], allow_pickle=False) as npz:
        keys = [k for k in npz.files if k != 'settings']
        assert len(keys) == 3
        assert all(npz[k].dtype == np.int16 and npz[k].shape == (5, 8192) for k in keys)


# ── section 3.6: phase RST ───────────────────────────────────────────────────


def _power_on_like() -> FakeA1580Resource:
    """The measured power-on facts that matter here: pulser on, DUAL, DATA:LENG 114688.

    (`power_on=True` also gives `TRIG:INT` 1 s and an empty `GAIN:TGC:ARB`, which the fake's
    `*RST` then fills: a diff that is the fake's, not the tool's.)
    """
    fake = FakeA1580Resource(length=114688)
    fake._values['TRAN:ENAB'] = '1'
    fake._values['TRAN:TYPE'] = 'DUAL'
    return fake


def _rst_args(out: Path, *extra: str) -> list[str]:
    return ['--fake', '--phase', 'RST', '--allow-rst', '--out', str(out), *extra]


def test_phase_rst_sends_rst_once_switches_the_pulser_off_at_once_and_prints_the_table(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _power_on_like()
    code = hw_probe.main(_rst_args(tmp_path), fake=fake)
    out = capsys.readouterr().out
    assert code == 0, out
    assert fake.log.count('*RST') == 1
    i = fake.log.index('*RST')
    assert fake.log[i + 1] == 'TRAN:ENAB?'
    assert fake.log[i + 2] == 'TRAN:ENAB OFF'
    assert 'TRAN:ENAB ON' not in fake.log
    assert 'TRAN:PULS 20 V' not in fake.log
    assert 'PULSER' not in out.split('STEP 13')[1].split('FINAL DIFF')[0].replace('PULSER-OFF', '')
    # the table: three columns, one row per STATE_HEADERS entry plus DATA:PORT and SYST:ERR:COUN
    assert 'after *RST' in out
    assert 'snapshot (step 2)' in out
    assert 'vendor DEFault' in out
    for header in (*STATE_HEADERS, 'DATA:PORT', 'SYST:ERR:COUN'):
        assert f'{header} ' in out
    assert '= snapshot' in out
    assert '= vendor default' in out
    # restore done (power-on DATA:LENG is the known defect, so the diff on it is expected)
    assert any(c.startswith('FREQ ') for c in fake.log[i + 3 :])
    assert 'RESTORE FAILED (known firmware defect' in out


def test_phase_rst_table_marks_rows_and_is_in_the_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _power_on_like()
    assert hw_probe.main(_rst_args(tmp_path), fake=fake) == 0
    out = capsys.readouterr().out
    assert 'before *RST (detuned)' in out
    assert '*RST changed 0 of 6 detuned settings' in out
    data = _load_probe(tmp_path)['steps']['STEP 13']['data']
    rows = {r['header']: r for r in data['table']}
    assert set(rows) == {*STATE_HEADERS, 'DATA:PORT', 'SYST:ERR:COUN'}
    assert data['detuned_changed_by_rst'] == []
    # the fake's `*RST` changes nothing (measured on the device): the six keep the detuned value
    # (the fake's TRIG:INT is already 10 ms, so that row is no detune and keeps its old mark)
    assert rows['TRIG:INT']['mark'] == '= snapshot, = vendor default'
    for header, before in (('GAIN', '6'), ('AVER:COUN', '2')):
        assert rows[header]['before_rst'] == before
        assert rows[header]['after_rst'] == before
        assert rows[header]['mark'] == 'kept the detuned value'
    assert rows['TRAN:FREQ']['before_rst'] == '50000'
    assert rows['FILT:HPAS:IND']['before_rst'] == '2'
    assert rows['TRIG:DEL']['before_rst'] == '0'
    # an undetuned header keeps the old marks; DUAL is the power-on value, not the default
    assert rows['TRAN:TYPE']['after_rst'] == 'DUAL'
    assert rows['TRAN:TYPE']['mark'] == '= snapshot'
    assert rows['TRAN:IMP']['mark'] == '= snapshot, = vendor default'
    assert rows['DATA:PORT']['snapshot'] == '-'
    assert rows['SYST:ERR:COUN']['default'] == '-'
    # detune writes: the six, never the pulser or its voltage
    detune = [
        c for c in fake.log[: fake.log.index('*RST')] if c.split(' ')[0] in hw_probe.RST_DETUNE
    ]
    assert len(detune) >= 6
    assert not [c for c in fake.log if c.startswith(('TRAN:PULS ', 'TRAN:ENAB ON'))]


def test_phase_rst_no_detune_gives_the_old_table(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _power_on_like()
    assert hw_probe.main(_rst_args(tmp_path, '--no-detune'), fake=fake) == 0
    out = capsys.readouterr().out
    assert 'before *RST (detuned)' not in out
    assert 'detuned settings' not in out
    assert 'GAIN 6' not in fake.log
    rows = {r['header']: r for r in _load_probe(tmp_path)['steps']['STEP 13']['data']['table']}
    assert 'before_rst' not in rows['GAIN']
    assert rows['TRAN:TYPE']['mark'] == '= snapshot'


def test_phase_rst_detunes_only_settings_that_are_not_the_pulser() -> None:
    assert not {h for h in hw_probe.RST_DETUNE if h.startswith(('TRAN:PULS', 'TRAN:ENAB'))}
    assert len(hw_probe.RST_DETUNE) == 6


def test_phase_rst_row_that_matches_neither_is_marked_neither(tmp_path: Path) -> None:
    class Odd(FakeA1580Resource):
        def write(self, cmd: str) -> None:
            super().write(cmd)
            if cmd == '*RST':
                self._values['GAIN'] = '33'

    assert hw_probe.main(_rst_args(tmp_path, '--no-detune'), fake=Odd()) == 0
    rows = {r['header']: r for r in _load_probe(tmp_path)['steps']['STEP 13']['data']['table']}
    assert rows['GAIN']['mark'] == 'neither'


def test_phase_rst_detuned_rows_that_a_reset_changes_are_marked_and_counted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class Resets(FakeA1580Resource):
        def write(self, cmd: str) -> None:
            super().write(cmd)
            if cmd == '*RST':
                self._values['GAIN'] = '33'  # a third value
                self._values['AVER:COUN'] = DEFAULTS['AVER:COUN']  # the vendor default
                self._values['TRIG:DEL'] = '15000'  # the snapshot value of the plain fake

    assert hw_probe.main(_rst_args(tmp_path), fake=Resets()) == 0
    data = _load_probe(tmp_path)['steps']['STEP 13']['data']
    rows = {r['header']: r for r in data['table']}
    assert rows['GAIN']['mark'] == 'a third value'
    assert rows['AVER:COUN']['mark'] == 'back to the snapshot value'  # snapshot 0 = default
    assert rows['TRAN:FREQ']['mark'] == 'kept the detuned value'
    assert sorted(data['detuned_changed_by_rst']) == ['AVER:COUN', 'GAIN', 'TRIG:DEL']
    assert '*RST changed 3 of 6 detuned settings' in capsys.readouterr().out


def test_phase_rst_pulser_that_comes_on_with_rst_is_a_result_and_ends_off(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class OnAfterRst(FakeA1580Resource):
        def write(self, cmd: str) -> None:
            super().write(cmd)
            if cmd == '*RST':
                self._values['TRAN:ENAB'] = '1'

    fake = OnAfterRst()
    code = hw_probe.main(_rst_args(tmp_path), fake=fake)
    out = capsys.readouterr().out
    assert code == 0
    assert fake._values['TRAN:ENAB'] == '0'
    assert _enab_writes(fake.log)[-1] == 'TRAN:ENAB OFF'
    data = _load_probe(tmp_path)['steps']['STEP 13']['data']
    assert data['enab_after_rst'] == '1'
    assert 'pulser was ON right after *RST' in out


def test_phase_rst_pulser_that_stays_on_after_rst_aborts_with_exit_pulser(
    tmp_path: Path,
) -> None:
    class Stuck(FakeA1580Resource):
        armed = False

        def write(self, cmd: str) -> None:
            if cmd == '*RST':
                self.armed = True
            if cmd == 'TRAN:ENAB OFF' and self.armed and not self.stop_count:
                self.log.append(cmd)
                self._values['TRAN:ENAB'] = '1'
                return
            super().write(cmd)
            if cmd == '*RST':
                self._values['TRAN:ENAB'] = '1'

    fake = Stuck()
    code = hw_probe.main(_rst_args(tmp_path), fake=fake)
    assert code == hw_probe.EXIT_PULSER
    assert _enab_writes(fake.log)[-1] == 'TRAN:ENAB OFF'
    assert fake._values['TRAN:ENAB'] == '0'


def test_phase_rst_error_queue_after_rst_is_recorded(tmp_path: Path) -> None:
    class Noisy(FakeA1580Resource):
        def write(self, cmd: str) -> None:
            super().write(cmd)
            if cmd == '*RST':
                self._queue_error('-200,"Execution error"')

    assert hw_probe.main(_rst_args(tmp_path), fake=Noisy()) == 0
    data = _load_probe(tmp_path)['steps']['STEP 13']['data']
    assert data['errors_after_rst'] == ['-200,"Execution error"']


def test_phase_rst_never_sends_a_pulser_on_or_a_voltage(tmp_path: Path) -> None:
    fake = _power_on_like()
    assert hw_probe.main(_rst_args(tmp_path), fake=fake) == 0
    assert 'ON' not in [c.split(' ')[-1] for c in _enab_writes(fake.log)]
    assert not [c for c in fake.log if c.startswith('TRAN:PULS ')]


# ── section 3.7: teardown, expected restore failure, exit code ───────────────


def test_power_on_data_leng_restore_failure_is_expected_and_the_exit_code_is_0(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _c_fake(power_on=True)
    code = hw_probe.main(_c_args(tmp_path), fake=fake)
    out = capsys.readouterr().out
    assert code == 0, out
    line = next(x for x in out.splitlines() if x.startswith('RESTORE FAILED'))
    assert line.startswith(
        'RESTORE FAILED (known firmware defect: power-on DATA:LENG is refused by the setter): '
    )
    assert 'DATA:LENG' in line
    report = _load_probe(tmp_path)
    details = report['steps']['TEARDOWN']['data']['restore_failure_details']
    assert len(details) == 1
    assert details[0]['expected'] is True
    assert details[0]['failure'].startswith('DATA:LENG')
    changed = report['final_diff']['changed']
    assert changed['DATA:LENG']['expected'] is True
    assert changed['DATA:LENG']['was'] == '114688'
    assert changed['TRAN:ENAB']['expected'] is True
    assert report['exit_code'] == 0


def test_other_restore_failures_still_fail_phase_c(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class Refuses(FakeA1580Resource):
        def write(self, cmd: str) -> None:
            if cmd.startswith('TRAN:DUR ') and self.stop_count:  # only the restore
                self.log.append(cmd)
                self._queue_error('-224,"Illegal parameter value"')
                return
            super().write(cmd)

    fake = Refuses(signal='transmission')
    fake._values['TRAN:DUR'] = (
        '2'  # so that step 10's TRAN:DUR 1 changes it and the restore must undo it
    )
    code = hw_probe.main(_c_args(tmp_path), fake=fake)
    out = capsys.readouterr().out
    assert code == hw_probe.EXIT_FAILED_STEP
    assert 'RESTORE FAILED: TRAN:DUR' in out
    details = _load_probe(tmp_path)['steps']['TEARDOWN']['data']['restore_failure_details']
    assert details[0]['expected'] is False


def test_a_data_leng_restore_failure_with_a_legal_snapshot_value_is_not_expected(
    tmp_path: Path,
) -> None:
    fake = _c_fake(reject={'DATA:LENG': '-224,"Illegal parameter value"'})
    # the preparation already fails on DATA:LENG, so this only checks the classification
    code = hw_probe.main(_c_args(tmp_path), fake=fake)
    assert code == hw_probe.EXIT_FAILED_STEP


def test_phases_b_and_ab_keep_marking_a_data_leng_restore_failure_as_a_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeA1580Resource(power_on=True)
    code = hw_probe.main(['--fake', '--phase', 'B', '--out', str(tmp_path)], fake=fake)
    out = capsys.readouterr().out
    assert code == hw_probe.EXIT_FAILED_STEP
    assert 'RESTORE FAILED: DATA:LENG' in out
    assert 'known firmware defect' not in out


# ── section 4: dry-run ───────────────────────────────────────────────────────


def test_dry_run_phase_c_lists_the_pulser_commands_and_adjusts_never_sent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _no_sockets(monkeypatch)
    assert hw_probe.main(['--dry-run', '--phase', 'C', '--pulse-v', '20']) == 0
    out = capsys.readouterr().out
    lines = [line.strip() for line in out.splitlines()]
    assert out.splitlines()[0].startswith('DRY RUN, phase C')
    for number in ('10', '10b', '11'):
        assert f'STEP {number}:' in out
    assert 'STEP 13:' not in out
    assert out.count('TRAN:ENAB ON') == 5 + 1  # five pulses and the NEVER SENT line
    on_lines = [x for x in lines if x.startswith('TRAN:ENAB ON')]
    assert len(on_lines) == 5
    for i, line in enumerate(lines):
        if line.startswith('TRAN:ENAB ON'):
            assert any(x.startswith('TRAN:ENAB OFF') for x in lines[i : i + 8])
    assert any(
        x.startswith('TRAN:PULS 20 V') and 'only if TRAN:PULS? differs from --pulse-v' in x
        for x in lines
    )
    for cmd in (
        'DATA:LENG 8192',
        'FREQ 1 MHZ',
        'TRIG:INT 100 MS',
        'TRAN:FREQ 50 KHZ',
        'AVER:COUN 4',
        'TRIG:DEL 1000 US',
        'TRIG:DEL 2000 US',
    ):
        assert cmd in lines
    never = next(x for x in lines if x.startswith('NEVER SENT'))
    assert 'TRAN:ENAB ON outside' in never
    assert '*RST' in never
    assert '/config' in never
    assert out.index('STEP 10:') > out.index('PULSER-OFF CHECK')
    assert out.index('TEARDOWN') > out.index('STEP 11:')
    assert 'FINAL DIFF' in out


def test_dry_run_phase_rst_lists_rst_once_and_adjusts_never_sent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _no_sockets(monkeypatch)
    assert hw_probe.main(['--dry-run', '--phase', 'RST', '--allow-rst']) == 0
    out = capsys.readouterr().out
    lines = [line.strip() for line in out.splitlines()]
    assert out.splitlines()[0].startswith('DRY RUN, phase RST')
    assert 'STEP 13:' in out
    assert len([x for x in lines if x.startswith('*RST')]) == 1
    assert not [x for x in lines if x.startswith('TRAN:ENAB ON')]
    never = next(x for x in lines if x.startswith('NEVER SENT'))
    assert 'TRAN:ENAB ON' in never
    assert '*RST is sent once' in never
    assert 'STEP 10:' not in out
    assert out.index('PULSER-OFF CHECK') < out.index('STEP 13:') < out.index('TEARDOWN')


def test_dry_run_of_the_old_phases_keeps_the_old_never_sent_text(
    capsys: pytest.CaptureFixture[str],
) -> None:
    for phase in ('A', 'B', 'AB'):
        assert hw_probe.main(['--dry-run', '--phase', phase]) == 0
        out = capsys.readouterr().out
        assert 'NEVER SENT: TRAN:PULS writes, *RST, anything to /config/ (refused in code).' in out


def _plan_commands(blocks: list[tuple[str, list[str]]], first: str, last: str) -> list[str]:
    """Commands of the plan blocks from heading `first` to the one before `last`, no prose."""
    names = [h for h, _c in blocks]
    start = next(i for i, h in enumerate(names) if h.startswith(first))
    stop = next(i for i, h in enumerate(names) if h.startswith(last))
    commands = [c for _h, cs in blocks[start:stop] for c in cs]
    commands = [c for c in commands if 'only if TRAN:PULS? differs' not in c]
    commands = [c for c in commands if not c.startswith('read ')]
    return [c.split('   (')[0] for c in commands]


def test_dry_run_phase_c_matches_what_the_fake_run_sends(tmp_path: Path) -> None:
    args = hw_probe.build_parser().parse_args(['--phase', 'C', '--pulse-v', '20'])
    planned = _plan_commands(hw_probe.plan('C', args), 'PREPARATION', 'TEARDOWN')
    fake = _c_fake()
    assert hw_probe.main(_c_args(tmp_path), fake=fake) == 0
    start = fake.log.index('DATA:LENG 8192') - 1  # the error-queue drain before the setup
    last_stop = max(i for i, c in enumerate(fake.log) if c == 'STOP')
    sent = fake.log[start : last_stop - 1]  # up to tearDown's first OFF
    assert planned == sent


def test_dry_run_phase_c_with_a_different_voltage_matches_too(tmp_path: Path) -> None:
    args = hw_probe.build_parser().parse_args(['--phase', 'C', '--pulse-v', '15'])
    planned = [
        c
        for c in hw_probe.plan('C', args)[
            next(
                i
                for i, (h, _c) in enumerate(hw_probe.plan('C', args))
                if h.startswith('PREPARATION')
            )
        ][1]
    ]
    fake = _c_fake()
    argv = ['--fake', '--phase', 'C', '--pulse-v', '15', '--out', str(tmp_path)]
    assert hw_probe.main(argv, fake=fake) == 0
    start = fake.log.index('DATA:LENG 8192') - 1
    cut = start + len([c for c in planned])
    assert [c.split('   (')[0] for c in planned] == fake.log[
        start:cut
    ]  # conditional lines included


def test_dry_run_phase_rst_matches_what_the_fake_run_sends(tmp_path: Path) -> None:
    args = hw_probe.build_parser().parse_args(['--phase', 'RST', '--allow-rst'])
    planned = _plan_commands(hw_probe.plan('RST', args), 'STEP 13', 'TEARDOWN')
    fake = _power_on_like()
    assert hw_probe.main(_rst_args(tmp_path), fake=fake) == 0
    start = fake.log.index('GAIN 6') - 1  # the error-queue drain before the detune
    last_stop = max(i for i, c in enumerate(fake.log) if c == 'STOP')
    assert planned == fake.log[start : last_stop - 1]


@pytest.mark.parametrize('phase', ['C', 'RST'])
def test_dry_run_teardown_of_phases_c_and_rst_matches_what_the_fake_run_sends(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], phase: str
) -> None:
    extra = ['--pulse-v', '20'] if phase == 'C' else ['--allow-rst']
    assert hw_probe.main(['--dry-run', '--phase', phase, *extra]) == 0
    planned = _teardown_block(capsys.readouterr().out)
    fake = _c_fake()
    assert (
        hw_probe.main(['--fake', '--phase', phase, '--out', str(tmp_path), *extra], fake=fake) == 0
    )
    sent = fake.log[max(i for i, c in enumerate(fake.log) if c == 'STOP') - 1 :]
    skipped = planned.index('AVER:DEL:CONS <snapshot value>')
    planned = planned[:skipped] + planned[skipped + 2 :]
    assert [c.split(' ')[0] for c in planned] == [c.split(' ')[0] for c in sent]


@pytest.mark.parametrize('phase', ['C', 'RST'])
def test_commands_sent_by_phases_c_and_rst_are_all_in_the_plan(tmp_path: Path, phase: str) -> None:
    extra = ['--pulse-v', '15'] if phase == 'C' else ['--allow-rst']
    args = hw_probe.build_parser().parse_args(['--phase', phase, *extra])
    fake = _c_fake()
    assert (
        hw_probe.main(['--fake', '--phase', phase, '--out', str(tmp_path), *extra], fake=fake) == 0
    )
    planned = {
        _header_key(c)
        for _h, cs in hw_probe.plan(phase, args)
        for c in cs
        if not c.startswith('raw socket')
    }
    assert {_header_key(c) for c in fake.log} <= planned


# ── existing phases are untouched ────────────────────────────────────────────


def test_existing_phases_never_send_a_pulser_on_or_a_voltage(tmp_path: Path) -> None:
    for phase in ('A', 'B', 'AB'):
        fake = FakeA1580Resource()
        assert (
            hw_probe.main(['--fake', '--phase', phase, '--out', str(tmp_path / phase)], fake=fake)
            == 0
        )
        assert 'ON' not in [c.split(' ')[-1] for c in _enab_writes(fake.log)]
        assert not [c for c in fake.log if c.startswith('TRAN:PULS ') or c.startswith('*RST')]


def test_phase_ab_with_the_fake_flag_alone_still_works(tmp_path: Path) -> None:
    assert hw_probe.main(['--fake', '--phase', 'AB', '--out', str(tmp_path)]) == 0


def test_phase_c_with_the_fake_flag_alone_uses_the_transmission_fake(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert hw_probe.main(_c_args(tmp_path, '--gain', '40')) == 0
    assert 'WARNING: no signal' not in capsys.readouterr().out


# ── coherent signal, time per packet, --mem-clear, frame statistics ──────────


def _weak_wavelet_records(k: int = 10, n: int = 2048, amplitude: float = 16.0) -> np.ndarray:
    """k noisy records (std 3.6) with the same small wavelet in samples 11..40."""
    records = _noise((k, n), seed=7).astype(np.float64)
    records[:, 11:41] += amplitude * np.sin(np.arange(30) * 2 * np.pi / 20.0)
    return np.rint(records).astype(np.int16)


def test_coherent_signal_below_the_single_packet_threshold_is_found() -> None:
    result = _analyse(_noise((5, 2048)), _weak_wavelet_records())
    coherent = result['coherent']
    assert coherent['found']
    assert coherent['packets'] == 10
    assert 11 <= coherent['peak_index'] < 41
    assert 12 < coherent['peak'] < 20
    assert all(x['peak'] <= result['threshold'] for x in result['packets'])  # no single packet
    (warning,) = result['warnings']
    assert warning.startswith('no signal')
    assert 'but the mean of 10 packets shows a coherent signal' in warning
    assert 'try more gain' in warning


def test_pure_noise_has_no_coherent_signal_and_no_second_sentence() -> None:
    result = _analyse(_noise((5, 2048)), _noise((10, 2048), seed=9))
    assert not result['coherent']['found']
    (warning,) = result['warnings']
    assert 'coherent' not in warning


def test_step_10_prints_and_stores_the_coherent_signal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert hw_probe.main(_c_args(tmp_path), fake=_c_fake()) == 0  # 18 counts at 0 dB
    out = capsys.readouterr().out
    assert 'coherent signal in the mean of 10 packets: peak ' in out
    assert 'counts at sample ' in out
    assert 'try more gain' in out
    coherent = _load_probe(tmp_path)['steps']['STEP 10']['data']['analysis']['coherent']
    assert coherent['found']
    assert coherent['packets'] == 10


def test_step_10_says_none_without_a_coherent_signal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _c_fake()
    original = fake._packet

    def quiet() -> bytes:
        fake._values['TRAN:ENAB'] = '0'
        return original()

    fake._packet = quiet  # type: ignore[method-assign]
    assert hw_probe.main(_c_args(tmp_path), fake=fake) == 0
    assert 'coherent signal in the mean of 10 packets: none' in capsys.readouterr().out
    coherent = _load_probe(tmp_path)['steps']['STEP 10']['data']['analysis']['coherent']
    assert coherent['found'] is False


def test_step_10b_reports_the_time_per_packet_with_and_without_averaging(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert hw_probe.main(_c_args(tmp_path, '--gain', '40'), fake=_c_fake()) == 0
    report = _load_probe(tmp_path)['steps']
    plain = report['STEP 10']['data']['analysis']['time_per_packet_s']
    averaged = report['STEP 10b']['data']['time_per_packet_s']
    assert plain > 0
    assert averaged > 0
    assert report['STEP 10b']['data']['time_ratio'] == pytest.approx(averaged / plain)
    assert 'time per packet (pulser-on time / packets)' in capsys.readouterr().out


def test_mem_clear_is_off_by_default(tmp_path: Path) -> None:
    fake = _c_fake()
    assert hw_probe.main(_c_args(tmp_path), fake=fake) == 0
    assert not [c for c in fake.log if c.startswith('MEM:')]


def test_mem_clear_goes_right_before_every_star_auto_of_the_pulsed_acquisitions(
    tmp_path: Path,
) -> None:
    fake = _c_fake()
    assert hw_probe.main(_c_args(tmp_path, '--mem-clear'), fake=fake) == 0
    log = fake.log
    star = [i for i, c in enumerate(log) if c == 'STAR AUTO']
    clears = [i for i, c in enumerate(log) if c == 'MEM:CLEar']
    assert len(clears) == 1 + 5  # step 10: the baseline and one pulsed; 10b: 1; 11: 3
    assert all(log[i + 1] == 'STAR AUTO' for i in clears[1:])
    assert clears[0] < star[0]
    assert len([i for i in star if log[i - 1] == 'MEM:CLEar']) == 5


def test_mem_clear_is_in_the_dry_run_only_with_the_flag(capsys: pytest.CaptureFixture[str]) -> None:
    assert hw_probe.main(['--dry-run', '--phase', 'C', '--pulse-v', '20']) == 0
    assert 'MEM:CLEar' not in capsys.readouterr().out
    assert hw_probe.main(['--dry-run', '--phase', 'C', '--pulse-v', '20', '--mem-clear']) == 0
    assert capsys.readouterr().out.count('MEM:CLEar') == 6


def test_the_frame_statistics_of_every_pulsed_acquisition_are_in_the_json(
    tmp_path: Path,
) -> None:
    assert hw_probe.main(_c_args(tmp_path), fake=_c_fake()) == 0
    steps = _load_probe(tmp_path)['steps']
    assert steps['STEP 10']['data']['frame_stats'] == [{'dropped_bytes': 0, 'resyncs': 0}]
    assert steps['STEP 10b']['data']['frame_stats'] == [{'dropped_bytes': 0, 'resyncs': 0}]
    assert len(steps['STEP 11']['data']['frame_stats']) == 3
