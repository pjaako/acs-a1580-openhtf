# STATUS.md

Last updated: 2026-10-05 (after hardware phases A and B) by the project owner agent.

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
      The differences from the vendor material are kept by the owner as a local ticket list
      (not in the repository); README lists them in short.
- [x] Hardware phase B (pulser off, acquisition, nothing connected), 2026-10-05, fw 1.16, plus the
      extra experiments 5b (error queue) and B2 (`DATA:LENG` limits, header length field) and a
      power cycle. README entries 5 to 11; fake: queue depth 16 + `-350`, `-224` for illegal values,
      averaging as a mean over 2^N, `length_lo` = samples + 16, two packets after `STOP`, seconds
      replies as shortest decimals, `power_on=True` state; 676 tests.
- [x] Results of B2 in README (entries 12 and 13) and the fake: `DATA:LENG` range is the documented
      one, the length field is a 24-bit "samples + 16", `ctp[0]` is the packet counter, packets at
      the power-on length 114688 are partly invalid, automatic `AVER:DEL:CONS` is `TRIG:INT` minus
      70.75 us; 692 tests.
- [ ] Phase C (pulser on, transducer connected). The probe tool has no phase C yet; SPEC first.
- [ ] SPEC-golden (golden A-scan comparison), not written yet.

## Open questions after phases A and B

Still open from `PROTOCOL.md` "Unknowns to verify on hardware": single-shot acquisition,
count-to-volt scaling, time zero and what `TRIG:DEL` shifts, `*RST` defaults, settling times,
per-connection state, REST/WebSocket. Settled so far: data port 2758, terminators, reply forms,
error-queue depth (16 + `-350`), `AVER:COUN` is an exponent and the result a mean, `ascan_count`
stays 1, `STOP` leaves two packets in flight and the socket open, both connect orders work, a
`GAIN` change while streaming is accepted, `DATA:LENG` range 1024 to 36864 with `-224` outside.

New questions:

- The device powers on with `DATA:LENG` 114688, which its own setter refuses. A snapshot taken
  after power-on therefore cannot be restored completely. Decision needed for the plug: skip a
  value the device refuses, or keep reporting the failure (current behaviour: failure reported,
  everything else restored). At 114688 the device streams partly invalid packets (README entry 13).
- The device powers on with the pulser enabled at 20 V. The plug switches it off at `tearDown`
  only; should construction switch it off too? Owner decision.
- A second connection to port 5025 killed the first (seen once). Open or close? The data port does
  not have this effect.
- Input buffer: a burst above 160 to 320 bytes is discarded with `-363` and a query in it is never
  answered. The longest line the plug can produce (a long `GAIN:TGC:ARB` list) is not checked.
- `ctp[0]` equals the packet counter in `TRIG:MODE INT`; does it go on past 255?
- Automatic `AVER:DEL:CONS` seems to follow `TRIG:INT` (interval minus 70.75 us).

## Resuming as the owner agent on the local machine

The work so far was done in a cloud session with no device access. The next session runs
on the machine next to the A1580, with the human owner at the terminal. The local agent
takes the project-owner role described in `AGENTS.md`: it talks to the device itself,
following `HARDWARE-SESSION.md` step by step, and delegates code changes (fake, tests,
README edits) to coder subagents that never touch the device.

Checklist for that session, in order:
1. `git fetch && git checkout claude/friendly-brown-vhwy3d && git pull`.
2. Python 3.12+ (`uv python install 3.13` if needed), `uv sync --extra dev`, then run the
   gates from `CLAUDE.md`; expect 586 tests passing before anything else.
3. Ask the human for the three preconditions in `HARDWARE-SESSION.md` and record the
   answers in the session log. Do not assume any of them.
4. `tools/hw_probe.py --phase A --dry-run`, show the plan, get a go, run phase A, then
   phase B. Phase C (pulser on) only after an explicit go with the voltage named.
5. After each phase: README section "Measured on the device" (date, firmware), fake
   updated for every discrepancy with a test, `STATUS.md` unknowns resolved or sharpened,
   commit with the co-author line, push.
