# a1580-openhtf

OpenHTF plug for the ACS A1580 ultrasonic pulser-receiver. Ethernet only: SCPI on TCP 5025 plus a binary
A-scan stream on a second TCP port. Built on PyVISA + pyvisa-py for SCPI and a plain socket for the stream.
Sibling of [rigol-dho-openhtf](https://github.com/pjaako/rigol-dho-openhtf): same concept, different instrument.

**Status: pre-hardware. Nothing below has been verified on a real A1580 yet; every fact comes from the vendor's
a1580_examples repository (see PROTOCOL.md). This section will be rewritten with dated measurements after the
first hardware session.**

```python
import openhtf as htf
from a1580_openhtf import A1580Plug

@htf.measures(htf.Measurement('num_points'), htf.Measurement('peak_counts'))
@htf.plug(pr=A1580Plug)
def phase(test, pr):
    pr.apply_capture('captures/vendor_example.yaml')    # validated, written, read back, errors drained
    scans = pr.acquire(4)                                # four A-scans, then STOP
    test.measurements.num_points = len(scans[0].raw)    # raw int16 counts, no volts
    test.measurements.peak_counts = int(abs(scans[0].raw.astype(int)).max())
```

`vendor_example.yaml` switches the pulser on (20 V). The plug restores the settings it found when the test ends.

## Setup

```bash
uv sync --extra dev                                         # into .venv/, Python 3.12 or newer
.venv/bin/python -m pytest                                  # hardware-free suite, uses the fake
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy                                              # strict, the modules listed in pyproject.toml
.venv/bin/python examples/example_test.py --fake            # one OpenHTF phase, no hardware
.venv/bin/python -m a1580_openhtf.capture check captures/*.yaml
```

Configuration keys (OpenHTF `CONF`, declared when `a1580_openhtf.plug` is imported):

| Key | Default | Meaning |
|---|---|---|
| `a1580_host` | `192.168.200.18` | IP address or host name. The default is the address in all vendor examples. |
| `a1580_scpi_port` | `5025` | SCPI port. The data port is not configured: the plug asks the device (`DATA:PORT?`). |
| `a1580_restore_state` | `True` | Take a settings snapshot when the plug is created and restore it in `tearDown()`. |

Set them after importing the plug module, e.g. `CONF.load(a1580_host='10.0.0.5', _override=True)`; a value
loaded before the key is declared is lost. The environment variable `A1580_HOST` is read by
`examples/example_test.py` (and meant for tools), not by the plug itself. Addresses other than the vendor
default stay out of git.

## What the plug does

The plug stays thin: it has no per-setting setters. Settings are data: a dict of SCPI header to value
(`apply_setup`) or a capture file on top of it (`apply_capture`).

| Method | Behaviour |
|---|---|
| `apply_setup(settings)` | For each `header: value`: write, drain the error queue, query `<header>?`, compare with `values_match`. Collects every failure (device error, no answer, read-back mismatch) and raises one `RuntimeError`. Settings after a failed one are still written. |
| `apply_capture(capture_or_path, *, reset=False)` | Loads and validates a capture file (or a `Capture`) before anything is sent, optionally calls `reset()`, then `apply_setup(capture.to_scpi())`. Returns the `Capture`. Raises `CaptureError` for an invalid file. `reset` is off by default because what `*RST` restores is unknown. |
| `acquire(n=1, *, timeout_s=5.0)` | Queries `DATA:LENG?`, `FREQ?`, `TRIG:DEL?`, `DATA:PORT?`, opens the data socket, sends `STAR AUTO`, collects `n` packets, sends `STOP`, closes the socket, drains the error queue. Returns `list[AScan]`. `ValueError` for bad arguments, `TimeoutError` (with `DATA:LENG` and port) if fewer than `n` packets arrive, `RuntimeError` if the device queued errors or a stream is running. |
| `start_stream(callback, *, timeout_s=5.0)` / `stop_stream()` | Same start as `acquire`, then a daemon thread calls `callback(ascan)` for every A-scan. `timeout_s` is how long the stream may stay silent. `stop_stream()` sends `STOP`, joins the thread, closes the socket and returns the number delivered. It re-raises a callback exception, a stall or lost connection, a failed `STOP` or queued device errors, in that order. |
| `get_state()` / `set_state(state)` | `get_state()` queries every header in `STATE_HEADERS` (27 headers) and returns `{header: reply}`. `set_state()` writes a snapshot back with `write_checked`: `TRAN:ENAB OFF` first, the other headers in `STATE_HEADERS` order, the snapshot's `TRAN:ENAB` last. `AVER:DEL:CONS` is skipped unless `AVER:DEL:CONS:AUTO` is off, because the vendor says the device rejects it otherwise (-221). Replies are written back with unit suffixes (`REPLY_UNITS` gives the reply unit of each header). |
| `check_errors()` | Drains `SYST:ERR?` (at most 20 reads) and returns the entries whose number is not 0. Called at construction, after every `apply_setup` write and at the end of every acquisition. |
| `write_checked(cmd)` | `write()` then `check_errors()`; raises `RuntimeError` naming the command if the queue had entries. |
| `reset()` | `*RST`, then drains errors. The plug never sends `*RST` on its own. |
| `tearDown()` | Never raises. Stops a running stream, sends `STOP`, then restores the snapshot (`restore_state` on) or writes `TRAN:ENAB OFF` (`restore_state` off). Closes the data socket, the SCPI resource and the resource manager. |

Also on the plug: `write(cmd)`, `query(cmd)` (strips the reply), `idn()`, `stop()`, and `identity`
(`manufacturer`, `model`, `serial`, `firmware`, parsed from `*IDN?` at construction).

What `tearDown()` leaves on the device: acquisition stopped (`STOP`), the pulser switched off while the other
settings are restored, and then the snapshot's own `TRAN:ENAB` value, so a pulser that was on when the plug was
created is on again afterwards. With `a1580_restore_state` off, the settings stay as the test left them and the
pulser is switched off. The pulser write in `tearDown` is best effort: a failure is logged, not raised.

`acquire()` returns `AScan` named tuples:

| Field | Content |
|---|---|
| `raw` | `numpy` int16 array, `DATA:LENG` samples, exactly as received |
| `header` | `AScanHeader`: `magic`, `ctp` (3 values), `length_lo`, `length_hi`, `packet_number`, `telemetry_a/b/c`, `is_full`, `buffer_fill`, `ascan_count`, `reserved_b/c` |
| `fs_hz` | the `FREQ?` reply, Hz |
| `trigger_delay_ns` | the `TRIG:DEL?` reply, taken as ns (the vendor documents disagree, see below) |
| `t` | property: `arange(n) / fs_hz` in seconds, recomputed on each access |

**There is no volts array.** The count-to-volt scaling, the ADC bit depth and the full-scale input range are not
documented anywhere, so the plug stores raw counts plus the header and the settings that give the time base, and
nothing derived. A `v` array will be added when the scaling has been measured.

## Describing settings as a readable file

`captures/*.yaml` describe the test conditions for people who do not know SCPI. `src/a1580_openhtf/capture.py` holds the typed model
that is the single source of truth for keys, choices, units and ranges. Every group and field is optional;
only what is written is sent.

```yaml
# yaml-language-server: $schema=../capture.schema.json
sampling: {rate: 100 MHz, length: 8192}
mode: master
pulser:
  enabled: true            # always written last
  voltage: 20 V
  frequency: 2.5 MHz
  periods: 1
  impedance: '200'         # text, so quote it
receiver:
  gain: 10 dB
  tgc: {mode: 'off'}       # quote off and on: YAML reads them as booleans
trigger: {mode: internal, interval: 100 ms, delay: 0 ns}
averaging: {count: 0, constant_delay: auto, random_delay: 2 us}
```

Groups: `sampling`, `mode`, `pulser`, `receiver` (with `tgc`), `trigger`, `averaging`, and `extra`, a raw SCPI
`header: argument` escape hatch for anything the model does not cover (never validated, written before the
pulser enable). Quantities are strings with a unit: `20 V`, `2.5 MHz`, `10 us`, `10 dB`, `0.1 dB/us`. A bare
number where a quantity is expected is an error that names the unit to add. `Capture.to_scpi()` gives the
ordered header-to-argument dict, `Capture.describe()` a one-line summary for logs. The same model can be built
in Python: `Capture(pulser=Pulser(voltage=Decimal(20)))`.

Mistakes are caught in four places:

| Stage | Who | Catches |
|---|---|---|
| While typing | editor with `capture.schema.json` | unknown keys, wrong choices, wrong units, ranges |
| Before commit / in CI | `python -m a1580_openhtf.capture check captures/*.yaml` | the same, plus cross-checks, without an editor |
| When the test starts | `load_capture()` (inside `apply_capture()`) | everything above, all problems at once as `<file>:<line>: <key path>: ...`, with hints; nothing is sent to the device |
| On the device | `apply_setup()` (inside `apply_capture()`) | values the device clamps or rejects, found by the read-back |

`tests/data/broken.yaml` holds deliberate mistakes for the loader and CLI tests. The schema is regenerated with
`python -m a1580_openhtf.capture schema --write` (run from the repository root). VS Code with the Red Hat YAML
extension picks up `.vscode/settings.json` and maps `capture.schema.json` to `captures/*.yaml`.

**Voltage:** `pulser.voltage` accepts 5 to 100 V in steps of 5 V (the vendor says so). Anything above 50 V is
allowed, but `check` prints a warning and `apply_capture` logs one: high voltage should be a conscious choice.
The example and `vendor_example.yaml` use 20 V, not the vendor example's 100 V.

## How acquisition works on this device

The A-scans are not available over SCPI. They stream on a second TCP connection to the port that `DATA:PORT?`
returns. The plug opens that connection first, then sends `STAR AUTO`; `STOP` ends the stream. The plug has no
single-shot mode: `acquire(n)` starts the stream, keeps `n` packets and stops.

A packet is a 28-byte header (struct `'<4s3IH2B6B2B'`, magic `FtH1`) followed by `DATA:LENG` little-endian
int16 samples, so it is `28 + 2 * DATA:LENG` bytes. TCP does not keep packet boundaries, so `stream.FrameReader`
keeps one byte buffer, drops everything before the next `FtH1` (resync, counted in `reader.stats`) and cuts
exact packets. Changing `DATA:LENG` or `FREQ` while the stream runs is not supported. See PROTOCOL.md
"Acquisition and data stream" for the layout table, the resync algorithm and the weakness of the vendor's own
`recv`-per-packet reader.

## Things the vendor material does not tell you

The vendor documents are partly inconsistent and silent on these. The plug does the safest thing for each. All
are listed in PROTOCOL.md "Unknowns to verify on hardware" with the experiment.

- **Data port.** `SCPI_COMMANDS.md` shows `DATA:PORT?` replying 5025, the vendor script starts with 2758. The
  plug queries it on every acquisition and never hard-codes it. The real value is to be measured.
- **Single-shot acquisition.** Only `STAR AUTO` is documented, although the text mentions single acquisitions.
  Packets already in flight when `STOP` is sent may be lost or may arrive late. To be measured.
- **Averaging.** Whether `AVER:COUN` is N or 2^N averages, and what the header field `ascan_count` counts, is
  not stated. The plug sends and reads back the number and interprets nothing. To be measured.
- **Header fields.** `ctp` (timing array or XYZ coordinates, the docs say both), `length_lo`/`length_hi`,
  `telemetry_a/b/c`, `is_full`, `buffer_fill` and the wrap of `packet_number` have no stated meaning. The plug
  stores them as received. To be measured.
- **Count-to-volt scaling.** ADC bit depth, full scale and whether gain and TGC act before the ADC are unknown.
  Hence no volts array. To be measured.
- **Time zero.** Whether sample 0 is the trigger, the pulse start or the end of `TRIG:DEL`, and whether
  `TRIG:DEL` is in nanoseconds (SCPI document) or samples (REST document), is unknown. `AScan.t` starts at 0
  at the first sample and nothing more is claimed. To be measured.
- **`*RST` defaults.** Not documented beyond the `DEFault` column, and it is unknown whether `*RST` stops a
  running acquisition. The plug never sends `*RST` on its own and `apply_capture` does not reset by default.
  To be measured.
- **Reply formats.** Whether enumerations and booleans come back as the echo, the short form or the long form
  (`INT` or `INTERNAL`, `ON` or `1`), and which unit a bare number gets in a command. `values_match` accepts
  the long, short and case variants, and the plug writes units explicitly. To be measured.
- **Clamping.** The vendor documents silent clamping nowhere, so every value is read back after writing.
  Out-of-range behaviour (error, clamp or replace) is to be measured.
- **Settling times** after a change of gain, pulser voltage or impedance, and changes of settings while
  streaming. The plug does not wait or retry. To be measured.

## Testing without hardware

`a1580_openhtf.fake_resource.FakeA1580Resource` stands in for both channels: the PyVISA SCPI resource and the
A-scan data socket. Pass it to the plug: `A1580Plug(resource=FakeA1580Resource())`. The plug then uses the
fake's data socket too. `examples/example_test.py --fake` does exactly that.

What it models, from the vendor documents: the SCPI header set with long and short forms, unit conversion to
the documented reply formats, the error queue with `-113` (undefined header) and `-221` (settings conflict on
`AVER:DEL:CONS` while auto is on), `*IDN?` with the vendor's example reply, `*RST` to its default table, and a
data socket that sends `28 + 2 * DATA:LENG` byte packets only between `STAR AUTO` and `STOP`.

What it invents: the signal (a damped sine burst at `TRAN:FREQ`, starting at 10 % of the record, amplitude
scaled by `GAIN`), the defaults marked `UNKNOWN default` in the source, and every error code other than -113
and -221 (for example the -102 it queues for a value that does not parse). A fake only knows what we told it;
nothing is done until it has run on the instrument, and every finding from hardware goes back into it.

Constructor options:

| Option | Effect |
|---|---|
| `length=1024` | initial `DATA:LENG` |
| `chunk=k` | the data socket returns at most `k` bytes per `recv`, to exercise reassembly |
| `garbage_prefix=b'..'` | bytes sent once before the first packet, to exercise resync |
| `signal='burst'` | `'burst'` (default), `'zeros'` (all samples 0) or `'none'` (never sends a packet: timeouts) |
| `reject={'TRAN:PULS': '-221,"Settings conflict"'}` | a write to that header queues the given error and does not store the value |
| `idn=` | the `*IDN?` reply |

`fake.log` holds every write and query, verbatim. `connections`, `started`, `start_count` and `stop_count` are
there for assertions. An unknown header in a query raises `TimeoutError`, as a real socket resource would time out.

## Files

`src/a1580_openhtf/plug.py` the plug, `AScan`, `values_match`, `STATE_HEADERS`, `REPLY_UNITS` · `stream.py` packet
layout, parser, `FrameReader` · `capture.py` typed settings model, YAML front end and CLI · `fake_resource.py`
hardware-free stand-in · `capture.schema.json` generated JSON Schema · `captures/` capture files
(`vendor_example.yaml`, 20 V) · `examples/example_test.py` minimal OpenHTF test, see `examples/README.md` ·
`tests/` pytest suite (including `tests/data/broken.yaml`) · `.vscode/settings.json` YAML schema mapping ·
`.github/workflows/ci.yml` ruff, mypy and pytest · `PROTOCOL.md` digest of the vendor material, every claim cited,
`UNKNOWN` and `CONFLICT n` kept · `SPEC.md`/`SPEC-capture.md` contracts for coder agents · `STATUS.md` current
state and open questions · `AGENTS.md`/`CLAUDE.md` rules for agents · `pyproject.toml`, `uv.lock`, `LICENSE`.

`192.168.200.18` is the address used in all vendor examples. Do not commit any other real address or serial number.

## Licence

MIT, see `LICENSE`. Use it for anything; keep the copyright notice.
