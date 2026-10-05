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

from a1580_openhtf.fake_resource import FakeA1580Resource
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
    assert snapshot['idn'] == 'ACS-Solutions GmbH,A1580-HF,100500,1.6.b41'
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
    assert report['steps']['STEP 7']['data']['ascan_counts'] == [4]
    assert report['steps']['STEP 5']['data']['depth'] == 25
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
    assert '`ACS-Solutions GmbH,A1580-HF,100500,1.6.b41`' in out
    assert 'TRIG:INT?' in out
    assert '`10.0E-3`' in out
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
