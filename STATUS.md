# STATUS.md

Last updated: 2026-10-04 by the project owner agent.

## Decisions taken (with the human owner)

- Transport: pyvisa-py (`TCPIP::<host>::5025::SOCKET`) for SCPI, stdlib socket for the
  binary A-scan stream. Same as the vendor example.
- v1 scope: plug, fake, stream parser, tests, YAML capture conditions, example test,
  docs. Golden-waveform comparison comes later (SPEC-golden, not written yet).
- Layout: installable package `src/a1580_openhtf/`, `pyproject.toml`, `uv`, `ruff`,
  `mypy` on the typed modules, GitHub Actions running the hardware-free suite.
- Acquisition API: blocking `acquire(n, timeout_s)` built on start/collect/stop, plus
  `start_stream(callback)` / `stop_stream()`.
- Hardware: one A1580-HF at the vendor default `192.168.200.18`, firmware unknown.
- Package `a1580-openhtf`, import `a1580_openhtf`, class `A1580Plug`, MIT, Python 3.12+.
- Code from rigol-dho-openhtf may be copied and adapted (provenance comment at the top).

## State

- [x] Vendor material digested into `PROTOCOL.md`.
- [x] Scaffold (SPEC.md section 0).
- [x] Plug, fake, stream parser, tests (SPEC.md). 375 hardware-free tests green.
- [x] Capture YAML (SPEC-capture.md).
- [x] Example test and README (README is pre-hardware; the vendor material is its only source).
- [ ] Independent code review of the core (in progress), findings folded in.
- [ ] `tools/hw_probe.py` implementing phases A and B of `HARDWARE-SESSION.md` (in progress).
- [ ] First hardware session (needs the device reachable from the agent, see HARDWARE-SESSION.md preconditions).
- [ ] SPEC-golden (golden A-scan comparison), not written yet.

## Open questions for the first hardware session

See `PROTOCOL.md`, section "Unknowns to verify on hardware". The ones that block design
choices are: the real `DATA:PORT?` value, whether a single-shot acquisition exists,
the meaning of `AVER:COUN` and the header `ascan_count`, and count-to-volt scaling.
