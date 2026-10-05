#!/usr/bin/env python3
"""Instrument for the first session with the real A1580 (HARDWARE-SESSION.md).

Phase A is read-only (experiments 1 to 5, the only writes are the deliberate bad headers of
step 5). Phase B (experiments 6 to 9) switches the pulser off first, then changes a few
acquisition settings and streams A-scans. Phase C (experiments 10, 10b and 11) is the only
one that switches the pulser ON, at `--pulse-v`, with transducers connected; phase RST
(experiment 13) sends `*RST` once. Experiment 12 is deferred.

Safety, enforced in code and not only by convention:

- Every SCPI string goes through `GuardedResource`. By default it refuses `TRAN:PULS` writes,
  `*RST`, `TRAN:ENAB ON`, anything that mentions `/config` and anything that chains a second
  command (`;`, a line break). Phase C opens two gates: a `TRAN:PULS` write that equals
  `--pulse-v` (which is `<= --max-pulse-v`) and, only inside `pulsed_acquire`, `TRAN:ENAB ON`.
  Phase RST opens `*RST`. Every such command is printed before it is sent.
- The pulser is on only inside `pulsed_acquire`: the `finally` of that function sends
  `TRAN:ENAB OFF`, `STOP`, closes the data socket and verifies the pulser off by read-back; a
  read-back that is not off aborts the run (`EXIT_PULSER`).
- After the snapshot (step 2) the run stops if the device's `TRAN:PULS` is above
  `--max-pulse-v` or cannot be read.
- Everything runs inside `try: ... finally: plug.tearDown()`. Phases B, AB, C and RST restore
  the snapshot there (`TRAN:PULS` is never part of it); phase A changes no settings and
  restores nothing (the pulser is only switched off). Then the state headers are queried
  again and diffed against the snapshot (protocol step 14).

Usage:
    A1580_HOST=... tools/hw_probe.py --dry-run          # command list, no connection
    A1580_HOST=... tools/hw_probe.py --phase A
    A1580_HOST=... tools/hw_probe.py --phase C --pulse-v 20
    A1580_HOST=... tools/hw_probe.py --phase RST --allow-rst
    tools/hw_probe.py --fake --phase AB --out /tmp/probe # against FakeA1580Resource
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import os
import re
import socket
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

import numpy as np

from a1580_openhtf.fake_resource import DEFAULTS, FakeA1580Resource
from a1580_openhtf.plug import STATE_HEADERS, A1580Plug, normalize_header, values_match
from a1580_openhtf.stream import HEADER_SIZE, FrameReader, parse_header

# ── what the tool sends (one place, used by the steps and by --dry-run) ──────

SCPI_PORT = 5025
ERR_QUERY = 'SYSTem:ERRor?'
BAD_HEADER = 'ZZZ:NOPE'  # step 5: undefined on purpose
BAD_COUNT = 25
ERR_READ_LIMIT = 60  # step 5 drain limit
STEP6_SETTINGS: dict[str, object] = {
    'DATA:LENG': 1024,
    'FREQ': '100 MHZ',
    'TRIG:MODE': 'INT',
    'TRIG:INT': '10 MS',
}
STEP7_SETTINGS: dict[str, object] = {'AVER:COUN': 4}
GAIN_STEP_DB = 6  # step 9 changes GAIN by this much (down instead when above 80 dB)
GAIN_MAX_DB = 80
ACQ_TIMEOUT_S = 10.0  # per acquisition (steps 6 and 7) and per read in steps 8 and 9
OBSERVE_S = 3.0  # how long steps 8 and 9 watch the data socket
FORBIDDEN_WRITES = frozenset({'TRAN:PULS', '*RST'})
UNSNAPSHOTTED = frozenset({'TRAN:PULS'})  # never written back by the restore

# phase C and phase RST (SPEC-phaseC.md)
PULSE_V_MIN = 5.0  # --pulse-v below this is a usage error
BASELINE_PACKETS = 5  # step 10: pulser off
STEP10_PACKETS = 10
STEP10B_PACKETS = 5
STEP10B_AVER = 4
STEP11_PACKETS = 5
STEP11_DELAYS = ('0 NS', '1000 US', '2000 US')  # TRIG:DEL values of step 11, first is the reference
SHIFT_TOLERANCE_SAMPLES = 2  # "by the delay" means within this many samples
ONSET_FACTOR = 10.0  # onset threshold: max(ONSET_FACTOR * noise std, ONSET_MIN_COUNTS)
ONSET_MIN_COUNTS = 20.0
COHERENT_FACTOR = 10.0  # coherent signal: peak of the mean > COHERENT_FACTOR * s / sqrt(n)
COHERENT_MIN_COUNTS = 1.0  # ... and at least this (the int16 resolution)
FLAT_TOP_RUN = 4  # this many equal extreme samples in a row: a flat top
TAIL_FRACTION = 0.05  # "the end of the record"
PRE_ONSET_MIN_SAMPLES = 50
# Step 13 moves these away from the snapshot before `*RST` (never TRAN:PULS or TRAN:ENAB).
RST_DETUNE: dict[str, object] = {
    'GAIN': 6,
    'TRIG:INT': '10 MS',
    'AVER:COUN': 2,
    'TRAN:FREQ': '50 KHZ',
    'FILT:HPAS:IND': 2,
    'TRIG:DEL': '0 NS',
}
SAMPLE_MAX = 32767
SAMPLE_MIN = -32768
PHASES = ('A', 'B', 'AB', 'C', 'RST')
PULSER_OFF_BEFORE = (6, 10, 13)  # the pulser-off check runs before the first of these steps
# Phase C settings are words that go into SCPI strings: letters, digits, blanks, `.`, `+`, `-`.
_SAFE_SETTING = re.compile(r'[A-Za-z0-9 .+\-]+')
# `DEFAULTS` entries of the fake that its source marks `UNKNOWN default`: a guess, not the vendor's
GUESSED_DEFAULTS = frozenset(
    {
        'MODE',
        'TRIG:MODE',
        'GAIN:PRE:COMB',
        'GAIN:PRE:SPLIT',
        'GAIN:TGC:LIN',
        'GAIN:TGC:ARB',
        'AVER:DEL:CONS:AUTO',
        'AVER:DEL:CONS',
        'FILT:HPAS:IND',
    }
)
EXPECTED_LENG_RANGE = (1024, 36864)  # the setter's range; the power-on value 114688 is outside

EXIT_FAILED_STEP = 1
EXIT_USAGE = 2
EXIT_CEILING = 3
EXIT_PULSER = 4


class AbortRun(Exception):
    """Stop the whole run (after tearDown and the final diff)."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


class PulserCheckError(AbortRun):
    """The pulser could not be switched off and confirmed off by read-back."""

    def __init__(self, message: str) -> None:
        super().__init__(EXIT_PULSER, message)


# ── guards and recorders ─────────────────────────────────────────────────────


class ForbiddenCommand(RuntimeError):
    """Raised instead of sending a command this tool must never send."""


@dataclass(frozen=True)
class GuardPolicy:
    """What the guard opens beyond its defaults: phase C (voltage, pulser), phase RST (`*RST`)."""

    phase: str = 'A'
    pulse_v: float | None = None
    max_pulse_v: float = 20.0


_ON_WORDS = ('ON', '1')
_OFF_WORDS = ('OFF', '0')


def _write_arg(cmd: str) -> tuple[str, str]:
    """`(head, argument)` of a command line; any whitespace separates them."""
    parts = cmd.strip().split(None, 1)
    if not parts:
        return '', ''
    return parts[0], (parts[1].strip() if len(parts) > 1 else '')


def _pulse_write_allowed(arg: str, policy: GuardPolicy) -> bool:
    """A `TRAN:PULS` write is let through only in phase C, as `<number> [V]` equal to --pulse-v."""
    if policy.phase != 'C' or policy.pulse_v is None:
        return False
    match = re.fullmatch(r'([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*(?:[vV])?', arg)
    if match is None:
        return False
    volts = float(match.group(1))
    return volts <= policy.max_pulse_v and abs(volts - policy.pulse_v) <= 1e-9


def check_allowed(
    cmd: str,
    *,
    is_write: bool,
    policy: GuardPolicy | None = None,
    enab_on_open: bool = False,
) -> None:
    """Raise ForbiddenCommand for what this tool must not send, see the module docstring.

    Always refused: `/config`, a second command chained with `;` or a line break, and (writes)
    `TRAN:ENAB` with anything but OFF/0 unless phase C has the gate open (`enab_on_open`, set
    by `GuardedResource.pulser_gate` and only inside `pulsed_acquire`), `TRAN:PULS` unless phase
    C and the value equals --pulse-v (which is <= --max-pulse-v), `*RST` unless phase RST.
    """
    policy = policy if policy is not None else GuardPolicy()
    if '/config' in cmd.lower():
        raise ForbiddenCommand(f'refusing to send {cmd!r}: /config is never touched')
    if ';' in cmd or '\n' in cmd or '\r' in cmd:
        raise ForbiddenCommand(f'refusing to send {cmd!r}: one command per string')
    head, arg = _write_arg(cmd)
    try:
        normal = normalize_header(head)
    except ValueError:
        return  # unknown (like the deliberate ZZZ:NOPE of step 5): none of the forbidden ones
    if not is_write:
        return
    if normal == 'TRAN:ENAB' and arg.upper() not in _OFF_WORDS:
        if not (policy.phase == 'C' and enab_on_open and arg.upper() in _ON_WORDS):
            raise ForbiddenCommand(
                f'refusing to send {cmd!r}: only pulsed_acquire in phase C switches the pulser on'
            )
    elif normal == 'TRAN:PULS':
        if not _pulse_write_allowed(arg, policy):
            raise ForbiddenCommand(f'refusing to send {cmd!r}')
    elif normal == '*RST' and policy.phase != 'RST':
        raise ForbiddenCommand(f'refusing to send {cmd!r}')


class GuardedResource:
    """Wrap the SCPI resource: log every string, refuse the forbidden ones, defer `close`.

    `close()` does nothing so that the plug's `tearDown` leaves the session usable for the
    final read-back; `shutdown()` really closes it. `pulser_gate()` is the only way to let a
    `TRAN:ENAB ON` through (phase C). `set_announce(f)` makes the guard call `f(cmd)` for every
    `TRAN:PULS` write, `TRAN:ENAB ON` and `*RST` that it lets through, before it is sent.
    """

    def __init__(
        self,
        inner: Any,
        closer: Callable[[], None] | None = None,
        policy: GuardPolicy | None = None,
    ) -> None:
        object.__setattr__(self, '_inner', inner)
        object.__setattr__(self, '_closer', closer)
        object.__setattr__(self, '_policy', policy if policy is not None else GuardPolicy())
        object.__setattr__(self, '_gate', {'enab_on': False})
        object.__setattr__(self, '_announce', None)
        object.__setattr__(self, 'sent', [])
        object.__setattr__(self, 'replies', {})  # last reply per query string, exactly as received

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._inner, name, value)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def set_announce(self, announce: Callable[[str], None] | None) -> None:
        object.__setattr__(self, '_announce', announce)

    @contextlib.contextmanager
    def pulser_gate(self) -> Iterator[None]:
        """Let `TRAN:ENAB ON` through (phase C only) while the `with` block runs."""
        self._gate['enab_on'] = True
        try:
            yield
        finally:
            self._gate['enab_on'] = False

    def write(self, cmd: str) -> None:
        check_allowed(cmd, is_write=True, policy=self._policy, enab_on_open=self._gate['enab_on'])
        head, arg = _write_arg(cmd)
        try:
            normal = normalize_header(head)
        except ValueError:
            normal = ''
        if self._announce is not None and (
            normal in ('TRAN:PULS', '*RST') or (normal == 'TRAN:ENAB' and arg.upper() in _ON_WORDS)
        ):
            self._announce(cmd)
        self.sent.append(cmd)
        self._inner.write(cmd)

    def query(self, cmd: str) -> Any:
        # a "query" without the question mark is really a write and is checked as one (the
        # pulser gate never opens for it)
        check_allowed(cmd, is_write=not cmd.strip().endswith('?'), policy=self._policy)
        self.sent.append(cmd)
        reply = self._inner.query(cmd)
        self.replies[cmd] = reply
        return reply

    def close(self) -> None:
        """Deliberately a no-op, see the class docstring."""

    def shutdown(self) -> None:
        try:
            self._inner.close()
        finally:
            if self._closer is not None:
                self._closer()


class RecordingSocket:
    """Data socket wrapper that timestamps every `recv` (chunk sizes, arrival times)."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.t_open = time.monotonic()
        self.chunks: list[tuple[float, int]] = []
        self.timeouts = 0
        self.eof_at: float | None = None

    def settimeout(self, value: float | None) -> None:
        self._inner.settimeout(value)

    def recv(self, n: int) -> bytes:
        try:
            data: bytes = self._inner.recv(n)
        except TimeoutError:
            self.timeouts += 1
            raise
        now = time.monotonic()
        if data:
            self.chunks.append((now, len(data)))
        else:
            self.eof_at = now
        return data

    def shutdown(self, how: int) -> None:
        self._inner.shutdown(how)

    def close(self) -> None:
        self._inner.close()


class RecordingFactory:
    """Data socket factory that wraps every socket it opens in a `RecordingSocket`."""

    def __init__(self, inner: Callable[[str, int], Any]) -> None:
        self._inner = inner
        self.sockets: list[RecordingSocket] = []

    def __call__(self, host: str, port: int) -> RecordingSocket:
        sock = RecordingSocket(self._inner(host, port))
        self.sockets.append(sock)
        return sock


def packet_times(sock: RecordingSocket, psize: int, n: int) -> list[float]:
    """Arrival time (monotonic) of each of the first `n` packets: the recv that completed it.

    Assumes no garbage between packets; a resync would shift the estimate.
    """
    times: list[float] = []
    total = 0
    for t, size in sock.chunks:
        total += size
        while len(times) < n and total >= (len(times) + 1) * psize:
            times.append(t)
    return times


# ── report plumbing ──────────────────────────────────────────────────────────


def bt(text: object) -> str:
    """An instrument reply, verbatim, between backticks."""
    return f'`{text}`'


def _shape(reply: str) -> str:
    if not reply:
        return 'empty'
    if re.fullmatch(r'[+-]?\d+', reply):
        return 'integer'
    if re.fullmatch(r'[+-]?(\d+\.?\d*|\.\d+)[eE][+-]?\d+', reply):
        return 'exponent notation'
    if re.fullmatch(r'[+-]?(\d+\.\d*|\.\d+)', reply):
        return 'decimal'
    if ',' in reply:
        return 'list'
    if reply.isupper():
        return 'upper-case word'
    if reply.islower():
        return 'lower-case word'
    return 'mixed-case word'


class StepRecord:
    """One block of the report."""

    def __init__(self, label: str, title: str) -> None:
        self.label = label
        self.title = title
        self.status = 'ok'
        self.error: str | None = None
        self.lines: list[str] = []
        self.data: dict[str, Any] = {}

    def as_json(self) -> dict[str, Any]:
        return {
            'title': self.title,
            'status': self.status,
            'error': self.error,
            'lines': self.lines,
            'data': self.data,
        }


class Probe:
    """State of one run: the plug, the guarded resource, the report and the snapshot."""

    def __init__(
        self,
        plug: A1580Plug,
        resource: GuardedResource,
        data_factory: RecordingFactory,
        args: argparse.Namespace,
        host: str,
        stamp: str,
        fake: bool,
    ) -> None:
        self.plug = plug
        self.resource = resource
        self._data_factory = data_factory
        self.args = args
        self.host = host
        self.stamp = stamp
        self.fake = fake
        self.out_dir = Path(args.out)
        self.records: list[StepRecord] = []
        self.current = StepRecord('-', '-')
        self.idn_reply = ''
        self.state: dict[str, str] = {}
        self.answered: dict[str, str] = {}
        self.timing_s: dict[str, float] = {}
        self.acq: dict[str, dict[str, Any]] = {}
        self.sockets = data_factory.sockets
        self.serial = _clean(plug.identity.serial)
        # phase C and RST
        self.warnings: list[str] = []
        self.pulses: list[dict[str, Any]] = []
        self.baseline: np.ndarray | None = None  # step 10, pulser off, int16 (packets, samples)
        self.pulsed10: np.ndarray | None = None
        self.step10: dict[str, Any] | None = None
        self.pulse_written = False  # phase C changed TRAN:PULS (it is never written back)
        self.expected_diffs: dict[str, str] = {}  # header -> why a final-diff change is expected
        resource.set_announce(self._announce)

    # report ---------------------------------------------------------------

    def say(self, line: str = '') -> None:
        print(line, flush=True)
        self.current.lines.append(line)

    def warn(self, text: str) -> None:
        """A measurement warning: printed now, repeated in WARNINGS, stored in the JSON."""
        self.say(f'WARNING: {text}')
        self.warnings.append(text)
        self.current.data.setdefault('warnings', []).append(text)

    def _announce(self, cmd: str) -> None:
        """Called by the guard before it sends a `TRAN:PULS` write, `TRAN:ENAB ON` or `*RST`."""
        kind = 'RESET' if cmd.strip().upper().startswith('*RST') else 'PULSER'
        self.say(f'{kind}: about to send {cmd}')

    def run_block(self, label: str, title: str, func: Callable[[Probe], None]) -> None:
        """Run one step: a failing step prints `<label> FAILED: ...` and the run continues.

        `AbortRun` (failed pulser-off check, pulser ceiling) is recorded and re-raised.
        """
        rec = StepRecord(label, title)
        self.records.append(rec)
        self.current = rec
        self.say('')
        self.say(f'{label}: {title}')
        try:
            func(self)
        except AbortRun as exc:
            rec.status = 'aborted'
            rec.error = str(exc)
            self.say(f'{label} ABORTED: {exc}')
            raise
        except Exception as exc:  # noqa: BLE001 - one failing step must not end the session
            rec.status = 'failed'
            rec.error = f'{type(exc).__name__}: {exc}'
            self.say(f'{label} FAILED: {exc if str(exc) else repr(exc)}')

    # instrument -----------------------------------------------------------

    def try_query(self, cmd: str) -> tuple[str | None, str | None, float]:
        """`(reply, error, seconds)`; an unanswered query is data here, not an exception."""
        t0 = time.perf_counter()
        try:
            reply: str | None = self.plug.query(cmd)
            error = None
        except Exception as exc:  # noqa: BLE001 - recording which headers do not answer
            reply, error = None, f'{type(exc).__name__}: {exc}'
        return reply, error, time.perf_counter() - t0

    def data_socket(self, port: int) -> RecordingSocket:
        return self._data_factory(self.host, port)

    def drain_errors(self, limit: int) -> tuple[list[str], bool]:
        """Read `SYST:ERR?` until the number is 0 or `limit` reads; `(entries, hit_limit)`."""
        entries: list[str] = []
        for _ in range(limit):
            reply = self.plug.query(ERR_QUERY)
            number = reply.split(',', 1)[0].strip()
            if number == '0':
                return entries, False
            entries.append(reply)
        return entries, True


def _clean(text: str) -> str:
    return re.sub(r'[^A-Za-z0-9_.-]', '_', text) or 'unknown'


def packet_stats(packet: bytes) -> dict[str, Any]:
    """Header fields and raw min/max/std of one packet."""
    header = parse_header(packet)
    raw = np.frombuffer(packet, dtype='<i2', offset=HEADER_SIZE)
    fields = header._asdict()
    fields['magic'] = header.magic.decode('latin-1')
    fields['ctp'] = list(header.ctp)
    fields.update(min=int(raw.min()), max=int(raw.max()), std=float(raw.std()), n=int(raw.size))
    return fields


# ── Phase A ──────────────────────────────────────────────────────────────────


def step_1(p: Probe) -> None:
    """Experiment 1: `*IDN?`, `SYST:VERS?`, `SYST:ERR:COUN?`. Record firmware."""
    data: dict[str, Any] = {}
    for key, cmd in (
        ('idn', '*IDN?'),
        ('syst_vers', 'SYST:VERS?'),
        ('err_count', 'SYST:ERR:COUN?'),
    ):
        reply, error, dt = p.try_query(cmd)
        data[key] = reply if error is None else f'<ERROR {error}>'
        if error is None:
            p.say(f'{cmd} -> {bt(reply)} ({dt * 1000:.1f} ms)')
        else:
            p.say(f'{cmd} -> NO ANSWER: {error} ({dt:.2f} s)')
    p.idn_reply = data['idn']
    p.say(f'firmware (4th *IDN? field): {bt(p.plug.identity.firmware)}')
    p.say(f'model {bt(p.plug.identity.model)}, serial {bt(p.plug.identity.serial)}')
    p.current.data.update(data, firmware=p.plug.identity.firmware)


def step_2(p: Probe) -> None:
    """Experiment 2: query every STATE_HEADERS entry; save first, change second.

    Records which headers answer, their exact reply format and the time each query took,
    then writes the snapshot to `<out>/<serial>-<YYYYMMDD-HHMMSS>.json`.
    """
    unanswered: list[str] = []
    for header in STATE_HEADERS:
        reply, error, dt = p.try_query(f'{header}?')
        p.timing_s[header] = round(dt, 6)
        if error is None and reply is not None:
            p.state[header] = reply
            p.answered[header] = reply
            p.say(f'{header + "?":22s} -> {bt(reply)}  [{_shape(reply)}, {dt * 1000:.1f} ms]')
        else:
            p.state[header] = f'<ERROR {error}>'
            unanswered.append(header)
            p.say(f'{header + "?":22s} -> NO ANSWER: {error} ({dt:.2f} s)')
    p.out_dir.mkdir(parents=True, exist_ok=True)
    path = p.out_dir / f'{p.serial}-{p.stamp}.json'
    snapshot = {'idn': p.idn_reply, 'state': p.state, 'timing_s': p.timing_s}
    path.write_text(json.dumps(snapshot, indent=2) + '\n', encoding='utf-8')
    p.say(f'answered {len(p.answered)} of {len(STATE_HEADERS)} headers; snapshot saved to {path}')
    if unanswered:
        p.say(f'WARNING: no answer from {", ".join(unanswered)}; the tool never writes them back')
    if p.answered.get('TRAN:ENAB', 'OFF').strip().upper() not in ('OFF', '0'):
        p.say(
            f'WARNING: the pulser was ON as found (TRAN:ENAB {bt(p.answered["TRAN:ENAB"])}); '
            'the tool will leave it OFF, so the final diff will show TRAN:ENAB changed'
        )
    p.current.data.update(
        snapshot_path=str(path), unanswered=unanswered, state=p.state, timing_s=p.timing_s
    )


def step_3(p: Probe) -> None:
    """Experiment 3: `DATA:PORT?` (unknown 1).

    Does the reply change between sessions or after `*RST`?

    Within this session it is queried twice; across sessions it is compared with the last
    earlier probe file in the output directory. `*RST` is never sent by this tool, so that
    part stays open.
    """
    replies: list[str | None] = []
    for _ in range(2):
        reply, error, _dt = p.try_query('DATA:PORT?')
        replies.append(reply)
        p.say(f'DATA:PORT? -> {bt(reply) if error is None else "NO ANSWER: " + error}')
    stable = replies[0] == replies[1]
    p.say(f'within this session: {"unchanged" if stable else "CHANGED between the two queries"}')
    previous = _previous_port(p)
    if previous is None:
        p.say('across sessions: no earlier probe file for this serial, nothing to compare')
    else:
        verdict = 'unchanged' if previous == replies[0] else 'CHANGED'
        p.say(f'across sessions: earlier probe saw {bt(previous)}, now {bt(replies[0])}: {verdict}')
    p.say('after *RST: NOT TESTED (this tool never sends *RST)')
    p.current.data.update(data_port=replies[0], stable_in_session=stable, earlier_port=previous)


def _previous_port(p: Probe) -> str | None:
    me = f'probe-{p.serial}-{p.stamp}.json'
    for path in sorted(p.out_dir.glob(f'probe-{p.serial}-*.json'), reverse=True):
        if path.name == me:
            continue
        try:
            doc = json.loads(path.read_text(encoding='utf-8'))
            port = doc['steps']['STEP 3']['data']['data_port']
        except (OSError, ValueError, KeyError, TypeError):
            continue
        return str(port) if port is not None else None
    return None


def step_4(p: Probe) -> None:
    r"""Experiment 4: does a bare `\n` terminator work (unknown 13)?

    One harmless query (`*IDN?`) over a raw socket to port 5025, `\n` only. It runs last,
    after the main connection is closed (`run`), so it is the only connection to the device.
    With `--fake` this step prints `SKIPPED (fake)`: the fake has no TCP port and the real
    question is about the device's parser.
    """
    if p.fake:
        p.say('SKIPPED (fake)')
        p.current.data['skipped'] = True
        return
    try:
        sock = socket.create_connection((p.host, SCPI_PORT), timeout=5)
    except OSError as exc:
        p.say(f'second connection to port {SCPI_PORT} failed: {exc}')
        p.say('the device may accept one SCPI client at a time (unknown), nothing was sent')
        p.current.data.update(connected=False, error=str(exc))
        return
    with sock:
        sock.sendall(b'*IDN?\n')
        sock.settimeout(5)
        buf = b''
        try:
            while not buf.endswith(b'\n'):
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
        except TimeoutError:
            pass
    if buf:
        p.say(f'bare \\n terminator: reply {bt(buf.decode("iso-8859-1").rstrip())}')
        p.say(f'raw bytes {bt(repr(buf))}')
        p.current.data.update(connected=True, reply=buf.decode('iso-8859-1'))
    else:
        p.say('bare \\n terminator: NO REPLY within 5 s on the second socket')
        p.say('either the device needs \\r\\n, or it serves only one client at a time')
        p.current.data.update(connected=True, reply=None)


def step_5(p: Probe) -> None:
    """Experiment 5: error queue.

    One undefined header, `SYST:ERR?` twice; depth with 25 bad headers, each one followed by
    `SYST:ERR:COUN?` so that the device's input buffer (a few hundred bytes, measured
    2026-10-05, fw 1.16) is never overrun by a burst of writes.

    The queue is drained first. These are the only deliberate writes of Phase A.
    """
    before, hit = p.drain_errors(ERR_READ_LIMIT)
    p.say(f'queue before the test: {len(before)} entries{" (limit hit)" if hit else ""}')
    for entry in before:
        p.say(f'  {bt(entry)}')
    p.plug.write(f'{BAD_HEADER} 1')
    first = p.plug.query(ERR_QUERY)
    second = p.plug.query(ERR_QUERY)
    p.say(f'after one undefined header: first {ERR_QUERY} -> {bt(first)}')
    p.say(f'                           second {ERR_QUERY} -> {bt(second)}')
    counts: list[str | None] = []
    count_error: str | None = None
    for i in range(1, BAD_COUNT + 1):
        p.plug.write(f'{BAD_HEADER}{i:02d}')
        reply, count_error, _dt = p.try_query('SYST:ERR:COUN?')
        counts.append(reply)
        if count_error is not None:
            p.say(f'SYST:ERR:COUN? after bad header {i}: NO ANSWER: {count_error}')
            break
    count_reply = counts[-1] if counts else None
    p.say(f'SYST:ERR:COUN? after each bad header: {counts}')
    entries, hit = p.drain_errors(ERR_READ_LIMIT)
    overflow = bool(entries) and entries[-1].split(',', 1)[0].strip() == '-350'
    p.say(f'drained {len(entries)} entries{" (read limit hit, depth is larger)" if hit else ""}')
    if entries:
        p.say(f'  first {bt(entries[0])}')
        p.say(f'  last  {bt(entries[-1])}')
    p.say(f'depth {len(entries)}; last is -350 (queue overflow): {"yes" if overflow else "no"}')
    p.current.data.update(
        before=before,
        first=first,
        second=second,
        count_reply=count_reply,
        counts=counts,
        depth=len(entries),
        last_is_overflow=overflow,
        depth_limit_hit=hit,
        first_entry=entries[0] if entries else None,
        last_entry=entries[-1] if entries else None,
    )


# ── Phase B ──────────────────────────────────────────────────────────────────


def pulser_off_check(p: Probe) -> None:
    """Experiment 6, precondition: `TRAN:ENAB OFF` (checked), verified off by read-back.

    Anything but a confirmed OFF raises PulserCheckError, which aborts the whole run.
    """
    try:
        stale = p.plug.check_errors()  # earlier steps may have left entries, not this write's
        if stale:
            p.say(f'error queue drained before the write: {stale}')
        p.plug.write_checked('TRAN:ENAB OFF')
        reply = p.plug.query('TRAN:ENAB?')
    except Exception as exc:  # noqa: BLE001 - any failure here means the pulser state is unknown
        raise PulserCheckError(f'could not switch the pulser off: {exc}') from exc
    p.say(f'TRAN:ENAB? -> {bt(reply)}')
    if not values_match('TRAN:ENAB', 'OFF', reply):
        raise PulserCheckError(f'TRAN:ENAB? answered {reply!r} after TRAN:ENAB OFF')
    p.say('pulser confirmed OFF by read-back')


def _acquire_block(p: Probe, key: str, settings: dict[str, object]) -> dict[str, Any]:
    """Apply `settings`, acquire `--packets` A-scans and report each packet; returns a summary."""
    n = int(p.args.packets)
    stale = p.plug.check_errors()  # so that no old entry is blamed on the first setting
    if stale:
        p.say(f'error queue drained before the setup: {stale}')
    p.say(f'apply_setup {settings}')
    p.plug.apply_setup(settings)
    p.say(f'settings read back OK; AVER:COUN as found {bt(p.answered.get("AVER:COUN", "?"))}')
    readbacks = {key: p.resource.replies.get(f'{key}?') for key in settings}
    for key, value in settings.items():
        p.say(f'{key} {value} -> {key}? -> {bt(readbacks[key])}')
    first_socket = len(p.sockets)
    t0 = time.monotonic()
    scans = p.plug.acquire(n, timeout_s=ACQ_TIMEOUT_S)
    wall = time.monotonic() - t0
    sock = p.sockets[first_socket]
    psize = HEADER_SIZE + 2 * len(scans[0].raw)
    arrivals = packet_times(sock, psize, n)
    intervals = [b - a for a, b in zip(arrivals, arrivals[1:], strict=False)]
    packets: list[dict[str, Any]] = []
    for i, scan in enumerate(scans):
        header = scan.header
        raw = scan.raw
        entry: dict[str, Any] = {
            'packet_number': header.packet_number,
            'length_lo': header.length_lo,
            'length_hi': header.length_hi,
            'ascan_count': header.ascan_count,
            'buffer_fill': header.buffer_fill,
            'is_full': header.is_full,
            'telemetry': [header.telemetry_a, header.telemetry_b, header.telemetry_c],
            'min': int(raw.min()),
            'max': int(raw.max()),
            'std': float(raw.std()),
            'dt_s': intervals[i - 1] if i >= 1 and i - 1 < len(intervals) else None,
        }
        packets.append(entry)
        dt = entry['dt_s']
        p.say(
            f'packet {i}: packet_number={entry["packet_number"]} '
            f'length_lo/hi={entry["length_lo"]}/{entry["length_hi"]} '
            f'ascan_count={entry["ascan_count"]} buffer_fill={entry["buffer_fill"]} '
            f'is_full={entry["is_full"]} min={entry["min"]} max={entry["max"]} '
            f'std={entry["std"]:.2f} interval={"-" if dt is None else f"{dt * 1000:.1f} ms"}'
        )
    numbers = [e['packet_number'] for e in packets]
    steps_seen = sorted({(b - a) % 256 for a, b in zip(numbers, numbers[1:], strict=False)})
    sizes = [size for _t, size in sock.chunks]
    mean_dt = float(np.mean(intervals)) if intervals else None
    summary = {
        'n': len(scans),
        'wall_s': wall,
        'first_packet_after_open_s': (arrivals[0] - sock.t_open) if arrivals else None,
        'mean_interval_s': mean_dt,
        'packet_rate_hz': (1.0 / mean_dt) if mean_dt else None,
        'packet_number_steps': steps_seen,
        'recv_chunks': len(sizes),
        'recv_chunk_min': min(sizes) if sizes else None,
        'recv_chunk_max': max(sizes) if sizes else None,
        'recv_chunk_sizes_first': sizes[:20],
        'std_mean': float(np.mean([e['std'] for e in packets])),
        'ascan_counts': sorted({e['ascan_count'] for e in packets}),
        'readbacks': readbacks,
        'packets': packets,
    }
    rate = summary['packet_rate_hz']
    p.say(
        f'{len(scans)} packets in {wall:.2f} s wall (incl. setup queries); '
        f'mean interval {"-" if mean_dt is None else f"{mean_dt * 1000:.1f} ms"}, '
        f'rate {"-" if rate is None else f"{rate:.1f} Hz"}'
    )
    p.say(f'packet_number increments (mod 256): {steps_seen}')
    p.say(
        f'recv: {len(sizes)} chunks, sizes min/max {summary["recv_chunk_min"]}/'
        f'{summary["recv_chunk_max"]} bytes (packet is {psize} bytes), '
        f'first {sizes[:8]}'
    )
    p.say(
        f'ascan_count values: {summary["ascan_counts"]}; mean noise std {summary["std_mean"]:.2f}'
    )
    p.acq[key] = summary
    return summary


def step_6(p: Probe) -> None:
    """Experiment 6: `DATA:LENG 1024`, `FREQ 100 MHZ`, `TRIG:MODE INT`, `TRIG:INT 10 MS`.

    Opens the data socket, `STAR AUTO`, reads `--packets` packets, `STOP` (all inside
    `plug.acquire`). Records packet rate, recv chunk sizes, the header fields and the noise
    floor in counts. The pulser was switched off by the pulser-off check before this step.
    """
    summary = _acquire_block(p, '6', STEP6_SETTINGS)
    p.current.data.update(summary)


def step_7(p: Probe) -> None:
    """Experiment 7: repeat with `AVER:COUN 4`.

    Does `ascan_count` change, does the noise amplitude change like a mean or like a sum
    (unknown 3)? Only the ratio is reported; deciding is left to the owner.
    """
    summary = _acquire_block(p, '7', STEP7_SETTINGS)
    base = p.acq.get('6')
    if base is None or not base['std_mean']:
        p.say('no usable step 6 baseline, no ratio')
    else:
        ratio = summary['std_mean'] / base['std_mean']
        summary['std_ratio_vs_step6'] = ratio
        p.say(
            f'std(AVER:COUN 4) / std(step 6) = {ratio:.3f}  '
            '(1.0 unchanged, 0.5 mean of 4 uncorrelated, 2.0 sum of 4)'
        )
        p.say(f'ascan_count: step 6 {base["ascan_counts"]}, step 7 {summary["ascan_counts"]}')
    p.current.data.update(summary)


def _start_stream(p: Probe) -> tuple[RecordingSocket, FrameReader, int]:
    """Lower-level start used by steps 8 and 9: query geometry, connect, `STAR AUTO`."""
    length = int(p.plug.query('DATA:LENG?'))
    port = int(p.plug.query('DATA:PORT?'))
    sock = p.data_socket(port)
    reader = FrameReader(sock, length)
    p.plug.write('STAR AUTO')
    return sock, reader, HEADER_SIZE + 2 * length


def _stop_and_close(p: Probe, sock: RecordingSocket | None) -> None:
    try:
        p.plug.write('STOP')
    finally:
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except (OSError, AttributeError):
                pass
            try:
                sock.close()
            except OSError:
                pass


def observe(
    sock: RecordingSocket, window_s: float, *, stop_on_data: bool = False
) -> dict[str, Any]:
    """Watch `sock` for `window_s`: bytes received, when, and whether the peer closed it."""
    t0 = time.monotonic()
    deadline = t0 + window_s
    total = 0
    first_at: float | None = None
    last_at: float | None = None
    first_bytes = b''
    closed: str | None = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        sock.settimeout(remaining)
        try:
            chunk = sock.recv(65536)
        except TimeoutError:
            time.sleep(0)
            continue
        except OSError as exc:
            closed = f'error {type(exc).__name__}: {exc} after {time.monotonic() - t0:.2f} s'
            break
        if not chunk:
            closed = f'closed by the peer after {time.monotonic() - t0:.2f} s'
            break
        now = time.monotonic() - t0
        if first_at is None:
            first_at, first_bytes = now, chunk[:8]
        last_at = now
        total += len(chunk)
        if stop_on_data:
            break
    return {
        'window_s': window_s,
        'bytes': total,
        'first_data_after_s': first_at,
        'last_data_after_s': last_at,
        'first_bytes': first_bytes.decode('latin-1'),
        'closed': closed,
    }


def _describe_after_stop(obs: dict[str, Any], psize: int) -> str:
    window = obs['window_s']
    total = obs['bytes']
    if obs['closed']:
        tail = f' (after {total} trailing bytes)' if total else ''
        return f'the device closed the socket: {obs["closed"]}{tail}'
    if total == 0:
        return f'idle: no data and no close within {window:g} s, the socket stayed open'
    packets = total / psize
    if obs['last_data_after_s'] is not None and obs['last_data_after_s'] > window * 0.8:
        return (
            f'data kept arriving until the end of the {window:g} s window '
            f'({total} bytes, about {packets:.1f} packets): STOP did not end the stream'
        )
    return (
        f'{total} trailing bytes (about {packets:.1f} packets) within '
        f'{obs["last_data_after_s"]:.2f} s of STOP, then idle; the socket stayed open'
    )


def step_8(p: Probe) -> None:
    """Experiment 8: `STOP` while the socket is open; then connect after `STAR AUTO` (unknown 7).

    8a: does the device close the data socket on `STOP`, or keep it idle?
    8b: with `STAR AUTO` already sent, does data arrive on a socket opened afterwards?
    """
    sock: RecordingSocket | None = None
    try:
        sock, reader, psize = _start_stream(p)
        reader.read(2, ACQ_TIMEOUT_S)
        p.say('8a: data flowing (2 packets read); sending STOP with the socket still open')
        p.plug.write('STOP')
        obs = observe(sock, OBSERVE_S)
        text = _describe_after_stop(obs, psize)
        p.say(f'8a: {text}')
        p.current.data['8a'] = {**obs, 'verdict': text}
    finally:
        _stop_and_close(p, sock)
    errors = p.plug.check_errors()
    p.say(f'8a: error queue afterwards: {errors if errors else "empty"}')

    sock = None
    try:
        port = int(p.plug.query('DATA:PORT?'))
        p.plug.write('STAR AUTO')
        p.say('8b: STAR AUTO sent, now connecting the data socket')
        try:
            sock = p.data_socket(port)
        except OSError as exc:
            text = f'connection failed: {type(exc).__name__}: {exc}'
            p.say(f'8b: {text}')
            p.current.data['8b'] = {'verdict': text}
        else:
            obs = observe(sock, OBSERVE_S, stop_on_data=True)
            if obs['bytes']:
                magic = obs['first_bytes'][:4]
                text = (
                    f'data arrived: {obs["bytes"]} bytes in the first recv, '
                    f'{obs["first_data_after_s"]:.3f} s after connecting, starts with {bt(magic)}'
                )
            elif obs['closed']:
                text = f'no data, {obs["closed"]}'
            else:
                text = f'no data within {OBSERVE_S:g} s, the socket stayed open and idle'
            p.say(f'8b: {text}')
            p.current.data['8b'] = {**obs, 'verdict': text}
    finally:
        _stop_and_close(p, sock)
    errors = p.plug.check_errors()
    p.say(f'8b: error queue afterwards: {errors if errors else "empty"}')


def step_9(p: Probe) -> None:
    """Experiment 9: change `GAIN` during acquisition (unknown 7): accepted, error, or ignored?

    Reads three packets, writes `GAIN` (current value changed by 6 dB, inside 0 to 80 dB),
    drains the error queue, reads `GAIN?` back and keeps reading to see whether the
    stream continues and whether the amplitude follows.
    """
    old = float(p.plug.query('GAIN?'))
    new = old + GAIN_STEP_DB if old + GAIN_STEP_DB <= GAIN_MAX_DB else old - GAIN_STEP_DB
    new = max(0.0, new)
    command = f'GAIN {format(new, "g")}'
    sock: RecordingSocket | None = None
    try:
        sock, reader, _psize = _start_stream(p)
        before = [packet_stats(x) for x in reader.read(3, ACQ_TIMEOUT_S)]
        p.say(f'before: max per packet {[e["max"] for e in before]}')
        p.say(f'sending {command} (was {bt(old)}) while streaming')
        p.plug.write(command)
        errors = p.plug.check_errors()
        readback = p.plug.query('GAIN?')
        p.say(f'error queue after the write: {errors if errors else "empty"}')
        p.say(f'GAIN? -> {bt(readback)}')
        after: list[dict[str, Any]] = []
        stream = 'continued'
        try:
            after = [packet_stats(x) for x in reader.read(5, ACQ_TIMEOUT_S)]
        except (TimeoutError, ConnectionError) as exc:
            stream = f'stopped: {type(exc).__name__}: {exc}'
        p.say(f'after: max per packet {[e["max"] for e in after]}')
        p.say(f'stream {stream}')
    finally:
        _stop_and_close(p, sock)
    if errors:
        verdict = f'error: {errors}'
    elif values_match('GAIN', format(new, 'g'), readback):
        verdict = 'accepted: no error and the read-back shows the new value'
    elif values_match('GAIN', format(old, 'g'), readback):
        verdict = 'ignored: no error, read-back still shows the old value'
    else:
        verdict = f'unclear: read-back {readback!r} is neither {old:g} nor {new:g}'
    p.say(f'GAIN change during acquisition: {verdict}')
    p.current.data.update(
        old=old,
        new=new,
        readback=readback,
        errors=errors,
        stream=stream,
        max_before=[e['max'] for e in before],
        max_after=[e['max'] for e in after],
        verdict=verdict,
    )


# ── Phase C: pulser on (experiments 10, 10b, 11) ─────────────────────────────

TRIG_DEL_ZERO = '0 NS'


def phase_c_settings(args: argparse.Namespace) -> dict[str, object]:
    """Section 3.1 of SPEC-phaseC.md: what the preparation writes (in this order)."""
    return {
        'DATA:LENG': int(args.length),
        'FREQ': args.sample_freq,
        'TRIG:MODE': 'INT',
        'TRIG:INT': args.interval,
        'TRAN:TYPE': 'DUAL',
        'TRAN:FREQ': args.tran_freq,
        'TRAN:DUR': 1,
        'TRAN:IMP': 'HIGH',
        'GAIN': format(float(args.gain), 'g'),
        'GAIN:TGC:MODE': 'OFF',
        'AVER:COUN': 0,
        'FILT:HPAS:IND': 0,
        'TRIG:DEL': TRIG_DEL_ZERO,
    }


def _apply_report(p: Probe, settings: dict[str, object]) -> dict[str, str | None]:
    """`apply_setup` with a drain of the error queue before and a report of every read-back."""
    stale = p.plug.check_errors()  # so that no old entry is blamed on the first setting
    if stale:
        p.say(f'error queue drained before the setup: {stale}')
    p.say(f'apply_setup {settings}')
    p.plug.apply_setup(settings)
    readbacks = {key: p.resource.replies.get(f'{key}?') for key in settings}
    for key, value in settings.items():
        p.say(f'{key} {value} -> {key}? -> {bt(readbacks[key])}')
    return readbacks


@contextlib.contextmanager
def _put_back(p: Probe, settings: dict[str, object]) -> Iterator[None]:
    """Write `settings` again when the block ends, also after a failure (not after an abort)."""
    try:
        yield
    except AbortRun:
        raise  # the run stops and tearDown restores everything
    except Exception:
        try:
            _apply_report(p, settings)
        except Exception as exc:  # noqa: BLE001 - the original failure is the one to report
            p.say(f'putting back {settings} failed as well: {exc}')
        raise
    _apply_report(p, settings)


def _close_enough(a: float, b: float) -> bool:
    return abs(a - b) <= 1e-6


def _set_voltage(p: Probe) -> None:
    """Section 3.1: write `TRAN:PULS` only if it differs; abort unless it reads back equal."""
    wanted = float(p.args.pulse_v)
    try:
        reply = p.plug.query('TRAN:PULS?')
        volts = _parse_volts(reply)
        p.say(f'TRAN:PULS? -> {bt(reply)}; wanted --pulse-v {wanted:g} V')
        if volts is not None and _close_enough(volts, wanted):
            p.say('the pulser voltage is already right, TRAN:PULS is not written')
            return
        p.plug.write_checked(f'TRAN:PULS {format(wanted, "g")} V')  # announced by the guard
        p.pulse_written = True
        readback = p.plug.query('TRAN:PULS?')
        p.say(f'TRAN:PULS? -> {bt(readback)}')
        got = _parse_volts(readback)
    except Exception as exc:  # noqa: BLE001 - the voltage is not known to be right: stop
        raise AbortRun(
            EXIT_CEILING, f'could not set the pulser voltage to {wanted:g} V: {exc}'
        ) from exc
    if got is None or not _close_enough(got, wanted):
        raise AbortRun(EXIT_CEILING, f'TRAN:PULS? answered {readback!r} after writing {wanted:g} V')
    p.say('pulser voltage confirmed by read-back')


def prepare_phase_c(p: Probe) -> None:
    """Section 3.1. Any failure aborts the run: nothing is pulsed with unverified settings."""
    try:
        p.current.data['readbacks'] = _apply_report(p, phase_c_settings(p.args))
        _set_voltage(p)
    except AbortRun:
        raise
    except Exception as exc:  # noqa: BLE001 - see the docstring
        raise AbortRun(
            EXIT_FAILED_STEP, f'the preparation failed, nothing was pulsed: {exc}'
        ) from exc


class PulsedPacket(NamedTuple):
    raw: np.ndarray  # int16 samples
    header: Any  # stream header


def _stack(packets: Sequence[PulsedPacket]) -> np.ndarray:
    return np.vstack([x.raw for x in packets]).astype(np.int16)


def _make_pulser_safe(p: Probe, sock: RecordingSocket | None, t_on: float | None) -> None:
    """The `finally` of `pulsed_acquire`: OFF (unchecked), STOP, close, verify OFF by read-back.

    Every part is tried even if an earlier one failed. A read-back that is not OFF gets one
    more `TRAN:ENAB OFF` and a second read-back; if that is not OFF either, or cannot be read,
    PulserCheckError (the run aborts).
    """
    problems: list[str] = []
    try:
        p.plug.write('TRAN:ENAB OFF')
    except Exception as exc:  # noqa: BLE001 - go on: STOP, close, and the read-back decides
        problems.append(f'TRAN:ENAB OFF raised {type(exc).__name__}: {exc}')
    on_s = None if t_on is None else time.monotonic() - t_on
    try:
        _stop_and_close(p, sock)
    except Exception as exc:  # noqa: BLE001 - the pulser read-back below still has to happen
        problems.append(f'STOP raised {type(exc).__name__}: {exc}')

    def read_back() -> tuple[str | None, bool]:
        try:
            reply = p.plug.query('TRAN:ENAB?')
        except Exception as exc:  # noqa: BLE001 - an unreadable pulser state is not "off"
            problems.append(f'TRAN:ENAB? raised {type(exc).__name__}: {exc}')
            return None, False
        return reply, values_match('TRAN:ENAB', 'OFF', reply)

    reply, off = read_back()
    retried = False
    if not off:
        retried = True
        p.say(f'TRAN:ENAB? -> {bt(reply)}: NOT OFF, sending TRAN:ENAB OFF once more (retry)')
        try:
            p.plug.write('TRAN:ENAB OFF')
        except Exception as exc:  # noqa: BLE001 - the second read-back decides
            problems.append(f'retry TRAN:ENAB OFF raised {type(exc).__name__}: {exc}')
        reply, off = read_back()
    errors: list[str] = []
    try:
        errors = p.plug.check_errors()
    except Exception as exc:  # noqa: BLE001 - not worth hiding the pulser result
        problems.append(f'draining the error queue raised {type(exc).__name__}: {exc}')
    p.pulses.append(
        {
            'on_s': on_s,
            'verified_off': off,
            'retried_off': retried,
            'tran_enab_reply': reply,
            'device_errors': errors,
            'problems': problems,
        }
    )
    p.say(
        f'pulser was ON for {"-" if on_s is None else f"{on_s:.3f} s"}; '
        f'TRAN:ENAB? -> {bt(reply)}{" (after a retry)" if retried and off else ""}'
    )
    if errors:
        p.say(f'error queue after the pulsed acquisition: {errors}')
    for problem in problems:
        p.say(f'pulser cleanup: {problem}')
    if not off:
        raise PulserCheckError(f'pulser not confirmed OFF after pulsed_acquire: {reply!r}')


def _mem_clear(p: Probe) -> None:
    """`--mem-clear`: `MEM:CLEar` (as the vendor example, before `STAR AUTO`), via the guard."""
    if getattr(p.args, 'mem_clear', False):
        p.plug.write('MEM:CLEar')


def pulsed_acquire(p: Probe, n: int) -> list[PulsedPacket]:
    """The only place where the pulser is switched on (SPEC-phaseC.md 3.2).

    Open the data stream (`STAR AUTO`), announce and send `TRAN:ENAB ON` through the guard's
    gate with `write_checked`, read `TRAN:ENAB?`, read `n` packets. In a `finally`, whatever
    happened: `TRAN:ENAB OFF`, `STOP`, close the socket, verify OFF (`_make_pulser_safe`).
    """
    sock: RecordingSocket | None = None
    reader: FrameReader | None = None
    t_on: float | None = None
    try:
        length = int(p.plug.query('DATA:LENG?'))
        port = int(p.plug.query('DATA:PORT?'))
        sock = p.data_socket(port)
        reader = FrameReader(sock, length)
        _mem_clear(p)
        p.plug.write('STAR AUTO')
        t_on = time.monotonic()  # taken before the write: a failed ON write may still have worked
        with p.resource.pulser_gate():
            p.plug.write_checked('TRAN:ENAB ON')  # announced by the guard
        reply = p.plug.query('TRAN:ENAB?')
        p.say(f'TRAN:ENAB? -> {bt(reply)}')
        if not values_match('TRAN:ENAB', 'ON', reply):
            raise RuntimeError(f'TRAN:ENAB? answered {reply!r} after TRAN:ENAB ON')
        raw = reader.read(n, ACQ_TIMEOUT_S)
    finally:
        if reader is not None:  # per acquisition: what the resync dropped (stale bytes)
            p.current.data.setdefault('frame_stats', []).append(
                {'dropped_bytes': reader.dropped_bytes, 'resyncs': reader.resyncs}
            )
            p.say(f'frame stats: dropped_bytes={reader.dropped_bytes}, resyncs={reader.resyncs}')
        _make_pulser_safe(p, sock, t_on)
    # parsed only now: the pulser is already off
    return [
        PulsedPacket(
            np.frombuffer(x, dtype='<i2', offset=HEADER_SIZE).astype(np.int16), parse_header(x)
        )
        for x in raw
    ]


# ── analysis of pulsed records (pure functions, int16 arrays in) ─────────────


def baseline_stats(baseline: np.ndarray) -> tuple[float, float, float]:
    """`(mean, std, threshold)`: medians over the baseline packets and `max(10 s, 20)`."""
    base = np.asarray(baseline)
    mean = float(np.median(base.mean(axis=1, dtype=np.float64)))
    std = float(np.median(base.std(axis=1, dtype=np.float64)))
    return mean, std, max(ONSET_FACTOR * std, ONSET_MIN_COUNTS)


def _long_runs(mask: np.ndarray, minimum: int) -> np.ndarray:
    """Boolean array: the True samples of `mask` that lie in a run of at least `minimum`."""
    padded = np.concatenate(([False], mask, [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    out = np.zeros(mask.size, dtype=bool)
    for start, stop in zip(edges[0::2], edges[1::2], strict=True):
        if stop - start >= minimum:
            out[start:stop] = True
    return out


def _spread(values: Sequence[float | int | None]) -> float | int | None:
    found = [v for v in values if v is not None]
    return (max(found) - min(found)) if found else None


def coherent_signal(baseline: np.ndarray, pulsed: np.ndarray) -> dict[str, Any]:
    """A signal in the mean of the pulsed packets that no single packet shows above the threshold.

    `peak` is `max |mean_pulsed - median(mean_pulsed)|`; it counts as a signal when it exceeds
    `max(COHERENT_FACTOR * s / sqrt(n), COHERENT_MIN_COUNTS)`, `s` being the baseline std of one
    packet and `n` the number of pulsed packets (the noise of the mean is `s / sqrt(n)`). The
    baseline's own mean waveform is measured the same way for comparison.
    """
    pul = np.asarray(pulsed, dtype=np.float64)
    n = pul.shape[0]
    _mean, std, _threshold = baseline_stats(baseline)
    limit = max(COHERENT_FACTOR * std / np.sqrt(n), COHERENT_MIN_COUNTS)
    mean_wave = pul.mean(axis=0)
    dev = np.abs(mean_wave - np.median(mean_wave))
    base_wave = np.asarray(baseline, dtype=np.float64).mean(axis=0)
    return {
        'packets': n,
        'peak': float(dev.max()),
        'peak_index': int(dev.argmax()),
        'limit': float(limit),
        'found': bool(dev.max() > limit),
        'baseline_packets': int(np.asarray(baseline).shape[0]),
        'baseline_peak': float(np.abs(base_wave - np.median(base_wave)).max()),
    }


def analyse_pulsed(baseline: np.ndarray, pulsed: np.ndarray, fs_hz: float) -> dict[str, Any]:
    """Step 10 analysis (SPEC-phaseC.md 3.3.3 and 3.3.4) of int16 records, one row per packet."""
    base = np.asarray(baseline)
    pul = np.asarray(pulsed)
    mean, std, threshold = baseline_stats(base)
    base_peak = float(np.abs(base.astype(np.float64) - mean).max())
    n = pul.shape[1]
    tail = max(1, int(n * TAIL_FRACTION))
    packets: list[dict[str, Any]] = []
    saturated = flat_total = sat_packets = tail_packets = 0
    for row in pul:
        dev = np.abs(row.astype(np.float64) - mean)
        peak_index = int(dev.argmax())
        peak = float(dev[peak_index])
        above = np.flatnonzero(dev > threshold)
        onset = int(above[0]) if above.size else None
        at_limit = (row >= SAMPLE_MAX) | (row <= SAMPLE_MIN)
        flat = np.zeros(n, dtype=bool)
        if peak > threshold:  # the largest value (furthest from the baseline) repeated in a row
            flat = _long_runs(row == row[peak_index], FLAT_TOP_RUN)
        bad = int(np.count_nonzero(at_limit | flat))
        saturated += bad
        flat_total += int(np.count_nonzero(flat))
        sat_packets += bool(bad)
        tail_packets += bool((dev[-tail:] > threshold).any())
        packets.append(
            {
                'min': int(row.min()),
                'max': int(row.max()),
                'peak': peak,
                'peak_index': peak_index,
                'onset_index': onset,
                'onset_us': None if onset is None else onset * 1e6 / fs_hz,
                'peak_us': peak_index * 1e6 / fs_hz,
            }
        )
    count = len(packets)
    warnings: list[str] = []
    if saturated:
        warnings.append(
            f'saturation: {saturated} samples at the int16 limit or in a flat top '
            f'({flat_total} of them in a flat top of {FLAT_TOP_RUN} or more equal samples) '
            f'in {sat_packets} of {count} pulsed packets'
        )
    coherent = coherent_signal(base, pul)
    if all(x['peak'] <= threshold for x in packets):
        text = (
            f'no signal: peak <= {threshold:.1f} counts in every pulsed packet '
            f'(largest peak {max(x["peak"] for x in packets):.1f})'
        )
        if coherent['found']:
            text += (
                f'; but the mean of {coherent["packets"]} packets shows a coherent signal of '
                f'{coherent["peak"]:.1f} counts at sample {coherent["peak_index"]}, try more gain'
            )
        warnings.append(text)
    if base_peak > threshold:
        warnings.append(
            f'signal already present in the baseline (pulser off): baseline peak '
            f'{base_peak:.1f} counts above the threshold {threshold:.1f}'
        )
    if tail_packets:
        warnings.append(
            f'signal still large at the end of the record: samples above {threshold:.1f} counts '
            f'in the last {tail} samples of {tail_packets} of {count} pulsed packets; the window '
            'is too short or the ringing runs into the next shot'
        )
    peaks = [x['peak'] for x in packets]
    return {
        'fs_hz': fs_hz,
        'baseline_mean': mean,
        'baseline_std': std,
        'threshold': threshold,
        'baseline_peak': base_peak,
        'packets': packets,
        'peak_median': float(np.median(peaks)),
        'onset_spread_samples': _spread([x['onset_index'] for x in packets]),
        'peak_index_spread_samples': _spread([x['peak_index'] for x in packets]),
        'peak_spread': _spread(peaks),
        'coherent': coherent,
        'warnings': warnings,
    }


def cross_lag(ref: np.ndarray, other: np.ndarray) -> int:
    """Integer lag with the largest cross-correlation: positive when `other` comes later."""
    a = np.asarray(ref, dtype=np.float64)
    b = np.asarray(other, dtype=np.float64)
    a = a - a.mean()
    b = b - b.mean()
    n = a.size
    size = 1 << (2 * n - 1).bit_length()
    corr = np.fft.irfft(np.fft.rfft(b, size) * np.conj(np.fft.rfft(a, size)), size)
    lags = np.concatenate((corr[size - (n - 1) :], corr[:n]))  # lag -(n-1) .. n-1
    return int(np.argmax(lags)) - (n - 1)


def shift_verdict(
    lag: int, expected_samples: float, in_window: bool, ref_in_window: bool = True
) -> str:
    """What `TRIG:DEL` did to the record: `lag` samples (positive: later) against the delay."""
    if not ref_in_window:
        return 'no signal above the threshold, nothing to compare'
    if not in_window:
        return 'the signal left the window'
    tol = SHIFT_TOLERANCE_SAMPLES
    if abs(lag - expected_samples) <= tol:
        return f'the record moves later by the delay (within {tol} samples)'
    if abs(lag + expected_samples) <= tol:
        return f'the record moves earlier by the delay (within {tol} samples)'
    if abs(lag) <= tol:
        if expected_samples:
            return 'the signal does not move in the record: burst and record are delayed together'
        return 'the record does not move (TRIG:DEL has no effect on the position)'
    direction = 'later' if lag > 0 else 'earlier'
    ratio = f', {abs(lag) / expected_samples:.3g} x the delay' if expected_samples else ''
    return f'something else: the record moves {direction} by {abs(lag)} samples{ratio}'


def _save_npz(p: Probe, name: str, arrays: dict[str, np.ndarray], settings: dict[str, Any]) -> str:
    """`<out>/phaseC-<serial>-<stamp>-<name>.npz`: int16 arrays plus the settings as JSON text."""
    path = p.out_dir / f'phaseC-{p.serial}-{p.stamp}-{name}.npz'
    p.out_dir.mkdir(parents=True, exist_ok=True)
    np.savez(
        path,
        **{key: np.asarray(value, dtype=np.int16) for key, value in arrays.items()},
        settings=np.array(json.dumps(settings, default=str)),
    )
    return str(path)


def _run_settings(p: Probe, **extra: Any) -> dict[str, Any]:
    settings: dict[str, Any] = {str(k): v for k, v in phase_c_settings(p.args).items()}
    settings.update(pulse_v=p.args.pulse_v, fs_hz=_sample_rate(p), **extra)
    return settings


def _sample_rate(p: Probe) -> float:
    reply = p.resource.replies.get('FREQ?')
    if reply is None:
        raise RuntimeError('FREQ? was never read back: no time base')
    return float(reply)


def _report_analysis(p: Probe, result: dict[str, Any]) -> None:
    p.say(
        f'baseline (pulser off): mean {result["baseline_mean"]:.2f}, '
        f'std {result["baseline_std"]:.2f} counts, peak {result["baseline_peak"]:.1f}; '
        f'onset threshold {result["threshold"]:.1f} counts'
    )
    for i, x in enumerate(result['packets']):
        onset = (
            'none' if x['onset_index'] is None else f'{x["onset_index"]} ({x["onset_us"]:.1f} us)'
        )
        p.say(
            f'pulsed packet {i}: min {x["min"]} max {x["max"]} peak {x["peak"]:.1f} at '
            f'{x["peak_index"]} ({x["peak_us"]:.1f} us), onset {onset}'
        )
    coherent = result['coherent']
    p.say(
        f'coherent signal in the mean of {coherent["packets"]} packets: '
        + (
            f'peak {coherent["peak"]:.1f} counts at sample {coherent["peak_index"]}'
            if coherent['found']
            else 'none'
        )
        + f' (limit {coherent["limit"]:.1f} counts; mean of the {coherent["baseline_packets"]} '
        f'baseline packets: peak {coherent["baseline_peak"]:.1f})'
    )
    p.say(
        f'packet-to-packet spread: onset {result["onset_spread_samples"]} samples, peak position '
        f'{result["peak_index_spread_samples"]} samples, '
        f'peak amplitude {result["peak_spread"]:.1f} counts'
    )


def _time_per_packet(p: Probe, n: int) -> float | None:
    """Pulser-on time of the last `pulsed_acquire` per packet (it includes a few queries)."""
    on_s = p.pulses[-1]['on_s'] if p.pulses else None
    return None if on_s is None else float(on_s) / n


def step_10(p: Probe) -> None:
    """Experiment 10: where is the signal (pulser on, `--pulse-v`, the bench of the SPEC)."""
    fs = _sample_rate(p)
    _mem_clear(p)
    scans = p.plug.acquire(BASELINE_PACKETS, timeout_s=ACQ_TIMEOUT_S)  # pulser off
    baseline = np.vstack([x.raw for x in scans]).astype(np.int16)
    p.baseline = baseline
    for i, row in enumerate(baseline):
        p.say(f'baseline packet {i}: mean {row.mean():.2f} std {row.std():.2f}')
    pulsed = _stack(pulsed_acquire(p, STEP10_PACKETS))
    p.pulsed10 = pulsed
    result = analyse_pulsed(baseline, pulsed, fs)
    result['time_per_packet_s'] = _time_per_packet(p, STEP10_PACKETS)
    p.step10 = result
    _report_analysis(p, result)
    for text in result['warnings']:
        p.warn(text)
    path = _save_npz(p, 'step10', {'baseline': baseline, 'pulsed': pulsed}, _run_settings(p))
    p.say(f'raw samples saved to {path}')
    p.current.data.update(analysis=result, npz=path)


def step_10b(p: Probe) -> None:
    """Experiment 10b: averaging with a real signal (`AVER:COUN 4`), compared with step 10."""
    with _put_back(p, {'AVER:COUN': 0}):
        _apply_report(p, {'AVER:COUN': STEP10B_AVER})
        packets = pulsed_acquire(p, STEP10B_PACKETS)
    pulsed = _stack(packets)
    per_packet = _time_per_packet(p, STEP10B_PACKETS)
    counts = sorted({x.header.ascan_count for x in packets})
    p.say(f'ascan_count values: {counts}')
    data: dict[str, Any] = {
        'ascan_counts': counts,
        'time_per_packet_s': per_packet,
        'peak_ratio': None,
        'noise_ratio': None,
    }
    prev = p.step10
    if prev is None or p.pulsed10 is None:
        p.say('no step 10 result: no comparison')
    else:
        mean = prev['baseline_mean']
        peak = float(np.median(np.abs(pulsed.astype(np.float64) - mean).max(axis=1)))
        data['peak'] = peak
        data['peak_ratio'] = peak / prev['peak_median']
        p.say(
            f'peak amplitude {peak:.1f} against {prev["peak_median"]:.1f} in step 10: ratio '
            f'{data["peak_ratio"]:.3f} (1.0 means a mean, 16 a sum)'
        )
        plain = prev.get('time_per_packet_s')
        if per_packet is not None and plain:
            data['time_ratio'] = per_packet / plain
            p.say(
                f'time per packet (pulser-on time / packets): {per_packet:.3f} s with '
                f'AVER:COUN {STEP10B_AVER} against {plain:.3f} s without: ratio '
                f'{data["time_ratio"]:.2f} (16 acquisitions in a row would be 16)'
            )
        onsets = [x['onset_index'] for x in prev['packets'] if x['onset_index'] is not None]
        onset = min(onsets) if onsets else None
        if onset is not None and onset >= PRE_ONSET_MIN_SAMPLES:
            cut = slice(0, onset)
            part = f'the first {onset} samples (before the onset)'
        else:
            cut = slice(pulsed.shape[1] - pulsed.shape[1] // 4, None)
            part = 'the last quarter of the record (fewer than 50 samples before the onset)'
        now = float(np.median(pulsed[:, cut].std(axis=1)))
        before = float(np.median(p.pulsed10[:, cut].std(axis=1)))
        data.update(noise_std=now, noise_std_step10=before, noise_part=part)
        if cut.start == 0:
            data['pre_onset_samples'] = onset
        data['noise_ratio'] = now / before if before else None
        p.say(
            f'noise std in {part}: {now:.2f} against {before:.2f} in '
            f'step 10: ratio {data["noise_ratio"]} (0.25 is a mean of 16)'
        )
    path = _save_npz(p, 'step10b', {'pulsed': pulsed}, _run_settings(p, aver_coun=STEP10B_AVER))
    p.say(f'raw samples saved to {path}')
    data['npz'] = path
    p.current.data.update(data)


_DELAY_UNITS_NS = {'NS': 1.0, 'US': 1e3, 'MS': 1e6, 'S': 1e9}


def _delay_ns(readback: str | None, text: str) -> float:
    """The delay in ns: the read-back (the device answers in ns) or else the written text."""
    try:
        return float(str(readback))
    except ValueError:
        pass
    number, _, unit = text.partition(' ')
    return float(number) * _DELAY_UNITS_NS[unit.strip().upper() or 'NS']


def step_11(p: Probe) -> None:
    """Experiment 11: what `TRIG:DEL` shifts. Needs the baseline of step 10 for the threshold."""
    if p.baseline is None:
        raise RuntimeError('no baseline from step 10: nothing is pulsed')
    fs = _sample_rate(p)
    mean, _std, threshold = baseline_stats(p.baseline)
    entries: list[dict[str, Any]] = []
    waves: list[np.ndarray] = []
    arrays: dict[str, np.ndarray] = {}
    with _put_back(p, {'TRIG:DEL': TRIG_DEL_ZERO}):
        for i, delay in enumerate(STEP11_DELAYS):
            readbacks = _apply_report(p, {'TRIG:DEL': delay})
            pulsed = _stack(pulsed_acquire(p, STEP11_PACKETS))
            dev = np.abs(pulsed.astype(np.float64) - mean)
            onsets = [
                int(np.flatnonzero(row > threshold)[0]) if (row > threshold).any() else None
                for row in dev
            ]
            peaks = [int(row.argmax()) for row in dev]
            in_window = bool((dev.max(axis=1) > threshold).any())
            found = [x for x in onsets if x is not None]
            entries.append(
                {
                    'delay': delay,
                    'delay_readback': readbacks['TRIG:DEL'],
                    'delay_ns': _delay_ns(readbacks['TRIG:DEL'], delay),
                    'onset_indices': onsets,
                    'onset_index': int(np.median(found)) if found else None,
                    'peak_indices': peaks,
                    'peak_index': int(np.median(peaks)),
                    'in_window': in_window,
                }
            )
            waves.append(pulsed.mean(axis=0))
            arrays[f'pulsed_{i}'] = pulsed
            x = entries[-1]
            p.say(
                f'TRIG:DEL {delay}: onset index {x["onset_indices"]}, '
                f'peak index {x["peak_indices"]}'
                f'{"" if in_window else " (no sample above the threshold)"}'
            )
    ref = entries[0]
    for entry, wave in zip(entries, waves, strict=True):
        if entry is ref:
            entry.update(shift_samples=0, shift_us=0.0, verdict='reference')
            continue
        expected = (entry['delay_ns'] - ref['delay_ns']) * 1e-9 * fs
        both = bool(entry['in_window'] and ref['in_window'])
        lag = cross_lag(waves[0], wave) if both else 0
        entry.update(
            expected_shift_samples=expected,
            shift_samples=lag if both else None,
            shift_us=lag / fs * 1e6 if both else None,
            verdict=shift_verdict(lag, expected, bool(entry['in_window']), bool(ref['in_window'])),
        )
        shift = f'{lag} samples = {lag / fs * 1e6:.1f} us' if both else 'no shift measured'
        p.say(
            f'TRIG:DEL {entry["delay"]} against {ref["delay"]}: expected {expected:.1f} samples; '
            f'cross-correlation {shift}: {entry["verdict"]}'
        )
    path = _save_npz(p, 'step11', arrays, _run_settings(p, delays=list(STEP11_DELAYS)))
    p.say(f'raw samples saved to {path}')
    p.current.data.update(delays=entries, threshold=threshold, baseline_mean=mean, npz=path)


# ── Phase RST: experiment 13 ─────────────────────────────────────────────────


def _same_value(a: str, b: str) -> bool:
    a, b = a.strip().replace(' ', ''), b.strip().replace(' ', '')
    if a.upper() == b.upper():
        return True
    try:
        return abs(float(a) - float(b)) <= 1e-9 * max(1.0, abs(float(a)), abs(float(b)))
    except ValueError:
        return False


def step_13(p: Probe) -> None:
    """Experiment 13: `*RST`, pulser off at once, every header against snapshot and default.

    Unless `--no-detune`, six settings are moved away first (`RST_DETUNE`, read back by
    `apply_setup`) so that a `*RST` that does something shows; the restore at teardown puts
    them back with everything else.
    """
    detuned = not p.args.no_detune
    before: dict[str, str] = {}
    if detuned:
        _apply_report(p, RST_DETUNE)
        for header in STATE_HEADERS:
            reply, error, _dt = p.try_query(f'{header}?')
            before[header] = reply if error is None and reply is not None else f'<ERROR {error}>'
    try:
        p.plug.write('*RST')  # announced by the guard, sent once
        enab = p.plug.query('TRAN:ENAB?')  # at once: the pulser may come on with *RST
        p.say(f'TRAN:ENAB? right after *RST -> {bt(enab)}')
        if not _pulser_off(enab):
            p.say('the pulser was ON right after *RST (a result, not an error); switching it off')
        p.plug.write('TRAN:ENAB OFF')
        errors = p.plug.check_errors()
        confirm = p.plug.query('TRAN:ENAB?')
    except Exception as exc:  # noqa: BLE001 - the pulser state after *RST is then unknown
        raise PulserCheckError(f'could not switch the pulser off after *RST: {exc}') from exc
    p.say(f'error queue after *RST: {errors if errors else "empty"}')
    p.say(f'TRAN:ENAB? after TRAN:ENAB OFF -> {bt(confirm)}')
    p.current.data.update(enab_after_rst=enab, errors_after_rst=errors, enab_confirmed=confirm)
    if not values_match('TRAN:ENAB', 'OFF', confirm):
        raise PulserCheckError(
            f'TRAN:ENAB? answered {confirm!r} after TRAN:ENAB OFF following *RST'
        )
    p.say('pulser confirmed OFF by read-back')
    rows: list[dict[str, Any]] = []
    for header in (*STATE_HEADERS, 'DATA:PORT', 'SYST:ERR:COUN'):
        reply, error, _dt = p.try_query(f'{header}?')
        after = reply if error is None and reply is not None else f'<ERROR {error}>'
        snapshot = p.state.get(header, '-')
        default = DEFAULTS.get(header, '-') if header != 'SYST:ERR:COUN' else '-'
        is_snapshot = snapshot != '-' and _same_value(after, snapshot)
        is_default = default != '-' and _same_value(after, default)
        was = before.get(header, '-')
        if was != '-' and snapshot != '-' and not _same_value(was, snapshot):
            mark = (  # a detuned header (or one that follows from one)
                'kept the detuned value'
                if _same_value(after, was)
                else 'back to the snapshot value'
                if is_snapshot
                else '= vendor default'
                if is_default
                else 'a third value'
            )
        else:
            mark = (
                '= snapshot, = vendor default'
                if is_snapshot and is_default
                else '= snapshot'
                if is_snapshot
                else '= vendor default'
                if is_default
                else 'neither'
            )
        rows.append(
            {
                'header': header,
                'after_rst': after,
                'snapshot': snapshot,
                'default': default,
                'default_guessed': header in GUESSED_DEFAULTS,
                'mark': mark,
                **({'before_rst': was} if detuned else {}),
            }
        )
    head = f'{"header":22s} {"after *RST":26s} {"snapshot (step 2)":26s} {"vendor DEFault":26s} '
    if detuned:
        head += f'{"before *RST (detuned)":26s} '
    p.say(head + 'mark')
    for row in rows:
        star = '*' if row['default_guessed'] else ''
        line = (
            f'{row["header"]:22s} {row["after_rst"]:26s} {row["snapshot"]:26s} '
            f'{row["default"] + star:26s} '
        )
        if detuned:
            line += f'{row["before_rst"]:26s} '
        p.say(line + row['mark'])
    if detuned:
        changed = [
            h
            for h in RST_DETUNE
            if not _same_value(
                next(r['after_rst'] for r in rows if r['header'] == h), before.get(h, '')
            )
        ]
        p.say(
            f'*RST changed {len(changed)} of {len(RST_DETUNE)} detuned settings'
            + (f': {", ".join(changed)}' if changed else '')
        )
        p.current.data['detuned_changed_by_rst'] = changed
        p.current.data['detuned'] = {h: before.get(h) for h in RST_DETUNE}
    p.say('* = the fake uses a guess for this default (not documented by the vendor)')
    p.current.data['table'] = rows


# ── the plan (also what --dry-run prints) ────────────────────────────────────

StepFn = Callable[[Probe], None]

STEPS: dict[int | str, tuple[str, StepFn]] = {
    1: ('*IDN?, SYST:VERS?, SYST:ERR:COUN?', step_1),
    2: ('query every STATE_HEADERS entry, save the snapshot', step_2),
    3: ('DATA:PORT? stability', step_3),
    4: ('bare newline terminator on a raw socket', step_4),
    5: ('error queue behaviour and depth', step_5),
    6: ('acquisition at 1024 / 100 MHz / internal 10 ms', step_6),
    7: ('acquisition with AVER:COUN 4', step_7),
    8: ('STOP with the socket open; connect after STAR AUTO', step_8),
    9: ('GAIN change during acquisition', step_9),
    10: ('pulser on: where is the signal', step_10),
    '10b': ('pulser on: averaging with a real signal', step_10b),
    11: ('pulser on: what TRIG:DEL shifts', step_11),
    13: ('*RST: every header against snapshot and vendor default', step_13),
}


def steps_for(phase: str) -> list[int | str]:
    """Step ids of a phase. Every phase but A includes 1 and 2: the snapshot comes first."""
    a: list[int | str] = [1, 2, 3, 4, 5]  # step 4 is listed here but `run` and `plan` put it last
    return {
        'A': a,
        'B': [1, 2, 6, 7, 8, 9],
        'AB': a + [6, 7, 8, 9],
        'C': [1, 2, 10, '10b', 11],
        'RST': [1, 2, 13],
    }[phase]


def restores(phase: str) -> bool:
    """Phase A changes no settings and restores nothing; every other phase restores."""
    return phase != 'A'


def _setup_commands(settings: dict[str, object]) -> list[str]:
    commands: list[str] = []
    for key, value in settings.items():
        commands += [f'{key} {value}', ERR_QUERY, f'{key}?']
    return commands


_ACQUIRE = ['DATA:LENG?', 'FREQ?', 'TRIG:DEL?', 'DATA:PORT?', 'STAR AUTO', 'STOP', ERR_QUERY]


_ONLY_IF_VOLTAGE = '   (only if TRAN:PULS? differs from --pulse-v)'


def _pulsed_commands(n: int, mem_clear: bool = False) -> list[str]:
    """What `pulsed_acquire(n)` sends, in order."""
    return [
        'DATA:LENG?',
        'DATA:PORT?',
        *(['MEM:CLEar   (--mem-clear)'] if mem_clear else []),
        'STAR AUTO',
        'TRAN:ENAB ON   (PULSER ON, announced, only from pulsed_acquire)',
        ERR_QUERY,
        'TRAN:ENAB?',
        f'read {n} packets   (the pulser is on only while they are read)',
        'TRAN:ENAB OFF   (always, in a finally)',
        'STOP',
        'TRAN:ENAB?',
        ERR_QUERY + '   (drain)',
    ]


def _phase_c_commands(args: argparse.Namespace) -> dict[int | str, list[str]]:
    volts = '<--pulse-v>' if args.pulse_v is None else format(args.pulse_v, 'g')
    mem_clear = bool(getattr(args, 'mem_clear', False))
    drain = [ERR_QUERY + '   (drain)']
    return {
        'prep': drain
        + _setup_commands(phase_c_settings(args))
        + [
            'TRAN:PULS?',
            f'TRAN:PULS {volts} V{_ONLY_IF_VOLTAGE}   (announced)',
            ERR_QUERY + _ONLY_IF_VOLTAGE,
            'TRAN:PULS?' + _ONLY_IF_VOLTAGE,
        ],
        10: [
            *(['MEM:CLEar   (--mem-clear)'] if mem_clear else []),
            *_ACQUIRE,
            *_pulsed_commands(STEP10_PACKETS, mem_clear),
        ],
        '10b': [
            *drain,
            *_setup_commands({'AVER:COUN': STEP10B_AVER}),
            *_pulsed_commands(STEP10B_PACKETS, mem_clear),
            *drain,
            *_setup_commands({'AVER:COUN': 0}),
        ],
        11: [
            line
            for delay in STEP11_DELAYS
            for line in (
                *drain,
                *_setup_commands({'TRIG:DEL': delay}),
                *_pulsed_commands(STEP11_PACKETS, mem_clear),
            )
        ]
        + [*drain, *_setup_commands({'TRIG:DEL': TRIG_DEL_ZERO})],
        13: [
            *(
                []
                if args.no_detune
                else [
                    *drain,
                    *_setup_commands(RST_DETUNE),
                    *[f'{h}?' for h in STATE_HEADERS],
                ]
            ),
            '*RST   (announced, sent once)',
            'TRAN:ENAB?',
            'TRAN:ENAB OFF',
            ERR_QUERY + '   (drain)',
            'TRAN:ENAB?',
            *[f'{h}?' for h in STATE_HEADERS],
            'DATA:PORT?',
            'SYST:ERR:COUN?',
        ],
    }


def plan(phase: str, args: argparse.Namespace | None = None) -> list[tuple[str, list[str]]]:
    """`(heading, commands)` for every step of `phase`, in execution order."""
    chosen = steps_for(phase)
    if args is None:
        args = build_parser().parse_args(['--phase', phase])
    blocks: list[tuple[str, list[str]]] = [
        (
            'CONNECT (plug construction, restore_state snapshot deferred to step 2)',
            [ERR_QUERY, '*IDN?'],
        )
    ]
    commands: dict[int, list[str]] = {
        1: ['*IDN?', 'SYST:VERS?', 'SYST:ERR:COUN?'],
        2: [f'{h}?' for h in STATE_HEADERS],
        3: ['DATA:PORT?', 'DATA:PORT?'],
        4: [f'raw socket to <host>:{SCPI_PORT}, send b"*IDN?\\n", read until newline'],
        5: [
            ERR_QUERY + '   (drain)',
            f'{BAD_HEADER} 1',
            ERR_QUERY,
            ERR_QUERY,
            *[
                line
                for i in range(1, BAD_COUNT + 1)
                for line in (f'{BAD_HEADER}{i:02d}', 'SYST:ERR:COUN?')
            ],
            ERR_QUERY + f'   (drain, at most {ERR_READ_LIMIT} reads)',
        ],
        6: _setup_commands(STEP6_SETTINGS) + _ACQUIRE,
        7: _setup_commands(STEP7_SETTINGS) + _ACQUIRE,
        8: [
            'DATA:LENG?',
            'DATA:PORT?',
            'STAR AUTO',
            'STOP',
            ERR_QUERY,
            'DATA:PORT?',
            'STAR AUTO',
            'STOP',
            ERR_QUERY,
        ],
        9: [
            'GAIN?',
            'DATA:LENG?',
            'DATA:PORT?',
            'STAR AUTO',
            f'GAIN <GAIN as found +{GAIN_STEP_DB} dB, or -{GAIN_STEP_DB} dB above 74 dB>',
            ERR_QUERY,
            'GAIN?',
            'STOP',
            ERR_QUERY,
        ],
    }
    phase_c = _phase_c_commands(args)
    for number in chosen:
        if number == 4:
            continue
        if number in PULSER_OFF_BEFORE:
            blocks.append(
                (
                    'PULSER-OFF CHECK (aborts the run on failure)',
                    ['TRAN:ENAB OFF', ERR_QUERY, 'TRAN:ENAB?'],
                )
            )
        if number == 10:
            blocks.append(('PREPARATION (aborts the run on failure)', phase_c['prep']))
        blocks.append(
            (f'STEP {number}: {STEPS[number][0]}', commands.get(number, phase_c.get(number, [])))
        )
    final_queries = [f'{h}?' for h in STATE_HEADERS]
    if restores(phase):
        restore = [
            line
            for h in STATE_HEADERS
            if h not in UNSNAPSHOTTED and h != 'TRAN:ENAB'
            for line in (f'{h} <snapshot value>', ERR_QUERY)
        ]
        blocks.append(
            (
                'TEARDOWN (plug.tearDown, restore_state=True) then FINAL DIFF (protocol step 14)',
                [
                    'TRAN:ENAB OFF',
                    'STOP',
                    'TRAN:ENAB OFF',
                    ERR_QUERY + '   (drain)',
                    *restore,
                    'TRAN:ENAB OFF',
                    ERR_QUERY,
                    *final_queries,
                ],
            )
        )
    else:
        blocks.append(
            (
                'TEARDOWN (plug.tearDown, no restore: phase A is read-only) then FINAL DIFF '
                '(protocol step 14)',
                ['TRAN:ENAB OFF', 'STOP', 'TRAN:ENAB OFF', ERR_QUERY, *final_queries],
            )
        )
    if 4 in chosen:
        blocks.append(
            (
                f'STEP 4: {STEPS[4][0]} (runs last, on its own connection, after the main '
                'connection is closed)',
                commands[4],
            )
        )
    return blocks


_NEVER_SENT = {
    'C': (
        'NEVER SENT: TRAN:PULS writes other than --pulse-v (at most one, only if the device '
        'differs), TRAN:ENAB ON outside pulsed_acquire, *RST, anything to /config/ '
        '(refused in code).'
    ),
    'RST': (
        'NEVER SENT: TRAN:PULS writes, TRAN:ENAB ON, anything to /config/ (refused in code); '
        '*RST is sent once, in step 13.'
    ),
}


def print_plan(phase: str, args: argparse.Namespace | None = None) -> None:
    print(f'DRY RUN, phase {phase}: nothing is connected. Commands per step:')
    for heading, commands in plan(phase, args):
        print()
        print(heading)
        for cmd in commands:
            print(f'    {cmd}')
    print()
    print(
        _NEVER_SENT.get(
            phase, 'NEVER SENT: TRAN:PULS writes, *RST, anything to /config/ (refused in code).'
        )
    )
    print('Safety stop: TRAN:PULS as found must be <= --max-pulse-v after the snapshot.')


# ── the run ──────────────────────────────────────────────────────────────────


def _parse_volts(text: str | None) -> float | None:
    if text is None:
        return None
    match = re.match(r'\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)', text)
    return float(match.group(1)) if match else None


def ceiling_check(p: Probe) -> None:
    """Safety: refuse to go on if the device's `TRAN:PULS` is above `--max-pulse-v`.

    A `TRAN:PULS` that did not answer or cannot be read as a number also refuses: the
    ceiling cannot be verified. Nothing has been written at this point.
    """
    reply = p.answered.get('TRAN:PULS')
    volts = _parse_volts(reply)
    p.say(f'TRAN:PULS as found: {bt(reply)}; ceiling --max-pulse-v {p.args.max_pulse_v:g} V')
    if volts is None:
        raise AbortRun(EXIT_CEILING, f'TRAN:PULS {reply!r} cannot be read as a voltage')
    if volts > p.args.max_pulse_v:
        raise AbortRun(
            EXIT_CEILING, f'TRAN:PULS is {volts:g} V, above --max-pulse-v {p.args.max_pulse_v:g}'
        )
    p.say('pulser voltage within the ceiling')


def _pulser_off(value: str) -> bool:
    return value.strip().upper() in ('OFF', '0')


def _diff_is_expected(p: Probe, header: str, now: str) -> bool:
    """A phase C voltage change counts only if the device now has the voltage that was written."""
    if header == 'TRAN:PULS':
        volts = _parse_volts(now)
        return p.pulse_written and volts is not None and _close_enough(volts, float(p.args.pulse_v))
    return True


def final_diff(p: Probe) -> dict[str, Any]:
    """Protocol step 14: query `STATE_HEADERS` again (fresh read) and diff against the snapshot.

    A header that cannot be re-read is `unreadable`, not `changed`. `TRAN:ENAB` going from on
    to off is `expected`: the tool always leaves the pulser off.
    """
    p.current = StepRecord('FINAL DIFF', 'state after tearDown against the snapshot of step 2')
    p.records.append(p.current)
    p.say('')
    p.say('FINAL DIFF (protocol step 14): state after tearDown against the snapshot of step 2')
    if not p.state:
        p.say('no snapshot was taken, nothing to compare')
        return {'compared': 0, 'changed': {}, 'unreadable': {}}
    changed: dict[str, dict[str, Any]] = {}
    unreadable: dict[str, str] = {}
    for header in STATE_HEADERS:
        was = p.state.get(header, '<not in snapshot>')
        try:
            now = str(p.resource.query(f'{header}?')).strip()
        except Exception as exc:  # noqa: BLE001 - a header that never answered still must not stop the diff
            now = f'<ERROR {type(exc).__name__}: {exc}>'
            if now != was:
                if not unreadable:
                    p.say(f'UNREADABLE {header}: {type(exc).__name__}: {exc}')
                unreadable[header] = now
                continue
        if now != was:
            expected = header == 'TRAN:ENAB' and _pulser_off(now) and not _pulser_off(was)
            note = ' (expected: the tool always leaves the pulser off)' if expected else ''
            if header in p.expected_diffs and _diff_is_expected(p, header, now):
                expected = True
                note = f' (expected: {p.expected_diffs[header]})'
            changed[header] = {'was': was, 'now': now, **({'expected': True} if expected else {})}
            p.say(f'CHANGED {header}: was {bt(was)} now {bt(now)}{note}')
    if unreadable:
        p.say(
            f'final diff NOT POSSIBLE: {len(unreadable)} of {len(STATE_HEADERS)} headers '
            'could not be read (connection lost)'
        )
    if changed:
        p.say(f'{len(changed)} of {len(STATE_HEADERS)} headers differ from the snapshot')
    elif not unreadable:
        p.say(f'no differences: all {len(STATE_HEADERS)} headers equal the snapshot')
    result = {'compared': len(STATE_HEADERS), 'changed': changed, 'unreadable': unreadable}
    p.current.data.update(result)
    return result


def _open(
    args: argparse.Namespace, fake: FakeA1580Resource | None
) -> tuple[GuardedResource, Callable[[str, int], Any], str]:
    """The guarded SCPI resource, the data socket factory and the host for the report."""
    policy = GuardPolicy(
        phase=args.phase, pulse_v=getattr(args, 'pulse_v', None), max_pulse_v=args.max_pulse_v
    )
    if args.fake:
        # phase C needs a signal that depends on the pulser; the other phases keep the old fake
        sim = (
            fake
            if fake is not None
            else FakeA1580Resource(signal='transmission' if args.phase == 'C' else 'burst')
        )
        return GuardedResource(sim, policy=policy), sim.data_socket_factory, args.host or 'fake'
    import pyvisa  # lazy: --dry-run and --fake need no pyvisa

    rm = pyvisa.ResourceManager('@py')
    try:
        inner = rm.open_resource(f'TCPIP::{args.host}::{SCPI_PORT}::SOCKET')
    except Exception:
        rm.close()
        raise

    def factory(host: str, port: int) -> socket.socket:
        return socket.create_connection((host, port), timeout=5)

    return GuardedResource(inner, closer=rm.close, policy=policy), factory, args.host


def run(args: argparse.Namespace, fake: FakeA1580Resource | None = None) -> int:
    """Connect, run the selected steps, restore, diff, write the report. Returns the exit code."""
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    try:
        resource, raw_factory, host = _open(args, fake)
        factory = RecordingFactory(raw_factory)
        # restore_state=False here: the constructor snapshot would die on the first header
        # that does not answer. Step 2 takes the snapshot header by header instead.
        plug = A1580Plug(
            resource=resource, data_socket_factory=factory, host=host, restore_state=False
        )
    except Exception as exc:  # noqa: BLE001 - report any connection failure cleanly
        print(f'CONNECT FAILED: {type(exc).__name__}: {exc}', file=sys.stderr)
        return EXIT_FAILED_STEP
    probe = Probe(plug, resource, factory, args, host, stamp, bool(args.fake))
    code = 0
    diff: dict[str, Any] | None = None
    try:
        # tearDown restores only after a phase that changes settings (step 6 and above).
        steps = steps_for(args.phase)
        plug._restore_state = restores(args.phase)
        for number in steps:
            if number == 4:
                continue  # runs last, on its own connection, after the main one is closed
            if number in PULSER_OFF_BEFORE:
                probe.run_block('PULSER-OFF CHECK', 'TRAN:ENAB OFF and read-back', pulser_off_check)
            if number == 10:
                probe.run_block('PREPARATION', 'settings and pulser voltage', prepare_phase_c)
            title, func = STEPS[number]
            probe.run_block(f'STEP {number}', title, func)
            if number == 2:
                probe.run_block('SAFETY CHECK', 'pulser voltage ceiling', ceiling_check)
                plug._initial_state = {
                    h: v for h, v in probe.answered.items() if h not in UNSNAPSHOTTED
                }
        if any(r.status == 'failed' for r in probe.records):
            code = EXIT_FAILED_STEP
    except AbortRun as exc:
        code = exc.code
        print(f'RUN STOPPED: {exc}', file=sys.stderr)
    finally:
        restore = plug._restore_state
        probe.current = StepRecord('TEARDOWN', f'plug.tearDown(), restore_state={restore}')
        probe.records.append(probe.current)
        probe.say('')
        if restore:
            probe.say('TEARDOWN: STOP, restore the snapshot, close (plug.tearDown)')
        else:
            probe.say('TEARDOWN: no restore: phase A is read-only; pulser OFF, STOP, close')
        failures = _tear_down(plug)
        probe.current.data['restore_failures'] = failures
        details = []
        for failure in failures:
            expected = _expected_restore_failure(probe, failure)
            details.append({'failure': failure, 'expected': expected})
            if expected:
                probe.expected_diffs['DATA:LENG'] = 'known firmware defect, see the restore failure'
                probe.say(
                    'RESTORE FAILED (known firmware defect: power-on DATA:LENG is refused by '
                    f'the setter): {failure}'
                )
            else:
                probe.say(f'RESTORE FAILED: {failure}')
        probe.current.data['restore_failure_details'] = details
        if args.phase == 'C' and probe.pulse_written:
            probe.expected_diffs['TRAN:PULS'] = (
                'phase C set the pulser voltage; TRAN:PULS is never written back'
            )
        diff = final_diff(probe)
        if any(not v.get('expected') for v in diff['changed'].values()) or diff['unreadable']:
            code = code or EXIT_FAILED_STEP
        try:
            resource.shutdown()  # the main connection is really closed from here on
        except Exception as exc:  # noqa: BLE001 - the report must still be written
            print(
                f'closing the main connection failed: {type(exc).__name__}: {exc}', file=sys.stderr
            )
        if probe.warnings:
            probe.current = StepRecord('WARNINGS', 'measurement warnings of this run')
            probe.records.append(probe.current)
            probe.say('')
            probe.say('WARNINGS')
            for text in probe.warnings:
                probe.say(f'WARNING: {text}')
        if 4 in steps_for(args.phase):
            probe.run_block('STEP 4', *STEPS[4])
            if probe.records[-1].status == 'failed':
                code = code or EXIT_FAILED_STEP
        _write_probe_json(probe, code, diff)
    return code


def _expected_restore_failure(p: Probe, failure: str) -> bool:
    """Phases C and RST: the restore of a power-on `DATA:LENG` outside 1024 to 36864 is refused."""
    if p.args.phase not in ('C', 'RST') or not failure.startswith('DATA:LENG:'):
        return False
    snapshot = _parse_volts(p.state.get('DATA:LENG'))  # any leading number
    low, high = EXPECTED_LENG_RANGE
    return snapshot is not None and not low <= snapshot <= high


def _tear_down(plug: A1580Plug) -> list[str]:
    """`plug.tearDown()`; the restore failures it only logs, one string per failed header.

    `tearDown` swallows the `RuntimeError` of `set_state`, whose text lists every failure
    joined by `; `. A wrapper on the instance sees it first and passes it on unchanged, so
    the plug is not touched. A failure that is not that list comes back as one string.
    """
    failures: list[str] = []
    original = plug.set_state

    def watching(state: dict[str, str]) -> None:
        try:
            original(state)
        except RuntimeError as exc:
            text = str(exc).removeprefix('set_state failed: ')
            failures.extend(text.split('; '))
            raise

    plug.set_state = watching  # type: ignore[method-assign]
    try:
        plug.tearDown()
    finally:
        plug.set_state = original  # type: ignore[method-assign]
    return failures


def _write_probe_json(p: Probe, code: int, diff: dict[str, Any] | None) -> None:
    doc = {
        'tool': 'hw_probe',
        'timestamp': p.stamp,
        'fake': p.fake,
        'phase': p.args.phase,
        'max_pulse_v': p.args.max_pulse_v,
        'pulse_v': p.args.pulse_v,
        'idn': p.idn_reply,
        'exit_code': code,
        'steps': {r.label: r.as_json() for r in p.records},
        'final_diff': diff,
        'warnings': p.warnings,
        'pulses': p.pulses,
        'commands_sent': list(p.resource.sent),
    }
    p.out_dir.mkdir(parents=True, exist_ok=True)
    path = p.out_dir / f'probe-{p.serial}-{p.stamp}.json'
    path.write_text(json.dumps(doc, indent=2, default=str) + '\n', encoding='utf-8')
    print(f'\nreport written to {path}', flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='First-hardware-session probe for the A1580 (see HARDWARE-SESSION.md).'
    )
    parser.add_argument('--host', default=os.environ.get('A1580_HOST'), help='default $A1580_HOST')
    parser.add_argument('--phase', type=str.upper, choices=list(PHASES), default='A')
    parser.add_argument(
        '--dry-run', action='store_true', help='print the commands, connect to nothing'
    )
    parser.add_argument('--fake', action='store_true', help='run against FakeA1580Resource')
    parser.add_argument('--out', default='setups/', help='output directory (default setups/)')
    parser.add_argument(
        '--max-pulse-v',
        type=float,
        default=20.0,
        help='refuse to proceed if the device TRAN:PULS is above this (default 20); never written',
    )
    parser.add_argument('--packets', type=int, default=10, help='packets per acquisition (6, 7)')
    parser.add_argument(
        '--pulse-v',
        type=float,
        default=None,
        help='phase C (required): pulser voltage, 5 to --max-pulse-v; written only if it differs',
    )
    parser.add_argument(
        '--allow-rst', action='store_true', help='phase RST (required): allow the one *RST'
    )
    parser.add_argument(
        '--no-detune',
        action='store_true',
        help='phase RST: do not move six settings away before *RST (the old behaviour)',
    )
    parser.add_argument('--tran-freq', default='50 KHZ', help='phase C TRAN:FREQ (default 50 KHZ)')
    parser.add_argument('--sample-freq', default='1 MHZ', help='phase C FREQ (default 1 MHZ)')
    parser.add_argument('--length', type=int, default=8192, help='phase C DATA:LENG (default 8192)')
    parser.add_argument('--interval', default='100 MS', help='phase C TRIG:INT (default 100 MS)')
    parser.add_argument('--gain', type=float, default=0.0, help='phase C GAIN in dB (default 0)')
    parser.add_argument(
        '--mem-clear',
        action='store_true',
        help='phase C: send MEM:CLEar before every STAR AUTO of steps 10 to 11 (default off)',
    )
    return parser


def _usage_problem(args: argparse.Namespace) -> str | None:
    """The phase C and RST usage rules; also checked for `--dry-run`. None if all is well."""
    if args.phase == 'C':
        if args.pulse_v is None:
            return 'phase C needs --pulse-v (5 to --max-pulse-v)'
        if not PULSE_V_MIN <= args.pulse_v <= args.max_pulse_v:
            return (
                f'--pulse-v {args.pulse_v:g} must be between {PULSE_V_MIN:g} and '
                f'--max-pulse-v {args.max_pulse_v:g}'
            )
        for flag in ('tran_freq', 'sample_freq', 'interval'):
            if not _SAFE_SETTING.fullmatch(getattr(args, flag)):
                return f'--{flag.replace("_", "-")} may only hold letters, digits, blanks and . + -'
    if args.phase == 'RST' and not args.allow_rst:
        return 'phase RST sends *RST: pass --allow-rst'
    return None


def main(argv: Sequence[str] | None = None, *, fake: FakeA1580Resource | None = None) -> int:
    """Entry point. `fake` lets a test hand in the `FakeA1580Resource` it wants to inspect."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else EXIT_USAGE
    if args.packets < 1:
        print('--packets must be >= 1', file=sys.stderr)
        return EXIT_USAGE
    problem = _usage_problem(args)
    if problem:
        print(problem, file=sys.stderr)
        return EXIT_USAGE
    if args.dry_run:
        print_plan(args.phase, args)
        return 0
    if not args.fake and not args.host:
        print('no host: pass --host or set A1580_HOST', file=sys.stderr)
        return EXIT_USAGE
    return run(args, fake)


if __name__ == '__main__':
    sys.exit(main())
