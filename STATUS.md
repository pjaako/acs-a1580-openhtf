# STATUS.md

Last updated: 2026-10-05 (after hardware phase A) by the project owner agent.

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
      `VENDOR-ISSUES.md` lists every difference from the vendor material (A1 to A12).
- [ ] Hardware phase B (pulser off, acquisition), then phase C (pulser on; the probe tool has no
      phase C yet, a SPEC for it comes first).
- [ ] SPEC-golden (golden A-scan comparison), not written yet.

## Open questions after phase A

From `PROTOCOL.md` "Unknowns to verify on hardware"; the ones that block design choices are still
single-shot acquisition, the meaning of `AVER:COUN` and the header `ascan_count`, and count-to-volt
scaling. Settled: `DATA:PORT?` is `2758` (not after `*RST`), a bare `\n` terminator works, reply
forms of booleans and enumerations. New or sharpened by phase A:

- Input-buffer overrun: 25 lines written back to back lost 24 of them (`-363`). How many lines
  survive without a read in between is not measured. It matters for `tearDown`, which sends
  `TRAN:ENAB OFF`, `STOP`, `TRAN:ENAB OFF` without reading. Error-queue depth is still unknown for
  the same reason.
- A second connection to port 5025 killed the first (seen once). Open or close? Does the data
  socket count?
- `DATA:LENG` was 114688 as found, above the documented 36864: real range unknown; will the
  restore after phase B be accepted?
- Are the as-found settings (pulser on, `DUAL`, `TRIG:INT` 1 s) power-on values?
- Number format of time replies after a write (only `1`, `0.99992925`, `2e-06` seen so far); the
  fake still answers in the vendor's `10.0E-3` style.
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
