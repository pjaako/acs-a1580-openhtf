# a1580-openhtf

OpenHTF plug for the ACS A1580 ultrasonic pulser-receiver. Ethernet only: SCPI on TCP 5025 plus a binary
A-scan stream on a second TCP port. Built on PyVISA + pyvisa-py for SCPI and a plain socket for the stream.
Sibling of [rigol-dho-openhtf](https://github.com/pjaako/rigol-dho-openhtf): same concept, different instrument.

**Status: hardware phases A, B and C are done (2026-10-05, firmware 1.16 (861f022a), one A1580-HF): read-only
queries, the error queue, acquisition with the pulser off and nothing connected, and the pulser-on experiments 10,
10b and 11 with one pair of 50 kHz transducers face to face. The results are in "Measured on the device" below;
whatever is not listed there still comes from the vendor material (see PROTOCOL.md). `*RST` was measured too
(experiment 13): on this firmware it changes no setting. All experiments of the first hardware session are done except
count-to-volt (experiment 12, deferred).**

**Safety: after power-on this firmware has the pulser ENABLED at 20 V (`TRAN:ENAB?` -> `1`) until `TRAN:ENAB OFF` is
sent, so the first `STAR AUTO` fires it. Whether it already emits pulses before any `STAR AUTO` is not measured, so
treat the OUT connector as live. Settings are volatile and a power cycle brings that state back (measured 2026-10-05, fw 1.16). Switch the
pulser off before connecting anything to the OUT connector. The plug's `tearDown` and `tools/hw_probe.py` do this,
the device itself does not. After power-on set `DATA:LENG` to a value from 1024 to 36864 before acquiring: the
power-on value 114688 gives partly invalid packets (entry 13 below).**

```python
import openhtf as htf
from a1580_openhtf import A1580Plug


@htf.measures(htf.Measurement('num_points'), htf.Measurement('peak_counts'))
@htf.plug(pr=A1580Plug)
def phase(test, pr):
    pr.apply_capture(
        'captures/vendor_example.yaml'
    )  # validated, written, read back, errors drained
    scans = pr.acquire(4)  # four A-scans, then STOP
    test.measurements.num_points = len(scans[0].raw)  # raw int16 counts, no volts
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
| `apply_capture(capture_or_path, *, reset=False)` | Loads and validates a capture file (or a `Capture`) before anything is sent, optionally calls `reset()`, then `apply_setup(capture.to_scpi())`. Returns the `Capture`. Raises `CaptureError` for an invalid file. `reset` is off by default because `*RST` resets nothing on fw 1.16 (entry 18). |
| `acquire(n=1, *, timeout_s=5.0)` | Queries `DATA:LENG?`, `FREQ?`, `TRIG:DEL?`, `DATA:PORT?`, opens the data socket, sends `STAR AUTO`, collects `n` packets, sends `STOP`, closes the socket, drains the error queue. Returns `list[AScan]`. `ValueError` for bad arguments, `TimeoutError` (with `DATA:LENG` and port) if fewer than `n` packets arrive, `RuntimeError` if the device queued errors or a stream is running. |
| `start_stream(callback, *, timeout_s=5.0)` / `stop_stream()` | Same start as `acquire`, then a daemon thread calls `callback(ascan)` for every A-scan. `timeout_s` is how long the stream may stay silent. `stop_stream()` sends `STOP`, joins the thread, closes the socket and returns the number delivered. It re-raises a callback exception, a stall or lost connection, a failed `STOP` or queued device errors, in that order. |
| `get_state()` / `set_state(state)` | `get_state()` queries every header in `STATE_HEADERS` (27 headers) and returns `{header: reply}`. `set_state()` writes a snapshot back with `write_checked`: `TRAN:ENAB OFF` first, the other headers in `STATE_HEADERS` order, the snapshot's `TRAN:ENAB` last. `AVER:DEL:CONS` is skipped unless `AVER:DEL:CONS:AUTO` is off, because the vendor says the device rejects it otherwise (-221). Replies are written back with unit suffixes (`REPLY_UNITS` gives the reply unit of each header). |
| `check_errors()` | Drains `SYST:ERR?` (at most 20 reads) and returns the entries whose number is not 0. Called at construction, after every `apply_setup` write and at the end of every acquisition. |
| `write(cmd)`, `query(cmd)` | Every line the plug sends goes through these two. A line that is longer than 255 bytes with its CRLF raises `ValueError` (header, length, limit) before anything is sent, because the device discards such a line with `-363` (entry 5). `apply_setup` and `set_state` report it as a failure of that key and go on with the others; `capture check` reports an over-long `GAIN:TGC:ARB` line before anything is sent. |
| `write_checked(cmd)` | `write()` then `check_errors()`; raises `RuntimeError` naming the command if the queue had entries. |
| `reset()` | `*RST`, then drains errors. The plug never sends `*RST` on its own. |
| `tearDown()` | Never raises. Stops a running stream, sends `STOP`, then restores the snapshot (`restore_state` on) or writes `TRAN:ENAB OFF` (`restore_state` off). Closes the data socket, the SCPI resource and the resource manager. |

Also on the plug: `write(cmd)`, `query(cmd)` (strips the reply), `idn()`, `stop()`, and `identity`
(`manufacturer`, `model`, `serial`, `firmware`, parsed from `*IDN?` at construction).

What `tearDown()` leaves on the device: acquisition stopped (`STOP`), the pulser switched off while the other
settings are restored, and then `TRAN:ENAB OFF` once more, so the device ends with the snapshot restored and the
pulser off. With `a1580_restore_state` off, the settings stay as the test left them and the pulser is switched off. The pulser write in `tearDown` is best effort: a failure is logged, not raised.

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

**One SCPI client at a time.** As soon as a second connection to port 5025 is accepted, the device closes the first
one (measured 2026-10-05, fw 1.16). Any second client (another script, a monitoring tool) closes the running one. The
data port is not affected.

A packet is a 28-byte header (struct `'<4s3IH2B6B2B'`, magic `FtH1`) followed by `DATA:LENG` little-endian
int16 samples, so it is `28 + 2 * DATA:LENG` bytes. TCP does not keep packet boundaries, so `stream.FrameReader`
keeps one byte buffer, drops everything before the next `FtH1` (resync, counted in `reader.stats`) and cuts
exact packets. Changing `DATA:LENG` or `FREQ` while the stream runs is not supported. See PROTOCOL.md
"Acquisition and data stream" for the layout table, the resync algorithm and the weakness of the vendor's own
`recv`-per-packet reader.

## Measured on the device (2026-10-05, firmware 1.16 (861f022a))

Phases A and B of `HARDWARE-SESSION.md` (A: read-only queries, pulser not touched by the tool; B: acquisition), on one
A1580-HF. Lines were
sent to TCP 5025 with `\r\n`; replies are shown without the trailing `\r\n`. The serial number is written
`<serial>`. Phase B (entries 6 to 9 and later) ran with the pulser off, nothing connected to IN/OUT, `DATA:LENG 1024`,
`FREQ 100 MHZ`, `TRIG:MODE INT`, `TRIG:INT 10 MS`, data port 2758. The same facts are kept as tickets for the vendor in
`vendor-tickets/`: `VENDOR-ISSUES.md` (entries A1 to A13, B1 to B9, C1 to C6) and nine issue drafts made from it.

1. **Identity.** `*IDN?` -> `ACS-Solutions GmbH,A1580-HF,<serial>,1.16 (861f022a)` (the firmware field contains a
   space and brackets), `SYST:VERS?` -> `1999.0`, `SYST:ERR:COUN?` -> `0`. measured 2026-10-05, fw 1.16
2. **Every `STATE_HEADERS` entry, plus `DATA:PORT?`, answered.** Replies as found (not defaults; the pulser was on):
   `FREQ?` -> `100000000`, `DATA:LENG?` -> `114688`, `MODE?` -> `MASTer`, `TRAN:TYPE?` -> `DUAL`, `TRAN:REV?` -> `0`,
   `TRAN:PULS?` -> `20`, `TRAN:FREQ?` -> `5000000`, `TRAN:DUR?` -> `1`, `TRAN:GAP?` -> `5`, `TRAN:DAMP?` -> `0`,
   `TRAN:DAMP:GAP?` -> `10`, `TRAN:IMP?` -> `HIGH`, `TRAN:ENAB?` -> `1` (and `0` after `TRAN:ENAB OFF`),
   `TRIG:MODE?` -> `INTernal`, `TRIG:INT?` -> `1`, `TRIG:DEL?` -> `15000`, `GAIN?` -> `0`, `GAIN:PRE:COMB?` -> `0`,
   `GAIN:PRE:SPLIT?` -> `0`, `GAIN:TGC:LIN?` -> `0,0`, `GAIN:TGC:ARB?` -> an empty line, `GAIN:TGC:MODE?` -> `OFF`,
   `AVER:COUN?` -> `0`, `AVER:DEL:CONS:AUTO?` -> `1`, `AVER:DEL:CONS?` -> `0.99992925`, `AVER:DEL:RAND?` -> `2e-06`,
   `FILT:HPAS:IND?` -> `0`. Booleans are `0`/`1`; enumerations are the vendor's mixed-case notation string, whatever
   form the client wrote (`MASTer`, `INTernal`); times are the shortest decimal with a lower-case `e` and no `.0`.
   measured 2026-10-05, fw 1.16
3. **`DATA:PORT?`** -> `2758`, the same in three sessions that day. measured 2026-10-05, fw 1.16
4. **Terminator.** A query ended with a bare `\n` is answered; the reply still ends `\r\n`. measured 2026-10-05,
   fw 1.16
5. **Error queue.** `ZZZ:NOPE 1` then `SYST:ERR?` twice -> `-113,"Undefined header;ZZZ:NOPE 1"`, then `0,"No error"`
   (the whole line as sent, no `Command: ` prefix, no space after the comma of the empty reply). Depth: 40 undefined
   headers sent one at a time with `SYST:ERR:COUN?` after each: the count went 1 to 16, then 17 and stayed 17;
   draining gave 16 entries `-113,"Undefined header;<line>"` (the first 16 sent) and, as 17th and last, `-350,"Queue
   overflow"`; later errors are dropped. Input buffer: 25 lines `ZZZ:NOPE01` to `ZZZ:NOPE25` written back to back
   without a read (about 320 bytes), then `SYST:ERR:COUN?`: the first line was processed, the queue got
   `-363,"Input buffer overrun"`, the rest was discarded and the query was never answered (5 s timeout); the connection
   stayed usable. Bursts of 2, 3, 5 and 10 undefined 16-byte lines written with no gap and no read were all processed
   and the following `SYST:ERR:COUN?` answered, so this is an input-buffer size limit, not a pacing problem.
   Single line, exact: one line of L bytes in total (terminator `\r\n` included) followed by `SYST:ERR?`: for L = 100,
   150, 200, 250, 254 and 255 the line was parsed (`-113,"Undefined header;..."` for the undefined test header); for
   L = 256, 257, 258, 260, 300, 400 and 600 the queue got `-363,"Input buffer overrun"` and the line was discarded. So the
   buffer is 256 bytes and the longest accepted line is 253 characters plus `\r\n` (255 bytes). The text of a `-113` entry
   is cut off at 262 characters. A query sent 0.3 s after an overrun was answered normally. measured 2026-10-05, fw 1.16
6. **Acquisition, `AVER:COUN 0`** (pulser off, `DATA:LENG 1024`, `FREQ 100 MHZ`, `TRIG:MODE INT`, `TRIG:INT 10 MS`).
   10 packets, interval 10.0 ms. Each packet 2076 bytes (28 + 2 * 1024), arrived as one `recv` chunk.
   `packet_number` increments by 1; the first packet after `STAR AUTO` had number 2 and the numbering went on across
   `STOP`/`STAR AUTO` (2 to 11, then 12 to 21 in the next acquisition). Header: `length_lo` 1040, `length_hi` 0 (not
   1024), `ascan_count` 1, `buffer_fill` 0, `is_full` 0, telemetry bytes 120, 86, 52. Samples between -2 and 25
   counts, standard deviation 3.59 counts, so a DC offset of about +11 counts on an open input. measured 2026-10-05,
   fw 1.16
7. **Acquisition, `AVER:COUN 4`.** Same header values, `ascan_count` still 1, interval still 10.0 ms; samples 7 to 14,
   standard deviation 0.94 counts. Ratio 0.26, close to 1/4: a mean over 2^4 = 16 acquisitions (the count is an
   exponent), assuming uncorrelated noise. measured 2026-10-05, fw 1.16
8. **`STOP` and the data socket.** 8a: `STOP` with the data socket open: two more packets within 20 ms, then idle; the
   device does not close the socket; error queue empty. 8b: a data socket connected after `STAR AUTO` receives
   packets (the first after 20 ms). Both orders work. measured 2026-10-05, fw 1.16
9. **Change while streaming.** `GAIN 6` during `STAR AUTO`: no error, `GAIN?` -> `6`, the stream goes on with the same
   packet size. The data connection does not disturb the SCPI connection (five data connections in one SCPI
   session), unlike a second connection to port 5025. measured 2026-10-05, fw 1.16
10. **Power cycle.** All settings came back to the values first found, `TRAN:ENAB?` -> `1` although `0` had been set
    before the power cycle. Power-on state: pulser on at 20 V, `TRAN:TYPE DUAL`, `TRIG:INT` 1 s, `DATA:LENG` 114688.
    Settings are volatile. measured 2026-10-05, fw 1.16
11. **Restore.** The device accepted these writes without error and read them back equal: `FREQ 100 MHZ`, `MODE
    MASTer`, `TRAN:TYPE DUAL`, `TRAN:REV 0`, `TRAN:FREQ 5000 KHZ`, `TRAN:DUR 1`, `TRAN:GAP 5 NS`, `TRAN:DAMP 0`,
    `TRAN:DAMP:GAP 10 NS`, `TRAN:IMP HIGH`, `TRIG:MODE INTernal`, `TRIG:INT 1000000 US` (reads back `1`), `TRIG:DEL
    15000 NS`, `GAIN 0`, `GAIN:PRE:COMB 0`, `GAIN:PRE:SPLIT 0`, `GAIN:TGC:LIN 0,0`, `GAIN:TGC:MODE OFF`, `AVER:COUN 0`,
    `AVER:DEL:CONS:AUTO 1`, `AVER:DEL:RAND 2000 NS` (reads back `2e-06`), `FILT:HPAS:IND 0`. So the plug's unit tables
    are confirmed for these headers. The one failure: `DATA:LENG 114688` (the power-on value) ->
    `-224,"Illegal parameter value"`, the value stays 1024; the limits are in entry 12. A plug restore of
    a snapshot taken after power-on therefore fails for `DATA:LENG` (`set_state` reports it and goes on).
    measured 2026-10-05, fw 1.16
12. **`DATA:LENG` limits and the header length field** (pulser off, nothing connected, `TRIG:MODE INT`).
    Limits: `DATA:LENG MAX` -> `DATA:LENG?` `36864`; `MIN` -> `1024`; `DEF` -> `1024`; `36864` accepted; `36865`,
    `65536`, `114688` and `1023` each -> `-224,"Illegal parameter value"` and the previous value is kept (no
    clamping); `1025` accepted (reads `1025`), so there is no block-size rule. `TRIG:INT 10 MS` -> `TRIG:INT?`
    `0.01`. With `AVER:DEL:CONS:AUTO` on, `AVER:DEL:CONS?` is `0.99992925` at `TRIG:INT` 1 s and `0.00992925` at
    10 ms (both `AVER:COUN` 0), and `0.00242925` at 10 ms with `AVER:COUN 2` (entry 18): `TRIG:INT / 2^AVER:COUN`
    minus 70.75 us (at `FREQ` 100 MHz only). Header length field: `DATA:LENG`
    1024 / 2048 / 8192 / 36864 -> packet 2076 / 4124 / 16412 / 73756 bytes, `length_lo` 1040 / 2064 / 8208 / 36880,
    `length_hi` 0; at the power-on length 114688 -> packet 229404 bytes, `length_lo` 49168, `length_hi` 1, i.e. the
    24-bit value 114704 = 114688 + 16: the pair is a 24-bit "samples + 16" (the carry into `length_hi` is measured).
    Header `ctp`: `ctp[0]` equals `packet_number` (35, 36, ... 112 seen, and 2, 3, 4 right after power-on), `ctp[1]` =
    `ctp[2]` = 0; reserved bytes 0; telemetry 120, 86, 52; `is_full` 0, `buffer_fill` 0, `ascan_count` 1 in every
    packet. Throughput: at `DATA:LENG 36864` and `TRIG:INT 10 MS` 25 packets in 0.25 s (7.4 MB/s) with no error.
    measured 2026-10-05, fw 1.16
13. **Stream at the power-on length.** After a power cycle `DATA:LENG?` -> `114688`; with `TRAN:ENAB OFF`,
    `TRIG:INT 10 MS` and `STAR AUTO` the device sent 100 packets per second of 229404 bytes (22.9 MB/s). Only part of
    each packet is real data: samples 81906 to 114687 are exactly 0 (81906 = 5 * 16384 - 14), and a long stretch of
    one constant value (30 counts) lies inside the first 65522 samples, starting at a different index in every
    packet (about 41000 to 57000) and always ending at sample 65521; elsewhere the usual noise (mean 11, std 3.6
    counts). So acquisitions at the power-on length are partly invalid until `DATA:LENG` is set to a legal value.
    measured 2026-10-05, fw 1.16

14. **Phase C, experiment 10: where is the signal (pulser on).** Bench: two single-element 50 kHz transducers, one on
    OUT, one on IN, pressed face to face (this and entries 15 to 17 rest on this one bench setup). Settings:
    `DATA:LENG 8192`, `FREQ 1 MHZ`, `TRIG:MODE INT`, `TRIG:INT 100 MS`, `TRAN:TYPE DUAL`, `TRAN:FREQ 50 KHZ`,
    `TRAN:DUR 1`, `TRAN:IMP HIGH`, `GAIN:TGC:MODE OFF`, `AVER:COUN 0`, `FILT:HPAS:IND 0`, `TRIG:DEL 0 NS`, `TRAN:PULS`
    20 V (the power-on value, not written). All read back (`FREQ?` -> `1000000`, `TRIG:INT?` -> `0.1`, `TRAN:FREQ?`
    -> `50000`, `TRIG:MODE?` -> `INTernal`, `TRIG:DEL?` -> `0`; `TRAN:ENAB ON` reads back `1`, `OFF` reads `0`).
    Run 1, `GAIN 0`: baseline (pulser off) mean 4.65 counts, std 3.63 (at `FREQ 100 MHZ` the mean had been about 11).
    Pulser on: a repeatable wavelet of only +-15 to 18 counts in samples 11 to 40 (visible in the mean of 10 packets),
    below the tool's single-packet threshold of 36 counts, so the tool reported "no signal". Run 2, `GAIN 40`:
    baseline mean 4.3, std 25.7, peak 113. Pulser on, all 10 packets alike: onset at sample 3 (3 us), minimum about
    -1190 at sample 14, maximum +1320 to +1393 at sample 24, then damped ringing with a period of about 20 us (50 kHz)
    until about 120 us; from 200 us to the end of the 8.19 ms record only noise. No saturation. Amplitude ratio to
    run 1 about 74 (37.4 dB) for the 40 dB gain step; the noise grew 7.1 times. Run 3, as run 2 with the transducers
    pulled apart: no signal at all (no coherent wavelet in the mean of 10 packets), so the wavelet is the acoustic
    signal through the two transducers and there is no visible electrical feed-through from the pulser into the
    receiver at 40 dB. Time zero: with the transducers face to face the signal starts 3 us after sample 0, so sample 0
    is the start of the burst to within a few microseconds. The pulser was on for 1.0 s (10 packets) per
    `pulsed_acquire` and verified off after each. measured 2026-10-05, fw 1.16
15. **Phase C, experiment 10b: averaging with a real signal** (run 2: `GAIN 40`, same bench). `AVER:COUN 4` gave a
    peak of 1345.7 counts against 1347.7 at `AVER:COUN 0`, ratio 0.999: a mean, not a sum; header `ascan_count` stays
    1. Reading 5 packets took 1.54 s with `AVER:COUN 4` against 0.51 s without at `TRIG:INT 100 MS`, so an averaged
    packet takes about 3 trigger intervals, not 16; how the 16 acquisitions are spaced is not measured. The pulser
    was on for 1.5 s (5 averaged packets) and 0.5 s (5 packets). One bench setup. measured 2026-10-05, fw 1.16
16. **Phase C, experiment 11: what `TRIG:DEL` does** (run 2, same bench). `TRIG:DEL 0 NS`, `1000 US`, `2000 US`: onset
    sample 3 and peak sample 24 in every packet at all three delays, cross-correlation lag 0. `TRIG:DEL` therefore
    does not move the signal inside the record: burst and record are delayed together after the trigger. The
    read-backs were `0`, `1000000` and `2000000`. Whether the nanosecond unit is right cannot be seen this way (not
    measured). One bench setup. measured 2026-10-05, fw 1.16
17. **Stale bytes after a change of `DATA:LENG`.** Seen once: the first acquisition after `DATA:LENG` was changed from
    the power-on 114688 to 8192: the plug's `FrameReader` reported `dropped_bytes=98332` (= 6 * 16384 + 28) and 5 resyncs
    before 5 good packets. Not reproduced with legal lengths: a stream at `DATA:LENG 36864`, `STOP`, the data socket
    closed at once, `DATA:LENG 8192`, a new stream was framed from byte 0 in three trials, one of them with `MEM:CLEar`
    before `STAR AUTO`. The stale bytes followed a stream at the power-on length 114688. What `MEM:CLEar` is needed for is
    unknown; the plug does not send it (no measured need). The restore after phase C put everything back except the
    known power-on `DATA:LENG 114688` (`-224`). measured 2026-10-05, fw 1.16
18. **`*RST`.** Sent once by `tools/hw_probe.py --phase RST`, pulser off. First run: the connection stayed usable,
    the error queue was empty, `TRAN:ENAB?` right after -> `0`, `DATA:PORT?` -> `2758`, and all 27 state headers equal
    to the snapshot, including `DATA:LENG` 8192 (neither the vendor default 1024 nor the power-on 114688). That run
    could not tell a reset from no reset, because the device was in its power-on state apart from `DATA:LENG` and
    `TRAN:ENAB`. Second run, discriminating: six settings were moved away first and read back: `GAIN 6` (`6`),
    `TRIG:INT 10 MS` (`0.01`), `AVER:COUN 2` (`2`), `TRAN:FREQ 50 KHZ` (`50000`), `FILT:HPAS:IND 2` (`2`), `TRIG:DEL
    0 NS` (`0`). After `*RST`: no error, the next query answered 59 ms later, `TRAN:ENAB?` -> `0`, and all 27 headers
    read exactly as before `*RST`, also 2 s later. So on this firmware `*RST` changes no setting: not back to the
    documented defaults and not to the power-on state. Not tested: whether `*RST` stops a running acquisition or
    clears the error queue. Third point of the automatic delay: `TRIG:INT 10 MS` with `AVER:COUN 2` ->
    `AVER:DEL:CONS?` `0.00242925`, so the formula is `TRIG:INT / 2^AVER:COUN - 70.75 us` (`FREQ` 100 MHz only).
    measured 2026-10-05, fw 1.16
    measured 2026-10-05, fw 1.16

Second SCPI connection (4 trials, 4 of 4): connection A to port 5025 is open and answers queries; as soon as a second
connection B to port 5025 is accepted, the device closes A (the client's next `recv` returns 0 bytes, later sends fail
with a broken pipe; with pyvisa this showed up as a timeout). No traffic on B is needed; closing B does not bring A
back; B works normally; a new connection afterwards works at once; no error is queued. The data port (2758) does not
have this effect on the SCPI connection. The tool runs its second-connection experiment last on its own connection.
measured 2026-10-05, fw 1.16

Vendor statements contradicted by the device (A and B numbers are entries of `vendor-tickets/VENDOR-ISSUES.md`):

- Replies to booleans are `ON`/`OFF` in the manual: they are `0`/`1` (A4).
- Enumeration replies are the short, upper-case or echoed form in the manual examples (`MASTER`, `INT`): they are
  the mixed-case notation strings `MASTer`, `INTernal` (A3).
- Time replies look like `100.0E-3` in the manual: they are shortest-decimal numbers such as `2e-06` (A5).
- Error text carries `Command: ` in the manual: it does not; the empty reply has no space (A6).
- `DATA:PORT?` replies `5025` in the manual: `2758` (A7).
- `DATA:LENG` range 1024 to 36864: the device held 114688 (A8); the range itself is confirmed, only the power-on
  value breaks it (entry 12).
- Settings as found differ from the `DEFault` column: pulser on, `DUAL`, `TRIG:INT` 1 s (A9). They are power-on values:
  a power cycle brings them back.
- Automatic `AVER:DEL:CONS` depends on `TRIG:INT` and `AVER:COUN`: it is `TRIG:INT / 2^AVER:COUN` minus 70.75 us
  (A10, entries 12 and 18).
- `*RST` is documented as restoring the `DEFault` column: on fw 1.16 it changes no setting at all (entry 18).
- TGC replies before anything was set are `0,0` and an empty line, not the manual's examples (A11).
- `SYST:VERS?` has no documented reply: `1999.0`; the firmware field of `*IDN?` is `1.16 (861f022a)` (A12).
- `AVER:COUN` is "acquisitions per averaged vector": the count is an exponent (2^N, a mean), and the header field
  `ascan_count` stays 1 (B2).
- `DATA:LENG` power-on value 114688 is refused by its own setter with `-224` (B1); the length fields `length_lo`/
  `length_hi` are undocumented: a 24-bit "samples + 16" (entry 12, B3).
- Error queue depth and overflow are undocumented: 16 entries, then `-350,"Queue overflow"` (A13).
- Not a contradiction but undocumented: an input buffer of 256 bytes (a line over 255 bytes including CRLF is
  discarded with `-363`, A1), a second connection to port 5025 closes the first (A2), packets still
  arrive after `STOP` and the socket stays open (B4), a setting can be changed while streaming (B5), a data connection
  does not disturb the SCPI one (B6).

Not measured: whether `*RST` stops a running acquisition or clears the error queue; count-to-volt
scaling (experiment 12, deferred); whether `TRIG:DEL` is in nanoseconds (only seen not to move the signal); how the
16 acquisitions of an averaged packet are spaced (an averaged packet took about 3 trigger intervals); whether the pulser emits pulses while `TRAN:ENAB`
is 1 but no `STAR AUTO` has been sent (after power-on; measured only: with `TRAN:ENAB ON`, `STAR AUTO` produced a
received signal, with `TRAN:ENAB OFF` it did not); what `MEM:CLEar` does (it is not needed after a change of
`DATA:LENG` to a legal length, entry 17); the wrap of `packet_number` (the header field is one
byte) and whether `ctp[0]` goes on past 255; whether the automatic `AVER:DEL:CONS` offset of 70.75 us depends on
`FREQ`; whether errors queue again after the queue was read once while it was full; settling times; everything with another
transducer pair, voltage or sample rate: phase C used one pair of 50 kHz transducers, 20 V, `FREQ 1 MHZ`.

## Firmware defects the plug does not work around

Three behaviours of fw 1.16 are firmware defects, reported to the vendor; the plug does not hide them. The pulser is
enabled after power-on, and the plug does not switch it off at construction (only `tearDown` and `set_state` do). The
power-on `DATA:LENG` (114688) is refused by the setter itself (`-224`) and packets at that length are partly invalid, so
a restore keeps reporting it. `*RST` resets no setting. A user must therefore send `TRAN:ENAB OFF` and a legal
`DATA:LENG` first, and power-cycle the device to get back to the power-on state.

## Things the vendor material does not tell you

The vendor documents are partly inconsistent and silent on these. The plug does the safest thing for each. All
are listed in PROTOCOL.md "Unknowns to verify on hardware" with the experiment.

- **Data port.** `SCPI_COMMANDS.md` shows `DATA:PORT?` replying 5025, the vendor script starts with 2758. The
  device answered `2758` (measured 2026-10-05, fw 1.16), also right after `*RST`. The plug still queries it on every
  acquisition and never hard-codes it.
- **Single-shot acquisition.** Only `STAR AUTO` is documented, although the text mentions single acquisitions. To be
  measured. `STOP` itself: two packets already in flight arrive after it (within 20 ms), then the socket is idle and
  stays open (measured 2026-10-05, fw 1.16); a reader that stops at `STOP` leaves them unread.
- **Averaging.** `AVER:COUN N` averages 2^N acquisitions as a mean (noise std 3.59 counts at 0, 0.94 at 4), and the
  header field `ascan_count` stays 1 for every N (measured 2026-10-05, fw 1.16). With a real signal the peak is
  unchanged (ratio 0.999 at `AVER:COUN 4`, one bench setup), so it is a mean, not a sum. An averaged packet took about
  3 trigger intervals at `AVER:COUN 4`, not 16; how the acquisitions are spaced is not measured. The plug sends and
  reads back the number and interprets nothing; `ascan_count` does not tell how much was averaged.
- **Header fields.** Seen on 2026-10-05 (fw 1.16) with the pulser off, at five `DATA:LENG` values (1024 to 114688):
  `length_hi << 16 | length_lo` is the sample count plus 16 (24 bits, the carry measured), telemetry bytes 120, 86,
  52, `is_full` 0, `buffer_fill` 0, `ascan_count` 1, reserved bytes 0, `packet_number` +1 per packet and continuing
  across `STOP`/`STAR AUTO`, and in `TRIG:MODE INT` `ctp[0]` equal to `packet_number` with `ctp[1]` = `ctp[2]` = 0.
  Nothing in the plug or in `stream.py` depends on these fields: framing uses `DATA:LENG`. Still unknown: what `ctp`
  means in the external trigger modes (timing array or XYZ coordinates, the docs say both), what the telemetry bytes
  and the 16 mean, and the wrap of `packet_number` and `ctp[0]`. The plug stores them as received.
- **Count-to-volt scaling.** ADC bit depth, full scale and whether gain and TGC act before the ADC are unknown.
  Hence no volts array. To be measured.
- **Time zero.** With one pair of 50 kHz transducers face to face the signal starts 3 us after sample 0, so sample 0
  is the start of the burst to within a few microseconds, and `TRIG:DEL` does not move the signal inside the record:
  burst and record are delayed together after the trigger (measured 2026-10-05, fw 1.16, one bench setup). Whether
  `TRIG:DEL` is in nanoseconds (SCPI document) or samples (REST document) is still unknown. `AScan.t` starts at 0
  at the first sample and nothing more is claimed.
- **`*RST` defaults.** Settled on fw 1.16 (measured 2026-10-05): `*RST` resets nothing, neither to the `DEFault`
  column nor to the power-on state (entry 18). The plug never sends `*RST` on its own and `apply_capture` does not
  reset by default. To get the power-on state, power-cycle the device, and remember that the pulser is then enabled.
  Whether `*RST` stops a running acquisition or clears the error queue is not tested.
- **Reply formats.** Settled on 2026-10-05, fw 1.16: booleans come back as `0`/`1`, enumerations as the mixed-case
  notation (`MASTer`, `INTernal`), times as shortest decimals (`2e-06`); see the section above. `values_match`
  accepts the long, short, case and `ON`/`1` variants. Still to be measured: which unit a bare number gets in a
  command (the plug writes units explicitly) and the formats of the headers not yet written (for example
  `GAIN:TGC:LIN` after a write).
- **Clamping.** The vendor documents silent clamping nowhere, so every value is read back after writing. An
  out-of-range value gave `-224,"Illegal parameter value"` and the old value stayed, with no clamping (measured for
  `DATA:LENG` only: 1023, 36865, 65536 and 114688 refused, 1024, 1025 and 36864 accepted, 2026-10-05, fw 1.16); the
  other headers are unmeasured.
- **Settling times** after a change of gain, pulser voltage or impedance, and changes of settings while
  streaming. The plug does not wait or retry. To be measured.
- **Stale bytes after a change of `DATA:LENG`.** Seen once, after a stream at the power-on length 114688 (98332 stale
  bytes and 5 resyncs before good packets, measured 2026-10-05, fw 1.16); not reproduced with legal lengths (three
  trials, entry 17). `FrameReader` resynchronises over stale bytes, so the packets are right. What `MEM:CLEar` is
  needed for is unknown; the plug does not send it before `STAR AUTO`.

## Testing without hardware

`a1580_openhtf.fake_resource.FakeA1580Resource` stands in for both channels: the PyVISA SCPI resource and the
A-scan data socket. Pass it to the plug: `A1580Plug(resource=FakeA1580Resource())`. The plug then uses the
fake's data socket too. `examples/example_test.py --fake` does exactly that.

What it models, from the vendor documents: the SCPI header set with long and short forms, unit conversion to
the documented reply formats, the error queue with `-113` (undefined header) and `-221` (settings conflict on
`AVER:DEL:CONS` while auto is on), `*IDN?` with firmware `1.16 (861f022a)`, `*RST` that changes no setting (measured), and a
data socket that sends `28 + 2 * DATA:LENG` byte packets between `STAR AUTO` and `STOP`. Measured 2026-10-05
(fw 1.16) and folded in: the error queue of 16 entries plus `-350`, `-224` for a refused value (`DATA:LENG` outside
1024 to 36864 and bad words or numbers), `-363` for one line over 255 bytes including CRLF and error entries cut at 262
characters (the fake cannot see bursts of short lines), seconds replies as shortest decimals (`1`, `2e-06`), header length
fields (a 24-bit samples + 16) and telemetry bytes, `ctp[0]` = the packet counter, `ascan_count` 1, a packet counter
that starts at 2 and never restarts, two packets still delivered after `STOP`, `DATA:LENG MIN/MAX/DEF`, the automatic
`AVER:DEL:CONS` (`TRIG:INT / 2^AVER:COUN` minus 70.75 us), and optionally the power-on state, whose packets at `DATA:LENG` 114688
are zero from sample 81906 on (the device's constant stretch is not modelled).

What it invents: the signal of the default `burst` (a damped sine burst at `TRAN:FREQ`, starting at 10 % of the
record, amplitude scaled by `GAIN`; `signal='transmission'` follows the phase C measurement instead: a 50 kHz pair face
to face, the burst 3 us after sample 0 and not moved by `TRIG:DEL`, noise growing with `GAIN`, measured 2026-10-05, fw
1.16, one bench setup), the defaults marked `UNKNOWN default` in the source, and every error code other than -113
and -221, -224, -350, -363 (-224 is measured for `DATA:LENG` only), the `1e-05` format of 10 us (extrapolated) and the `noise` signal's offset and spread (measured at `AVER:COUN` 0 and 4 only).
A fake only knows what we told it;
nothing is done until it has run on the instrument, and every finding from hardware goes back into it.

Constructor options:

| Option | Effect |
|---|---|
| `length=1024` | initial `DATA:LENG` (1024 unless `power_on`, then 114688) |
| `power_on=True` | start from the measured power-on state (pulser on, `DUAL`, `TRIG:INT` 1 s, `DATA:LENG` 114688, which its own setter refuses); `*RST` changes nothing either way. Off by default: the suite assumes the vendor defaults |
| `chunk=k` | the data socket returns at most `k` bytes per `recv`, to exercise reassembly |
| `garbage_prefix=b'..'` | bytes sent once before the first packet, to exercise resync |
| `signal='burst'` | `'burst'` (default), `'zeros'` (all samples 0), `'noise'` (an open input: offset +11 counts, std 3.6 counts divided by 2^(`AVER:COUN`/2)), `'none'` (never sends a packet: timeouts) or `'transmission'` (pulser on: the measured two-transducer wavelet, off: noise) |
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
