# AGENTS.md

OpenHTF plug for the ACS A1580 ultrasonic pulser-receiver. Sibling of
[rigol-dho-openhtf](https://github.com/pjaako/rigol-dho-openhtf); same concept,
different instrument. Read this file, then `STATUS.md`, then the SPEC you were given.

## Who does what

- The project owner (a human plus their project-owner agent) writes the SPECs, commits,
  talks to the real instrument and keeps `STATUS.md`, `README.md` and the SPECs current.
- Coder agents implement one SPEC each. They never commit, never push, never touch the
  real instrument and never change `PROTOCOL.md`. They report back as the SPEC's
  `Report:` line asks.
- If a SPEC is wrong, ambiguous or impossible, do the closest sensible thing, keep going,
  and list every deviation in the report. Do not silently narrow the scope.

## Documents and which one wins

| File | Role | Wins over |
|---|---|---|
| `PROTOCOL.md` | Digest of the vendor material only. Every claim cites the vendor file. `UNKNOWN` and `CONFLICT n` are deliberate. | nothing; it records what the vendor wrote, not what the device does |
| `README.md` | What the real device does. Every hardware fact carries a date and the firmware version it was measured on ("measured 2026-10-04, fw 1.6.b41"). | `PROTOCOL.md` and the SPECs |
| `SPEC*.md` | Contracts for coder agents. Numbered sections; tests mirror the numbers. | nothing once superseded by README |
| `STATUS.md` | Current state, next steps, open questions for the hardware. Updated by the owner after every step. | n/a |
| `AGENTS.md` | This file. Rules and lessons learned. | n/a |

## Environment

- Python 3.12 or newer. The project venv is `.venv/` (made with `uv venv --python 3.13`),
  dependencies come from `pyproject.toml` (`uv sync --extra dev`). Do not install anything
  else. Run things as `.venv/bin/python -m pytest -q`, `.venv/bin/ruff check .`,
  `.venv/bin/ruff format --check .`, `.venv/bin/mypy` (strict, only the modules listed in
  `pyproject.toml`).
- Everything must run without hardware. The fake is `a1580_openhtf.fake_resource.FakeA1580Resource`.
  It covers both channels: the SCPI resource and the A-scan data socket.
- The real A1580 is on a private network. Coder agents must not open sockets to any
  address. Tests may only use the fake and `localhost` sockets they create themselves.

## Hard rules learned on the sibling project (keep them)

- A fake only knows what we told it. Nothing is "done" until it has run on the real
  instrument; every finding from hardware goes back into the fake and into README.
- The plug stays thin: no setter per instrument setting. Settings are data
  (`apply_setup(dict)` with read-back verification; capture YAML on top of it).
- Read every value back after writing it. The vendor doc documents silent clamping
  nowhere, so assume it can happen.
- Drain the SCPI error queue after configuration. The vendor example never does; we do.
- Store raw samples (`int16`) plus the header and the settings that give the time base.
  Never store derived float arrays. Count-to-volt scaling is UNKNOWN (see `PROTOCOL.md`),
  so there is no `v` array until it is measured.
- Hand-written hardware checks wrap their work in `try: ... finally: plug.tearDown()`.
- Public repository: `192.168.200.18` is the vendor factory default and may appear in
  docs. Any other real address, serial number or site detail stays out of git.
  Tools read the address from the environment variable `A1580_HOST`.
- Set OpenHTF config keys after importing the plug module (`CONF.load(...)` before the
  key is declared is lost).
- Commits are made by the owner and end with the line
  `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

## Code style

- Single-quoted strings, `# ── section ──` banner comments, `# noqa: BLE001 - reason` on
  deliberately broad excepts. `ruff` configuration in `pyproject.toml` is the arbiter.
- Lazy imports for `pyvisa`, `yaml`, `matplotlib` so that importing the package needs
  neither hardware nor heavy dependencies.
- Errors: `ValueError` for bad arguments, `RuntimeError` for instrument or state problems
  (message names the command or value), `TimeoutError` for waits.
