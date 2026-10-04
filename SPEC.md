# SPEC: OpenHTF plug for the ACS A1580 pulser-receiver (v1 core)

Design decision: the plug stays thin. It does **not** get one setter per instrument
setting. It opens the SCPI session, applies a dict of settings with read-back
verification, acquires A-scans from the binary data stream, and restores the instrument
in `tearDown`. Typed settings live in `SPEC-capture.md` on top of this.

**The real instrument is NOT available to you.** Do not open any network socket except
to a `localhost` listener your own test created. Everything must work and be tested with
`a1580_openhtf.fake_resource.FakeA1580Resource`.

Read `AGENTS.md`, `PROTOCOL.md` and `STATUS.md` first. `PROTOCOL.md` is the only source
for command strings, ranges, reply formats and the packet layout; when this SPEC and
`PROTOCOL.md` disagree, `PROTOCOL.md` wins and you report the difference. Do not commit.

The sibling project `/home/user/pjaako/rigol-dho-openhtf` (if present on this machine)
may be copied from and adapted; put `# Adapted from rigol-dho-openhtf <file>` at the top
of any file that is. Its `rigol_dho_plug.py`, `fake_resource.py` and `tests/test_plug.py`
are the closest models for sections 3, 4 and 5.

Facts to build on (from the vendor material, none yet verified on hardware):

- SCPI on TCP 5025, terminators `\r\n` both ways, encoding `iso-8859-1`, no reply to set
  commands, errors only via `SYST:ERR?` (`<num>,"<msg>"`, `0,"No error"` when empty).
- No data over SCPI. A-scans stream on a second TCP port returned by `DATA:PORT?`
  (vendor code default 2758). Open the data socket **before** `STAR AUTO`; `STOP` ends it.
- Packet = 28-byte header (`'<4s3IH2B6B2B'`, magic `FtH1`) + `DATA:LENG` int16 LE samples.
- `*IDN?` -> `ACS-Solutions GmbH,A1580-HF,100500,1.6.b41` (format: 4 comma fields).

## 0. Scaffold

Create these files. Keep each one minimal and conventional.

| File | Content |
|---|---|
| `pyproject.toml` | `[project]` name `a1580-openhtf`, version `0.1.0`, `requires-python = ">=3.12"`, MIT, dependencies `openhtf>=1.6`, `pyvisa>=1.14`, `pyvisa-py>=0.7`, `numpy>=1.26`, `pyyaml>=6`. Optional `dev`: `pytest`, `ruff`, `mypy`, `jsonschema`, `types-PyYAML`. Build backend `hatchling`, `[tool.hatch.build.targets.wheel] packages = ["src/a1580_openhtf"]`. `[tool.ruff]` line-length 100, target py312, `[tool.ruff.lint] select = ["E","F","I","UP","B","BLE"]`, `[tool.ruff.format] quote-style = "single"`. `[tool.mypy] strict = true, files = ["src/a1580_openhtf/stream.py", "src/a1580_openhtf/capture.py"]` (capture.py comes from SPEC-capture; list it anyway, mypy tolerates a missing file only if you configure `files` per existing module, so list `stream.py` now and let SPEC-capture add `capture.py`). `[tool.pytest.ini_options] testpaths = ["tests"]`, `addopts = "-q -p no:cacheprovider"`. |
| `LICENSE` | MIT, copyright 2026 Pavel Gromovikov. |
| `.gitignore` | `.venv/`, `__pycache__/`, `*.pyc`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, `dist/`, `*.egg-info/`, `examples/results/`, `HANDOFF.md`, `*.local.*`, `.serena/`, `opencode.*`. |
| `.github/workflows/ci.yml` | On push and pull_request: ubuntu-latest, `astral-sh/setup-uv@v5`, `uv python install 3.13`, `uv sync --extra dev`, then `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, `uv run pytest`. |
| `src/a1580_openhtf/__init__.py` | Re-exports `A1580Plug`, `AScan`, `AScanHeader`, `__version__ = '0.1.0'`. Must import without pyvisa installed. |
| `src/a1580_openhtf/py.typed` | empty |
| `tests/conftest.py` | nothing but a comment unless you need a fixture; prefer the `_plug()` helper pattern from the sibling. |
| `README.md` | Only a skeleton for now: title, one paragraph, "Status: pre-hardware, nothing measured yet", Setup block (`uv sync --extra dev`, `.venv/bin/python -m pytest`, `.venv/bin/python examples/example_test.py --fake`), and a `## Files` list. The owner will extend it. |

Install with `uv sync --extra dev` into the existing `.venv/` (it already has Python 3.13).

## 1. Package layout and public API

```
src/a1580_openhtf/
  __init__.py
  stream.py          packet layout, parser, FrameReader (pure, no I/O except the reader's recv)
  plug.py            A1580Plug(BasePlug), AScan
  fake_resource.py   FakeA1580Resource (+ FakeDataSocket)
tests/
  test_stream.py  test_plug.py  test_fake.py
examples/
  example_test.py    minimal OpenHTF test, --fake
```

Config keys, declared at import of `plug.py` with `description=`:

| Key | Default | Meaning |
|---|---|---|
| `a1580_host` | `'192.168.200.18'` | IP or hostname of the instrument (vendor factory default) |
| `a1580_scpi_port` | `5025` | SCPI port |
| `a1580_restore_state` | `True` | Restore the settings snapshot taken at construction in `tearDown()` |

## 2. `stream.py`

```python
MAGIC = b'FtH1'
HEADER_FORMAT = '<4s3IH2B6B2B'
HEADER_SIZE = 28

class AScanHeader(NamedTuple):
    magic: bytes; ctp: tuple[int, int, int]; length_lo: int; length_hi: int
    packet_number: int; telemetry_a: int; telemetry_b: int; telemetry_c: int
    is_full: int; buffer_fill: int; ascan_count: int; reserved_b: int; reserved_c: int

def packet_size(length: int) -> int                      # 28 + 2*length, ValueError if length < 1
def parse_header(buf: bytes) -> AScanHeader              # ValueError on wrong size or magic
def build_packet(samples, *, packet_number=0, **header_fields) -> bytes   # inverse, used by the fake and tests
def split_packets(buffer: bytearray, length: int) -> list[bytes]
    # the vendor resync algorithm (PROTOCOL.md "Resynchronisation algorithm"), consumes from
    # `buffer` in place, returns complete packets, keeps any tail. Garbage before a magic is dropped.
class FrameReader:
    def __init__(self, sock, length: int)                # sock has recv(n), settimeout(s)
    def read(self, n: int, timeout_s: float) -> list[bytes]
    # Collect n complete packets. Raise TimeoutError('...') with the count so far if the
    # deadline passes. Empty recv (peer closed) raises ConnectionError.
    stats: packets, dropped_bytes, resyncs (plain ints)
```

Parsing uses `struct`; samples are `numpy.frombuffer(..., dtype='<i2')` done by the caller
(`AScan.from_packet`). `stream.py` must stay `mypy --strict` clean and import only stdlib.

## 3. `plug.py`

```python
class AScan(NamedTuple):
    raw: np.ndarray          # int16, length DATA:LENG
    header: AScanHeader
    fs_hz: float             # FREQ? reply
    trigger_delay_ns: int    # TRIG:DEL? reply
    @property t(self) -> np.ndarray      # seconds, arange(n)/fs_hz, float64, recomputed
    @classmethod from_packet(cls, packet: bytes, fs_hz, trigger_delay_ns) -> AScan
```

No `v` array: scaling is UNKNOWN. Say so in the docstring.

```python
class A1580Plug(BasePlug):
    auto_placeholder = True
    def __init__(self, resource=None, data_socket_factory=None, host=None,
                 restore_state=None):
```

- `resource` injected: use it as the SCPI resource, import nothing from pyvisa.
  Otherwise lazy `import pyvisa`, `pyvisa.ResourceManager('@py')`,
  `open_resource(f'TCPIP::{host}::{port}::SOCKET')`, then set `encoding='iso-8859-1'`,
  `timeout=5000`, `read_termination='\r\n'`, `write_termination='\r\n'`.
  `host` defaults to `CONF.a1580_host`, port to `CONF.a1580_scpi_port`.
- `data_socket_factory(host, port) -> socket-like` injected: used for the data channel.
  Otherwise `socket.create_connection((host, port), timeout=5)`. When a fake resource is
  injected and no factory is given, use `resource.data_socket_factory` if it has one.
- On construction: drain the error queue (`check_errors()`, discard), `idn()` once and keep
  it in `self.identity` (a NamedTuple `manufacturer, model, serial, firmware`; raise
  `RuntimeError` naming the reply if it does not have 4 comma fields), then snapshot
  `self._initial_state = self.get_state()` if restore is enabled. Never send `*RST`
  implicitly.

Methods (SCPI strings exactly as in `PROTOCOL.md`; long or short form, your choice, but
be consistent and use the vendor example's forms where it has one):

| Method | Behaviour |
|---|---|
| `write(cmd)` / `query(cmd) -> str` | thin; `query` strips the reply |
| `idn() -> Identity` | `*IDN?` |
| `check_errors() -> list[str]` | drain `SYST:ERR?` until the number is 0, at most 20 reads; return the non-zero entries |
| `write_checked(cmd)` | write, then `check_errors()`; raise `RuntimeError(f'{cmd!r}: {errors}')` if any |
| `reset()` | `*RST`, then `check_errors()`; invalidates cached length/fs |
| `apply_capture(capture_or_path, *, reset=False) -> Capture` | delegates to `capture.apply_capture` (SPEC-capture) |
| `apply_setup(settings: Mapping[str, object]) -> None` | before anything is written: every header must normalise (section 3.1) and `TRAN:PULS` must not be set by keyword (`MIN`/`MAX`/`DEF`/`UP`/`DOWN`), else `ValueError`. Then for each key in order, inside one try/except per key: write, `check_errors()`, `query(f'{key}?')`, compare with `values_match`. Any exception or mismatch becomes a failure entry and later keys are still applied; after a transport exception the read buffer is flushed (best effort). One `RuntimeError` listing all failures at the end. |
| `get_state() -> dict[str, str]` | query every header in `STATE_HEADERS` (section 3.2), return `{header: reply}`. A header that raises is logged, its queued error drained, and omitted. After 3 consecutive transport errors the rest is skipped (dead link). |
| `set_state(state)` | `TRAN:ENAB OFF` first (unchecked, before any drain), then the other headers in `STATE_HEADERS` order with `write_checked`, failures collected, empty values skipped; `AVER:DEL:CONS` only when `AVER:DEL:CONS:AUTO` is `OFF`/`0`; always ends with a checked `TRAN:ENAB OFF` (a snapshot with the pulser ON is never re-enabled, a warning says so); one `RuntimeError` at the end if anything failed. Dead-link abort as in `get_state`. |
| `stop()` | `STOP` |
| `acquire(n=1, *, timeout_s=None) -> list[AScan]` | section 3.3; `None` means `max(5, 2*n*TRIG:INT + 2)` seconds |
| `start_stream(callback, *, timeout_s=5.0)` / `stop_stream()` | section 3.4 |
| `tearDown()` | first action, before anything else and unchecked: `TRAN:ENAB OFF`; then `stop_stream()` if running (swallow errors), `STOP`, then restore state if enabled, otherwise a checked `TRAN:ENAB OFF` (all best effort: try/except, `self.logger.warning`, never raise), close the data socket if open, close the resource, close the resource manager if we made one. The device is never left with the pulser on. |

### 3.1 `values_match(header, sent, reply) -> bool` (module-level function)

- Headers must normalise against an explicit node table (short and long form of every mnemonic in `PROTOCOL.md`); an unknown node raises `ValueError`. `sent == ''` never matches.
- Booleans: `ON`/`OFF`/`1`/`0` in either position match by truth value, only for `BOOLEAN_HEADERS` (`TRAN:ENAB`, `TRAN:REV`, `TRAN:DAMP`, `GAIN:PRE:COMB`, `GAIN:PRE:SPLIT`, `AVER:DEL:CONS:AUTO`).
- Enumerations (alphabetic tokens of 3+ letters only, never numbers): match if equal
  case-insensitively, or `reply.upper()` equals the capitals of the sent mnemonic
  (`INTernal` -> `INT`), or the shorter token (3 or 4 letters) is a prefix of the longer
  (`INT` vs `INTERNAL`). `LINear` vs `LINEARX` is False.
- Numbers with an optional unit suffix in `sent` (`100 MHZ`, `2500 KHz`, `100000 US`,
  `0 NS`, `100 V`, `10 DB`): convert to the reply's base unit using `REPLY_UNITS`
  (`FREQ`, `TRAN:FREQ` -> Hz; `TRIG:INT`, `AVER:DEL:CONS`, `AVER:DEL:RAND` -> s;
  `TRIG:DEL`, `TRAN:GAP`, `TRAN:DAMP:GAP` -> ns; `TRAN:PULS` -> V; `GAIN` -> dB;
  unit-less otherwise) and compare with `math.isclose(rel_tol=1e-6, abs_tol=1e-12)`;
  headers whose reply is an integer (`FREQ`, `TRAN:FREQ`, `TRIG:DEL`, `TRAN:GAP`,
  `TRAN:DAMP:GAP`, `DATA:LENG`, `TRAN:PULS`, `AVER:COUN`, `FILT:HPAS:IND`) use
  `abs_tol=0.5` in reply units instead, so a clamp of one LSB is a mismatch.
  Header lookup must accept long and short forms and an optional leading `SOUR:`/`SENS:`
  (normalise the header first: upper-case, strip the optional roots, keep only the
  capital letters of each mnemonic).
- Lists (`GAIN:TGC:LIN`, `GAIN:TGC:ARB`): split on commas, compare element-wise as numbers.
- `MIN`/`MAX`/`DEF`/`UP`/`DOWN` as `sent`: cannot be verified; return True and let the
  caller log it.

### 3.2 `STATE_HEADERS`

In this order: `FREQ`, `DATA:LENG`, `MODE`, `TRAN:TYPE`, `TRAN:REV`, `TRAN:PULS`,
`TRAN:FREQ`, `TRAN:DUR`, `TRAN:GAP`, `TRAN:DAMP`, `TRAN:DAMP:GAP`, `TRAN:IMP`,
`TRAN:ENAB`, `TRIG:MODE`, `TRIG:INT`, `TRIG:DEL`, `GAIN`, `GAIN:PRE:COMB`,
`GAIN:PRE:SPLIT`, `GAIN:TGC:LIN`, `GAIN:TGC:ARB`, `GAIN:TGC:MODE`, `AVER:COUN`,
`AVER:DEL:CONS:AUTO`, `AVER:DEL:CONS`, `AVER:DEL:RAND`, `FILT:HPAS:IND`.
`set_state` switches the pulser off before any other write and restores `TRAN:ENAB` last,
so the pulser is off while amplitude, frequency and impedance change. If the device's real short forms differ, that is a hardware finding; keep
the list in one place.

### 3.3 `acquire(n, timeout_s)`

1. `length` from `DATA:LENG?` (must be an integral number, else `RuntimeError` naming the
   reply), `fs = float(query('FREQ?'))`, `delay = int(round(float(query('TRIG:DEL?'))))`
   (warning if not integral, see CONFLICT 4), `port = int(query('DATA:PORT?'))`; if `port`
   equals the SCPI port raise `RuntimeError` citing CONFLICT 1 before opening anything.
2. Open the data socket via the factory. Create `FrameReader(sock, length)`.
3. `write('STAR AUTO')`.
4. `reader.read(n, timeout_s)`.
5. In `finally`: `write('STOP')`, close the socket, log `reader.stats` (warning if bytes
   were dropped or packets misaligned), then `check_errors()`; device errors found here are
   logged as warnings and the packets are still returned (a failed `STOP` still raises).
6. Return `[AScan.from_packet(p, fs, delay) for p in packets]`.
`ValueError` if `n < 1` or `timeout_s <= 0`. A `TimeoutError` or `ConnectionError` from the
reader propagates after the `finally` cleanup, with its message extended by
`'(DATA:LENG=..., port=...)'`.

### 3.4 `start_stream(callback, timeout_s)` / `stop_stream()`

Same steps 1 to 3, then a daemon thread loops `reader.read(1, timeout_s)` and calls
`callback(ascan)` until `stop_stream()` sets an `Event`. `stop_stream()` sets the event,
sends `STOP`, joins the thread (timeout `timeout_s + 1`), closes the socket, returns the
number of A-scans delivered. A callback exception is logged (`self.logger.exception`) and
stops the stream; `stop_stream()` re-raises it. `start_stream` while running raises
`RuntimeError`. `acquire()` while streaming raises `RuntimeError`. Per-stream state lives
in one `_Stream` object; a thread that does not stop in time leaves a zombie marker that
blocks a new stream until it dies. A device-side close ends the thread and is reported as
"the stream has ended". `write`/`query` hold an `RLock`, so a callback may query.

## 4. `fake_resource.py`

`FakeA1580Resource(*, reject=None, length=1024, idn='ACS-Solutions GmbH,A1580-HF,100500,1.6.b41', chunk=None, garbage_prefix=b'', signal='burst')`

- No pyvisa import. Attributes `log: list[str]` (every write and query, verbatim),
  `closed`, `timeout`, `encoding`, `read_termination`, `write_termination`.
- `DEFAULTS: dict[str, str]` keyed by **normalised** header (section 3.1 normalisation),
  values in the device's reply format from `PROTOCOL.md` (e.g. `FREQ` -> `100000000`,
  `TRAN:PULS` -> `20`, `TRIG:INT` -> `10.0E-3`, `TRIG:DEL` -> `15000`, `TRAN:ENAB` -> `OFF`,
  `TRAN:DAMP` -> `0`, `GAIN:TGC:MODE` -> `OFF`, `AVER:COUN` -> `0`, `MODE` -> `MASTER`,
  `TRIG:MODE` -> `INT`, `DATA:LENG` -> `1024`, `DATA:PORT` -> `2758`). Where the default is
  UNKNOWN in `PROTOCOL.md`, pick the vendor example's value and mark it `# UNKNOWN default`.
- `write('<hdr> <val>')`: normalise the header; unknown header -> queue
  `-113,"Undefined header;Command: <cmd>"` and ignore; known header -> store the value
  converted to the reply format (numbers with units converted to the base unit from
  `REPLY_UNITS`, booleans to the per-header style, enums to the short upper form).
  Special headers: `STAR AUTO` starts the data generator, `STOP` stops it, `*RST` restores
  `DEFAULTS`, `*CLS` clears the error queue, `MEM:CLE` no-op, `AVER:DEL:CONS` while
  `AVER:DEL:CONS:AUTO` is on -> queue `-221,"Settings conflict"` and keep the old value.
- `query('<hdr>?')`: `*IDN?` -> `idn`; `SYST:ERR?` pops the queue or `0,"No error"`;
  `*OPC?` -> `1`; `*TST?` -> `0`; known header -> stored value; unknown header -> queue
  `-113` and raise `TimeoutError` like a real socket resource would (`pyvisa` raises
  `VisaIOError`; the fake raises `TimeoutError`; the plug must not depend on the type).
- `reject={normalised_header: '<num>,"<msg>"'}`: a write to that header queues the error
  and does not store the value.
- `data_socket_factory(host, port) -> FakeDataSocket`. Records `(host, port)` in
  `connections`. The socket has `recv(n)`, `settimeout(s)`, `close()`, `shutdown(how)`.
  Before `STAR AUTO` and after `STOP`, `recv` raises `socket.timeout`. While running it
  returns bytes of synthetic packets: `DATA:LENG` int16 samples of a damped sine burst at
  `TRAN:FREQ` sampled at `FREQ`, starting at 10 % of the record, amplitude scaled by
  `GAIN` (deterministic, `numpy`), `packet_number` incrementing and wrapping at 256,
  `ascan_count` = `AVER:COUN`. `chunk=k` makes `recv` return at most `k` bytes at a time
  (to exercise resync); `garbage_prefix` is sent once before the first packet.
  `signal='zeros'` gives all-zero samples. The generator must not sleep.
- `connect_count`, `started: bool`, `start_count`, `stop_count` for assertions.

## 5. Tests (`tests/`, pytest, no hardware, numbered banners mirroring this section)

`test_stream.py`
1. `packet_size`, `parse_header` round-trip with `build_packet`; wrong magic and short
   buffer raise `ValueError`.
2. `split_packets`: exact packets; two packets in one chunk; one packet split across
   three chunks; garbage before the first magic is dropped and counted; a stream with no
   magic longer than 2 packets keeps only the last `packet_size` bytes.
3. `FrameReader.read` against a stub socket returning scripted chunks: collects n;
   timeout raises `TimeoutError` with the count so far; empty recv raises `ConnectionError`.
4. `FrameReader` against a real `localhost` TCP server thread sending 5 packets in
   odd-sized pieces (this is the only socket test allowed).

`test_plug.py`
1. Construction with a fake: `identity` parsed; the error queue was drained; no `*RST`
   in `fake.log`; a malformed `*IDN?` raises `RuntimeError`.
2. `apply_setup` with the vendor example's settings (`PROTOCOL.md` "Vendor example call
   sequence"): every write is followed by `SYST:ERR?` and a read-back query, in order.
3. `apply_setup` failures: a rejected header; a value the fake stores differently
   (monkeypatch the fake to clamp `TRAN:PULS` at 100) -> one `RuntimeError` naming both.
4. `values_match` parametrised: `('FREQ', '100 MHZ', '100000000')` True,
   `('TRIG:INT', '100000 US', '100.0E-3')` True, `('TRAN:ENAB', 'ON', '1')` True,
   `('TRIG:MODE', 'INTERNAL', 'INT')` True, `('GAIN:TGC:MODE', 'LINear', 'LINEAR')` True,
   `('TRAN:PULS', '100 V', '95')` False, `('GAIN:TGC:LIN', '20.0, 0.1', '20,0.1')` True.
5. `get_state` / `set_state` round trip; `AVER:DEL:CONS` skipped while auto is ON;
   `restore_state=True` snapshot at construction and restore in `tearDown`, including the
   `STOP`; `restore_state=False` has none of it; a failing restore is logged, not raised.
6. `acquire(3)`: order in the fake is `DATA:LENG?`, `FREQ?`, `TRIG:DEL?`, `DATA:PORT?`,
   socket connected to `(host, 2758)`, then `STAR AUTO`, then `STOP`, then `SYST:ERR?`;
   three `AScan`s with `raw.dtype == int16`, `len(raw) == length`, `t[1] == 1/fs`,
   consecutive `packet_number`s. Also with `chunk=7` and with `garbage_prefix=b'xx'`.
7. `acquire` timeout: fake constructed with `signal='none'` (never produces packets, `recv`
   raises `socket.timeout`) -> `TimeoutError`, and `STOP` was still sent.
8. `start_stream` delivers at least 5 A-scans to the callback within 1 s;
   `stop_stream` returns the count; `acquire` during streaming raises; a raising callback
   surfaces from `stop_stream`.
9. OpenHTF integration: a phase decorated with `@htf.plug(pr=A1580Plug)` through
   `with_plugs(pr=FakePlug)` runs with `htf.Test(phase).execute(test_start=lambda: 'dut1')`
   returning True, measuring `num_points` and `peak_counts`.
10. Config keys exist with the documented defaults; `A1580Plug(resource=fake, host='x')`
    passes `'x'` to the data socket factory.

`test_fake.py`: unknown header queues `-113` and query raises; `*RST` restores defaults;
`-221` on constant delay while auto; `reject` works; generator honours `DATA:LENG` and
`chunk`.

Also a subprocess test: `examples/example_test.py --fake` exits 0 and prints
`num_points=`.

## 6. `examples/example_test.py`

Mirror the sibling's `example_test.py`: one phase `acquire_ascan` with
`@htf.measures(htf.Measurement('num_points'), htf.Measurement('peak_counts'))`,
`@htf.plug(pr=A1580Plug)`, `pr.apply_setup(EXAMPLE_SETUP)` (the vendor example's values,
pulser at **20 V not 100 V** for safety until the owner says otherwise), `pr.acquire(1)`,
print `num_points=... peak_counts=...`. `--fake` swaps in `FakePlug`, `--host` overrides
`a1580_host`. `sys.exit(0 if passed else 1)`.

## 7. Done means

```
.venv/bin/python -m pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy
.venv/bin/python examples/example_test.py --fake
.venv/bin/python -c "import a1580_openhtf"      # with pyvisa uninstalled it must still work; test it by monkeypatching sys.modules['pyvisa']=None in a test
```

All green. `git status --short` shows only: `pyproject.toml`, `uv.lock`, `LICENSE`,
`.gitignore`, `.github/`, `README.md`, `src/`, `tests/`, `examples/`.

Report: files created; the final pytest summary line verbatim; the ruff and mypy output;
every place where this SPEC or `PROTOCOL.md` was wrong, ambiguous, or where you deviated
and why; what you could not test without hardware; bugs noticed but not touched.
