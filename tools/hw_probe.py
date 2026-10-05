#!/usr/bin/env python3
"""Instrument for the first session with the real A1580 (HARDWARE-SESSION.md).

Phase A is read-only (experiments 1 to 5, the only writes are the deliberate bad headers of
step 5). Phase B (experiments 6 to 9) switches the pulser off first, then changes a few
acquisition settings and streams A-scans. Phase C (pulser on) is not implemented on purpose.

Safety, enforced in code and not only by convention:

- Every SCPI string goes through `GuardedResource`, which refuses `TRAN:PULS` writes, `*RST`
  and anything that mentions `/config`. The tool never changes `TRAN:PULS`; the restore at
  the end (phases B and AB only) leaves it out of the snapshot it writes back.
- After the snapshot (step 2) the run stops if the device's `TRAN:PULS` is above
  `--max-pulse-v` or cannot be read.
- Everything runs inside `try: ... finally: plug.tearDown()`. Phases B and AB restore the
  snapshot there; phase A changes no settings and restores nothing (the pulser is only
  switched off). Then the state headers are queried again and diffed against the snapshot
  (protocol step 14).

Usage:
    A1580_HOST=... tools/hw_probe.py --dry-run          # command list, no connection
    A1580_HOST=... tools/hw_probe.py --phase A
    tools/hw_probe.py --fake --phase AB --out /tmp/probe # against FakeA1580Resource
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import socket
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from a1580_openhtf.fake_resource import FakeA1580Resource
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


def check_allowed(cmd: str, *, is_write: bool) -> None:
    """Raise ForbiddenCommand for `TRAN:PULS` writes, `*RST` and anything about `/config`."""
    if '/config' in cmd.lower():
        raise ForbiddenCommand(f'refusing to send {cmd!r}: /config is never touched')
    head = cmd.strip().split(' ', 1)[0]
    try:
        normal = normalize_header(head)
    except ValueError:
        return  # unknown (like the deliberate ZZZ:NOPE of step 5): none of the forbidden ones
    if is_write and normal in FORBIDDEN_WRITES:
        raise ForbiddenCommand(f'refusing to send {cmd!r}')


class GuardedResource:
    """Wrap the SCPI resource: log every string, refuse the forbidden ones, defer `close`.

    `close()` does nothing so that the plug's `tearDown` leaves the session usable for the
    final read-back; `shutdown()` really closes it.
    """

    def __init__(self, inner: Any, closer: Callable[[], None] | None = None) -> None:
        object.__setattr__(self, '_inner', inner)
        object.__setattr__(self, '_closer', closer)
        object.__setattr__(self, 'sent', [])
        object.__setattr__(self, 'replies', {})  # last reply per query string, exactly as received

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._inner, name, value)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def write(self, cmd: str) -> None:
        check_allowed(cmd, is_write=True)
        self.sent.append(cmd)
        self._inner.write(cmd)

    def query(self, cmd: str) -> Any:
        check_allowed(cmd, is_write=False)
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

    # report ---------------------------------------------------------------

    def say(self, line: str = '') -> None:
        print(line, flush=True)
        self.current.lines.append(line)

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


# ── the plan (also what --dry-run prints) ────────────────────────────────────

StepFn = Callable[[Probe], None]

STEPS: dict[int, tuple[str, StepFn]] = {
    1: ('*IDN?, SYST:VERS?, SYST:ERR:COUN?', step_1),
    2: ('query every STATE_HEADERS entry, save the snapshot', step_2),
    3: ('DATA:PORT? stability', step_3),
    4: ('bare newline terminator on a raw socket', step_4),
    5: ('error queue behaviour and depth', step_5),
    6: ('acquisition at 1024 / 100 MHz / internal 10 ms', step_6),
    7: ('acquisition with AVER:COUN 4', step_7),
    8: ('STOP with the socket open; connect after STAR AUTO', step_8),
    9: ('GAIN change during acquisition', step_9),
}


def steps_for(phase: str) -> list[int]:
    """Step numbers of a phase. Phase B includes 1 and 2: the snapshot comes first."""
    a = [1, 2, 3, 4, 5]  # step 4 is listed here but `run` and `plan` put it last
    b = [1, 2, 6, 7, 8, 9]
    return {'A': a, 'B': b, 'AB': a + [6, 7, 8, 9]}[phase]


def _setup_commands(settings: dict[str, object]) -> list[str]:
    commands: list[str] = []
    for key, value in settings.items():
        commands += [f'{key} {value}', ERR_QUERY, f'{key}?']
    return commands


_ACQUIRE = ['DATA:LENG?', 'FREQ?', 'TRIG:DEL?', 'DATA:PORT?', 'STAR AUTO', 'STOP', ERR_QUERY]


def plan(phase: str) -> list[tuple[str, list[str]]]:
    """`(heading, commands)` for every step of `phase`, in execution order."""
    chosen = steps_for(phase)
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
    for number in chosen:
        if number == 4:
            continue
        if number == 6:
            blocks.append(
                (
                    'PULSER-OFF CHECK (aborts the run on failure)',
                    ['TRAN:ENAB OFF', ERR_QUERY, 'TRAN:ENAB?'],
                )
            )
        blocks.append((f'STEP {number}: {STEPS[number][0]}', commands[number]))
    final_queries = [f'{h}?' for h in STATE_HEADERS]
    if any(n >= 6 for n in chosen):
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


def print_plan(phase: str) -> None:
    print(f'DRY RUN, phase {phase}: nothing is connected. Commands per step:')
    for heading, commands in plan(phase):
        print()
        print(heading)
        for cmd in commands:
            print(f'    {cmd}')
    print()
    print('NEVER SENT: TRAN:PULS writes, *RST, anything to /config/ (refused in code).')
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
            changed[header] = {'was': was, 'now': now, **({'expected': True} if expected else {})}
            note = ' (expected: the tool always leaves the pulser off)' if expected else ''
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
    if args.fake:
        sim = fake if fake is not None else FakeA1580Resource()
        return GuardedResource(sim), sim.data_socket_factory, args.host or 'fake'
    import pyvisa  # lazy: --dry-run and --fake need no pyvisa

    rm = pyvisa.ResourceManager('@py')
    try:
        inner = rm.open_resource(f'TCPIP::{args.host}::{SCPI_PORT}::SOCKET')
    except Exception:
        rm.close()
        raise

    def factory(host: str, port: int) -> socket.socket:
        return socket.create_connection((host, port), timeout=5)

    return GuardedResource(inner, closer=rm.close), factory, args.host


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
        plug._restore_state = any(n >= 6 for n in steps)
        for number in steps:
            if number == 4:
                continue  # runs last, on its own connection, after the main one is closed
            if number == 6:
                probe.run_block('PULSER-OFF CHECK', 'TRAN:ENAB OFF and read-back', pulser_off_check)
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
        for failure in failures:
            probe.say(f'RESTORE FAILED: {failure}')
        diff = final_diff(probe)
        if any(not v.get('expected') for v in diff['changed'].values()) or diff['unreadable']:
            code = code or EXIT_FAILED_STEP
        try:
            resource.shutdown()  # the main connection is really closed from here on
        except Exception as exc:  # noqa: BLE001 - the report must still be written
            print(
                f'closing the main connection failed: {type(exc).__name__}: {exc}', file=sys.stderr
            )
        if 4 in steps_for(args.phase):
            probe.run_block('STEP 4', *STEPS[4])
            if probe.records[-1].status == 'failed':
                code = code or EXIT_FAILED_STEP
        _write_probe_json(probe, code, diff)
    return code


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
        'idn': p.idn_reply,
        'exit_code': code,
        'steps': {r.label: r.as_json() for r in p.records},
        'final_diff': diff,
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
    parser.add_argument('--phase', type=str.upper, choices=['A', 'B', 'AB'], default='A')
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
    return parser


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
    if args.dry_run:
        print_plan(args.phase)
        return 0
    if not args.fake and not args.host:
        print('no host: pass --host or set A1580_HOST', file=sys.stderr)
        return EXIT_USAGE
    return run(args, fake)


if __name__ == '__main__':
    sys.exit(main())
