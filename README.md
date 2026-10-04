# a1580-openhtf

OpenHTF plug for the ACS A1580 ultrasonic pulser-receiver: SCPI session with
read-back-verified settings, A-scan acquisition from the binary data stream, and a
hardware-free fake for tests.

Status: pre-hardware, nothing measured yet.

## Setup

```
uv sync --extra dev
.venv/bin/python -m pytest
.venv/bin/python examples/example_test.py --fake
```

## Files

- `AGENTS.md` - rules for coder agents
- `SPEC.md`, `SPEC-capture.md` - contracts for coder agents
- `PROTOCOL.md` - digest of the vendor material
- `STATUS.md` - current state and next steps
- `src/a1580_openhtf/` - the package
- `tests/` - pytest suite (no hardware)
- `examples/` - minimal OpenHTF test
