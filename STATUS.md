# STATUS.md

Last updated: 2026-10-05 (end of the first hardware session) by the project owner agent.

## Decisions taken (with the human owner)

- Transport: pyvisa-py (`TCPIP::<host>::5025::SOCKET`) for SCPI, stdlib socket for the
  binary A-scan stream. Same as the vendor example.
- v1 scope: plug, fake, stream parser, tests, YAML capture conditions, example test,
  docs. Golden-waveform comparison comes later (SPEC-golden, not written yet).
- Layout: installable package `src/a1580_openhtf/`, `pyproject.toml`, `uv`, `ruff`,
  `mypy` on the typed modules, GitHub Actions running the hardware-free suite.
- Acquisition API: blocking `acquire(n, timeout_s)` built on start/collect/stop, plus
  `start_stream(callback)` / `stop_stream()`.
- Hardware: one A1580-HF at the vendor default `192.168.200.18`, firmware `1.16 (861f022a)`.
- Package `a1580-openhtf`, import `a1580_openhtf`, class `A1580Plug`, MIT, Python 3.12+.
- Code from rigol-dho-openhtf may be copied and adapted (provenance comment at the top).

## State

- [x] Vendor material digested into `PROTOCOL.md`.
- [x] Scaffold (SPEC.md section 0).
- [x] Plug, fake, stream parser, tests (SPEC.md). 375 hardware-free tests green.
- [x] Capture YAML (SPEC-capture.md).
- [x] Example test and README (README is pre-hardware; the vendor material is its only source).
- [x] Independent code review of the core; all 16 findings fixed with tests (586 tests).
      Key outcomes: pulser off is the first thing tearDown sends and a snapshot never
      re-enables it; apply_setup/set_state collect all failures; strict SCPI header table;
      values_match false positives closed; dead-link abort; per-stream state and SCPI lock.
- [x] `tools/hw_probe.py` implementing phases A and B of `HARDWARE-SESSION.md`, with
      `--dry-run` and `--fake`. Phase C (pulser on) is not implemented yet on purpose.
- [x] Hardware phase A (read-only), 2026-10-05, fw 1.16: README "Measured on the device", fake
      updated (booleans `0`/`1`, enumeration notation strings, error text, `SYST:VERS?`), 656 tests.
      The probe tool no longer restores after phase A and runs the second-connection experiment last.
      The differences from the vendor material are in `vendor-tickets/VENDOR-ISSUES.md`;
      README lists them in short.
- [x] Hardware phase B (pulser off, acquisition, nothing connected), 2026-10-05, fw 1.16, plus the
      extra experiments 5b (error queue) and B2 (`DATA:LENG` limits, header length field) and a
      power cycle. README entries 5 to 11; fake: queue depth 16 + `-350`, `-224` for illegal values,
      averaging as a mean over 2^N, `length_lo` = samples + 16, two packets after `STOP`, seconds
      replies as shortest decimals, `power_on=True` state; 676 tests.
- [x] Results of B2 in README (entries 12 and 13) and the fake: `DATA:LENG` range is the documented
      one, the length field is a 24-bit "samples + 16", `ctp[0]` is the packet counter, packets at
      the power-on length 114688 are partly invalid, automatic `AVER:DEL:CONS` is `TRIG:INT` minus
      70.75 us; 692 tests.
- [x] `SPEC-phaseC.md` and its implementation in `tools/hw_probe.py` (`--phase C --pulse-v N`,
      `--phase RST --allow-rst`): the pulser is on only inside `pulsed_acquire`, behind a guard.
- [x] Hardware phase C, experiments 10, 10b, 11, 2026-10-05, fw 1.16, two 50 kHz transducers face
      to face, 20 V: three runs (0 dB, 40 dB, 40 dB with the transducers pulled apart as the
      control). README entries 14 to 17; the fake's `signal='transmission'` follows the
      measurement; 861 tests.
- [x] Experiment 13 (`*RST`), two runs, 2026-10-05, fw 1.16: `*RST` changes no setting (six
      detuned settings stayed detuned). README entry 18; the fake's `*RST` no longer resets;
      `hw_probe.py --phase RST` detunes first by default; 868 tests.
- [x] Input buffer measured exactly (a line over 255 bytes including CRLF is discarded with
      `-363`); the plug refuses to send such a line (`MAX_LINE_BYTES`), the capture model reports
      an over-long `GAIN:TGC:ARB` line; 876 tests. Checked on the fake only; the limit itself was
      measured on the device with a raw socket.
- [x] Second SCPI connection, repeated 4 times: the device closes the first connection as soon as
      a second one to port 5025 is accepted.
- [ ] Experiment 12 (count-to-volt): deferred, a signal generator is available later.
- [ ] SPEC-golden (golden A-scan comparison), not written yet.

## Open questions after phases A and B

Still open from `PROTOCOL.md` "Unknowns to verify on hardware": single-shot acquisition,
count-to-volt scaling (experiment 12, deferred until a signal generator is on the bench), the
unit of `TRIG:DEL`, settling times, REST/WebSocket, whether `*RST` stops an acquisition or clears
the error queue, whether the pulser emits before `STAR AUTO` while `TRAN:ENAB` is 1. Settled in phase C (one bench setup): sample 0 is the start of the burst
to within a few microseconds; `TRIG:DEL` delays burst and record together, so the signal does
not move in the record; averaging is a mean also with a real signal; 40 dB of `GAIN` gave 37.4 dB. Settled so far: data port 2758, terminators, reply forms,
error-queue depth (16 + `-350`), `AVER:COUN` is an exponent and the result a mean, `ascan_count`
stays 1, `STOP` leaves two packets in flight and the socket open, both connect orders work, a
`GAIN` change while streaming is accepted, `DATA:LENG` range 1024 to 36864 with `-224` outside.

Owner decisions of 2026-10-05 (final):

- The plug does not switch the pulser off at construction, does not hide the refused power-on
  `DATA:LENG` in the restore, and does not send `MEM:CLEar` before `STAR AUTO` (no measured
  need: stale bytes were not reproduced with legal lengths). The first two are firmware defects
  and are reported to the vendor; README section "Firmware defects the plug does not work around".
- The tickets for the vendor are in `vendor-tickets/` (nine drafts and `post-tickets.sh`); the
  owner posts them himself. Not posted yet as of 2026-10-05.

Still open:

- One SCPI client only (a second connection closes the first); the fake does not model it.
- Burst overrun (many short lines without a read) is not modelled in the fake.
- `ctp[0]` equals the packet counter in `TRIG:MODE INT`; does it go on past 255?
- An averaged packet at `AVER:COUN 4` took about 3 trigger intervals, not 16.
- Next feature candidate: SPEC-golden (golden A-scan comparison), not written yet.

## Resuming (cold start, any machine)

You are the project-owner agent described in `AGENTS.md`: you talk to the device yourself,
following `HARDWARE-SESSION.md`; every code change goes to a coder agent that never touches
the device; you review, run the gates yourself, commit with the co-author line. Ask the human
owner whether you may push from the machine you are on (on the lab machine he pushes himself).

1. Read `AGENTS.md`, this file, `HARDWARE-SESSION.md`, then README sections "Measured on the
   device" and "Firmware defects the plug does not work around". `PROTOCOL.md` is the vendor
   digest and is never edited; README wins where they differ.
2. `uv sync --extra dev`, then the gates from `CLAUDE.md`. Expect 876 tests passing, ruff and
   mypy clean, and `tools/hw_probe.py --phase AB --fake`, `--phase C --pulse-v 20 --gain 40 --fake`,
   `--phase RST --allow-rst --fake` all exiting 0.
3. Before any contact with the device ask the human the preconditions of `HARDWARE-SESSION.md`
   again (address and reachability, what is connected and the voltage ceiling, exclusive use).
   The device is only reachable from a machine on its network.

What to know before touching the device (firmware 1.16):

- After power-on the pulser is enabled at 20 V and `DATA:LENG` is 114688 (illegal, partly invalid
  packets). First commands: `TRAN:ENAB OFF`, read back, then a legal `DATA:LENG`.
- One SCPI client: a second connection to port 5025 closes the first. Never run two tools at once.
- A line over 255 bytes including CRLF, or a burst of that size without reads, is discarded.
- `*RST` resets nothing; only a power cycle returns the power-on state.
- `tools/hw_probe.py` is the way to run experiments (`--dry-run` first, show the plan, wait for the
  human's go; `TRAN:PULS`, `TRAN:ENAB ON` and `*RST` are gated and need his explicit go each time).
  One-off experiments were done with short raw-socket scripts: one connection, state saved first,
  restore in a `finally`, log kept.

Next steps, in the order the owner last agreed:

1. Experiment 12, count-to-volt scaling: needs a signal generator on `IN` with known amplitude
   and frequency, pulser off. The probe tool has no phase for it: write a SPEC first (settings,
   amplitude steps, saturation check, raw int16 records saved, gain steps), then a coder, then the
   run. Result: README entry, a `v` conversion only if the scaling proves stable.
2. SPEC-golden (golden A-scan comparison): the next feature, not written yet.
3. Smaller open measurements, when convenient: `ctp[0]` past 255, whether the pulser emits before
   `STAR AUTO`, spacing of averaged acquisitions, whether `*RST` stops a stream or clears the error
   queue, the unit of `TRIG:DEL`, single-shot acquisition, settling times.

Not in this repository (kept by the owner on the lab machine, they contain the serial number or
are his to publish): the session log of 2026-10-05 and the raw records and probe reports under
`setups/`. Every fact from them that matters is in README.
