# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

OpenHTF plug for the ACS A1580 ultrasonic pulser-receiver (Ethernet: SCPI on TCP 5025
plus a binary A-scan stream on a second port). Sibling of pjaako/rigol-dho-openhtf.

Read in this order: `AGENTS.md` (roles, rules, which document wins), `STATUS.md` (current
state, next steps, open hardware questions), then the `SPEC*.md` you are working on.
`PROTOCOL.md` is the vendor digest; `README.md` records what the real device does and
overrides it.

## Commands

```bash
uv sync --extra dev                                  # into .venv/ (Python 3.12+)
.venv/bin/python -m pytest                           # hardware-free suite, uses the fake
.venv/bin/python -m pytest tests/test_plug.py -k acquire   # one test
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy                                       # strict, modules listed in pyproject
.venv/bin/python examples/example_test.py --fake     # minimal OpenHTF test, no hardware
.venv/bin/python -m a1580_openhtf.capture check captures/*.yaml
```

## Architecture in one paragraph

`src/a1580_openhtf/plug.py` holds `A1580Plug(BasePlug)`: thin, no per-setting setters;
`apply_setup(dict)` writes each SCPI header and verifies it by read-back; `acquire(n)` and
`start_stream()` open the data socket, send `STAR AUTO`, collect packets through
`stream.FrameReader`, send `STOP`. `stream.py` is the pure packet layer (28-byte `FtH1`
header, int16 samples, resync). `fake_resource.py` fakes both channels and is the only
instrument coder agents may use. `capture.py` is the typed settings model behind the YAML
files in `captures/` and the generated `capture.schema.json`. Config keys are
`a1580_host`, `a1580_scpi_port`, `a1580_restore_state`, declared when `plug.py` is imported.

## Rules that are easy to break

- Never open a socket to the real instrument from a coder task; never change its network
  settings over the network.
- Nothing is done until it ran on hardware; every hardware finding goes into the fake and
  into README with a date and firmware version.
- Commits end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
