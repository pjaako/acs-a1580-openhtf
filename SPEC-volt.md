# SPEC-volt: experiment 12 (count-to-volt scaling) as phase V of `tools/hw_probe.py`

Decision (project owner, 2026-10-12): `tools/hw_probe.py` gets phase V for experiment 12 of
`HARDWARE-SESSION.md` (PROTOCOL.md "Unknowns to verify on hardware" item 9; the
"(unknown 2)" label in `HARDWARE-SESSION.md` is wrong and is corrected in section 6).
A signal generator feeds a known sine into `IN`, the pulser stays off, and the tool records
counts against volts. Same safety design as phases A to C: every SCPI string through
`GuardedResource`, `--dry-run` and `--fake`, everything inside `try/finally`, restore and
final diff, JSON report, raw `int16` saved.

**The real instrument is NOT available to you.** Work and test with `FakeA1580Resource`
only. Do not open network sockets (except localhost ones made by tests). Do not commit. Do
not edit `PROTOCOL.md`, `STATUS.md`, `README.md`, `AGENTS.md`.

Read first: `AGENTS.md`, `HARDWARE-SESSION.md`, README sections "Measured on the device"
and "Things the vendor material does not tell you", `SPEC-phaseC.md` (the pattern this
SPEC follows), then `tools/hw_probe.py` and `tests/test_hw_probe.py` completely, and
`fake_resource.py` `_packet()`.

Files you may change: `tools/hw_probe.py`, `tests/test_hw_probe.py`,
`src/a1580_openhtf/fake_resource.py` and `tests/test_fake.py` (section 5 only),
`HARDWARE-SESSION.md` (section 6 only).

## 0. Facts and unknowns this phase rests on

- Vendor material: `GAIN` 0 to 80 dB in 1 dB steps; `GAIN:PRE:COMB` and `GAIN:PRE:SPLIT`
  each "add 20 dB", whether both at once is unknown; `TRAN:IMP` is `HIGH`, `200` or `1000`
  (ohm); `FILT:HPAS:IND` 0 to 3 is a high-pass at 1 kHz, 100 kHz, 500 kHz, 1000 kHz;
  `FREQ` is 1, 2, 5, 10, 25, 50 or 100 MHz. ADC depth, full scale and whether gain acts
  before the ADC are UNKNOWN (PROTOCOL.md "Amplitude scale").
- Measured (fw 1.16, 2026-10-05): open input at `GAIN 0` gives mean about +11 counts, std
  3.6 counts at `FREQ 100 MHZ` and mean 4.65 at `1 MHZ`; a 40 dB `GAIN` step gave 37.4 dB.
- The receiver input's maximum safe voltage is UNKNOWN. The tool therefore never asks for
  more than `--max-gen-vpp` (default 1.0 V peak-to-peak) and the human confirms that
  ceiling before the session (section 6 adds it to the preconditions).
- Generator loading: a generator's displayed amplitude assumes a load. With the display set
  to "High-Z" the open-circuit voltage equals the display; with "50 ohm" it is twice the
  display. Into the device the voltage is `v_open * R / (R + Z_out)` with `R` from
  `TRAN:IMP` (`HIGH` = infinite, `1000`, `200`) and `Z_out` the generator's output
  impedance (`--gen-zout`, default 50). The tool reports the displayed value, the computed
  input value and the assumption that produced it, always.

## 1. Command line

- `--phase` gains `V` (case-insensitive as now). `V` runs steps 1, 2, the safety check, the
  pulser-off check, the preparation of 3.1, then 12a and 12b; 12c, 12d and 12e only with
  `--with-preamps`, `--with-impedance`, `--with-hpf`. Existing phases are unchanged.
- Generator description, all recorded in the JSON:
  `--gen-hz FLOAT` (default `100000`), `--gen-vpp LIST` (comma-separated displayed Vpp,
  default `0,0.02,0.05,0.1,0.2,0.5,1.0`; `0` means "generator output off" and must be first
  if present), `--max-gen-vpp FLOAT` (default `1.0`), `--gen-display {HIGHZ,50}` (what the
  generator display assumes, default `HIGHZ`), `--gen-zout FLOAT` (ohm, default `50`),
  `--gen-note TEXT` (free text: model, output used, cable; default empty).
  The ceiling applies to the voltage at the input, not to the display: for every `--gen-vpp`
  entry and for `--gain-vpp` the open-circuit voltage (the display, or twice the display
  with `--gen-display 50`) must not exceed `--max-gen-vpp`, otherwise `EXIT_USAGE` before
  connecting (also in `--dry-run`); so are a non-ascending list and a `0` not in first place.
  (Decision 2026-10-12 after review: a warning at run time is not enough, the receiver's
  rating is unknown.)
- Settings of phase V, each with its default: `--sample-freq '10 MHZ'`, `--length 8192`,
  `--interval '100 MS'`. `--packets` (existing, default 10) is the number of packets per
  generator setting. `--gains LIST` (default `0,6,12,20,40,60`) for 12b; `--gain-vpp FLOAT`
  (default `0.05`) is the displayed amplitude used in 12b to 12e and must appear in
  `--gen-vpp`'s range (not above `--max-gen-vpp`).
- `--no-prompt`: never wait for the human (the generator is already set; useful for a
  one-entry `--gen-vpp`). With `--fake` there are no prompts either (section 5).

## 2. Guard and prompts

- Phase V opens no gate: `TRAN:PULS` writes, `TRAN:ENAB ON`, `*RST` and `/config` stay
  refused by `GuardedResource` exactly as in phases A and B. Test it.
- Every change of the generator is a prompt, never a command: the tool prints
  `GENERATOR: set <vpp> Vpp (display <HIGHZ|50>) sine at <hz> Hz on IN, output ON, then press
  Enter` (or `... output OFF ...` for `0`), reads one line from stdin and continues on an
  empty line, skips the entry on `s`, aborts the run on `a` (AbortRun, `EXIT_FAILED_STEP`,
  restore runs). Any other input repeats the prompt. The prompt is a function
  `set_generator(p, vpp)` so tests can monkeypatch `builtins.input`; in `--fake` mode it sets
  the fake's generator instead (section 5); with `--no-prompt` it only prints the line.
- Before step 12a one extra prompt: `GENERATOR: connect the generator to IN (nothing on OUT),
  confirm the input ceiling <max-gen-vpp> Vpp, then press Enter`.

## 3. Steps

### 3.1 Preparation (phase V, after the pulser-off check)

`apply_setup` with, in this order: `DATA:LENG <--length>`, `FREQ <--sample-freq>`,
`TRIG:MODE INT`, `TRIG:INT <--interval>`, `TRAN:IMP HIGH`, `GAIN 0`, `GAIN:PRE:COMB 0`,
`GAIN:PRE:SPLIT 0`, `GAIN:TGC:MODE OFF`, `AVER:COUN 0`, `FILT:HPAS:IND 0`, `TRIG:DEL 0 NS`.
Record every read-back as `prepare_phase_c` does. Then `TRAN:ENAB?` once more; anything but
off is `EXIT_PULSER`.

### 3.2 Analysis of one generator setting (`analyse_sine(packets, fs_hz, gen_hz)`)

On the `int16` samples of every packet (numpy):

1. `mean` (DC offset), `min`, `max`, `std`.
2. Sine fit at `gen_hz`: least squares of `a*sin(2*pi*f*t) + b*cos(2*pi*f*t) + c`, giving
   `amplitude = hypot(a, b)` in counts (peak, not peak-to-peak), `offset = c`, and
   `residual_std` (std of samples minus fit).
3. Frequency check: the FFT peak bin of the mean-removed record, in Hz (`peak_hz`, the
   median over the packets), and the relative difference to `gen_hz`. Over 2 % or one FFT
   bin, whichever is larger: `WARNING: generator or sample rate off` (not at 0 Vpp). The
   `--length` help says that a short record widens this tolerance.
4. Code usage over all packets of the setting, computed before the saturation check:
   `distinct_values`, and `lowest_used_bit` = the largest `k` such that every
   `sample - min` is a multiple of `2**k` (0 when the LSB is used). This is the only cheap
   hint at the ADC depth; interpret nothing in the tool.
5. Saturation: any sample `>= 32767` or `<= -32768` is saturation. A flat top is a run of
   equal samples at the largest or smallest value that is longer than quantisation
   explains: with `q = 2**lowest_used_bit`, `A` the fitted amplitude (at least 1 count) and
   `n_q = 2 * (fs / (2 * pi * gen_hz)) * sqrt(2 * q / A) + 1` samples (the width of a
   sine peak within one code of its extreme), a run of at least `max(4, ceil(3 * n_q))`
   is a flat top. `saturated` true or false, plus the count of such samples and the
   clipping value when seen. (Decision 2026-10-12 after review: "4 equal samples" alone
   reads a coarse ADC's sine peak as clipping, e.g. 80 counts amplitude at a 16-count step.)

Per setting the tool stores the per-packet values and their medians.

### 3.3 Step 12a: amplitude sweep

For each `--gen-vpp` entry in order: `set_generator`, then `p.plug.acquire(--packets,
timeout_s=ACQ_TIMEOUT_S)` and `analyse_sine`. Store per entry: `vpp_display`, `v_in_pp`
(the computed input voltage, section 0), `v_in_peak = v_in_pp / 2`, the analysis.
Stop the sweep at the first saturated entry (announce `saturated at <vpp>; higher entries
skipped`); the `0` entry gives `mean_off` and `noise_off` (std).
Then the fit over the unsaturated entries with `vpp_display > 0`: linear least squares of
`amplitude` against `v_in_peak`: `counts_per_volt` (slope), `intercept_counts`, the largest
relative residual, the number of points used. Fewer than 2 points: `fit: not possible` with
the reason. If a clipping value was seen: `full_scale_v_estimate = clip_counts /
counts_per_volt`. Warnings as in 3.2 plus `WARNING: fewer than 3 unsaturated points` and
`WARNING: largest residual over 5 %`.
Save all packets of every entry to `<out>/phaseV-<serial>-<stamp>-step12a.npz`: one
`int16` array per entry named `vpp_<index>` plus the `settings` JSON string (which includes
the list of entries and the generator description). Never store floats.

### 3.4 Step 12b: `GAIN` steps

`set_generator(--gain-vpp)` once. For each value of `--gains` in order: `GAIN <g>` with
read-back (inside `_put_back` so `GAIN 0` returns at the end), acquire `--packets`,
`analyse_sine`. Measured gain in dB = `20 * log10(amplitude / amplitude_at_0)`, deviation
from nominal in dB. Stop ascending at the first saturated value (announce it). Save to
`...-step12b.npz` (`gain_<g>` arrays).

### 3.5 Step 12c (`--with-preamps`): preamplifier paths

At `GAIN 0` and `--gain-vpp`, in order: `GAIN:PRE:COMB 1` / `SPLIT 0`, then `COMB 0` /
`SPLIT 1`, then both `1`. Each write with read-back and the error queue drained and
recorded (a refused combination is a result: `error: ...`, not a failure). dB against the
reference of 12b at `GAIN 0`. Saturation stops nothing here (one setting each), it is
reported. Both back to `0` inside `_put_back`. Save to `...-step12c.npz`.

### 3.6 Step 12d (`--with-impedance`): `TRAN:IMP`

At `GAIN 0`, preamps `0`, `--gain-vpp`: `TRAN:IMP 200`, `1000`, `HIGH` in that order (HIGH
last so the device ends where the restore expects it). Per value: read-back, acquire,
`analyse_sine`, `v_in_peak` recomputed for that `R` (section 0), and `counts_per_volt_at_R
= amplitude / v_in_peak`. Save to `...-step12d.npz`.

### 3.7 Step 12e (`--with-hpf`): `FILT:HPAS:IND`

At `GAIN 0`, `HIGH`, `--gain-vpp`: index `1`, `2`, `3`, then back to `0` (`_put_back`).
Attenuation in dB against index `0` (the 12b reference). Only when `--gen-hz` is
within 10 % of 100 kHz, note in the output that the index-1 corner lies on the generator
frequency, so a value near -3 dB is expected there. Save to `...-step12e.npz`.

### 3.8 Teardown and exit code

Phase V restores the snapshot (as B). The known power-on `DATA:LENG` restore failure is
handled as in phase C (expected, exit code unchanged). `WARNING` lines are results, not
failures; they are repeated in the `WARNINGS` block. A generator abort (`a`) is
`EXIT_FAILED_STEP` after the restore.

## 4. Dry-run

`--phase V --dry-run` prints the full command list in the order sent, like the other
phases, with each prompt as a `PROMPT: ...` line at its place, the 12b `GAIN` writes, and
the closing `NEVER SENT` line (`TRAN:PULS`, `TRAN:ENAB ON`, `*RST`, `/config`). A test
compares the dry-run list with what a `--fake` run really sends (prompts excepted), as the
existing phase C test does.

## 5. Fake (smallest change; everything invented is marked so)

Add `signal='sine'` to `FakeA1580Resource`: a continuous sine at `gen_hz` with random
phase per packet, amplitude in counts `gen_vpp / 2 * COUNTS_PER_VOLT * 10**(GAIN/20) *
PRE * IMP * HPF`, plus the measured noise (offset 11, std 3.6), clipped to `int16`.
Attributes settable by the tool and by tests: `gen_vpp` (float, 0 = output off),
`gen_hz` (float). Invented constants, each with the comment
"not measured, invented so that hw_probe phase V can run; replace after the hardware run":
`COUNTS_PER_VOLT = 8000.0` (peak counts per peak volt at `GAIN 0`, `HIGH`), `PRE` = 10 for
each preamp that is `1` (so 100 for both; the fake accepts both on), `IMP` = 1.0 for `HIGH`,
0.95 for `1000`, 0.8 for `200`, `HPF` at 100 kHz = 1.0, 0.707, 0.2, 0.1 for index 0 to 3.
`TRAN:ENAB` plays no role in `sine`. Existing signals and all existing tests keep their
behaviour. `--fake` with `--phase V` uses `FakeA1580Resource(signal='sine')`, and
`set_generator` sets `fake.gen_vpp` (and `gen_hz` from `--gen-hz`) instead of prompting.
Tests in `tests/test_fake.py` section 5: amplitude follows `gen_vpp`, `GAIN`, the preamps,
`TRAN:IMP` and `FILT:HPAS:IND` as specified; `gen_vpp = 0` gives noise only; clipping at
`int16` for an absurd amplitude.

## 6. `HARDWARE-SESSION.md`

Experiment 12 only: replace "(unknown 2)" by "(PROTOCOL.md unknown 9)", keep the
"Deferred on 2026-10-05" sentence, and add one sentence naming the invocation
(`tools/hw_probe.py --phase V`, the generator on `IN`, the human sets the generator at each
prompt, optional `--with-preamps --with-impedance --with-hpf`). In the preconditions add
one item: the generator model, its output impedance and display assumption, and the input
ceiling (`--max-gen-vpp`, default 1.0 Vpp) that the human confirms, since the receiver
input's rating is unknown. Nothing else.

## 7. Tests (`tests/test_hw_probe.py`, existing style, fake only)

- usage errors: `--gen-vpp` with an entry above `--max-gen-vpp`, a non-ascending list, a
  `0` not first, `--gain-vpp` above the ceiling (each also in `--dry-run`).
- guard in phase V: `TRAN:ENAB ON`, `TRAN:PULS 20 V`, `*RST`, `/config` all refused.
- phase V on the fake, defaults: exit 0; `PREPARATION` read-backs in the report (`FREQ`
  `10000000`, `DATA:LENG` `8192`, `TRAN:IMP` `HIGH`, `GAIN` `0`); step 12a has one entry per
  `--gen-vpp` value, none saturated, `counts_per_volt` within 2 % of the fake's
  `COUNTS_PER_VOLT` (display `HIGHZ`, `HIGH`), `mean_off` about 11; step 12b measured gains
  within 0.1 dB of nominal for the fake; `FINAL DIFF` `changed == {}`; `TRAN:ENAB` written
  only as `OFF`; no `TRAN:PULS` write.
- `--gen-display 50`: `v_in_pp` is twice the display at `HIGH`; with `--with-impedance`
  the `200` and `1000` entries use the divider of section 0 (check one value by hand).
- saturation: a fake with an absurd `COUNTS_PER_VOLT` (monkeypatch the constant) makes
  12a stop at the first saturated entry, `saturated` true, higher entries listed as skipped,
  the fit uses only the lower entries, `full_scale_v_estimate` present.
- `analyse_sine` on synthetic arrays: amplitude and offset recovered within 1 count on a
  clean sine, `peak_hz` warning when the frequency is 5 % off, each warning once (also the
  residual warning), `lowest_used_bit` = 2 for samples that are all multiples of 4;
  saturation: a sine of 80 counts quantised to 16-count steps (fs 10 MHz, 100 kHz, noise
  std 3.6) is not saturated, a sine clipped at +-600 is, the int16 limit is.
- a `KeyboardInterrupt` at a prompt becomes `AbortRun` (`EXIT_FAILED_STEP`), the restore runs.
- prompts: with `builtins.input` monkeypatched, a `--fake`-less run is not possible, so test
  `set_generator` directly with a fake `Probe` stub: empty line continues, `s` skips, `a`
  raises `AbortRun`, other input re-prompts; `--no-prompt` never calls `input`.
- `--with-preamps`, `--with-impedance`, `--with-hpf` each: the writes appear with
  read-backs, the settings are put back, the dB values match the fake's constants within
  0.1 dB; without the flags none of those headers is written.
- dry-run matches the fake run (prompts excepted); every command sent by the fake run is in
  the plan (parametrize the existing test over phase V).
- `.npz` files per step are written to `--out` with `int16` arrays and a `settings` string.
- power-on fake: the `DATA:LENG` restore failure is marked expected and the exit code is 0.

## 8. Gates (run all, report the results)

```
.venv/bin/python -m pytest          # baseline 876 passed
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy
.venv/bin/python tools/hw_probe.py --phase V --dry-run
.venv/bin/python tools/hw_probe.py --phase V --dry-run --with-preamps --with-impedance --with-hpf
.venv/bin/python tools/hw_probe.py --phase V --fake --out <temp dir outside the repo>
.venv/bin/python tools/hw_probe.py --phase V --fake --with-preamps --with-impedance --with-hpf --out <temp dir outside the repo>
.venv/bin/python tools/hw_probe.py --phase AB --fake --out <temp dir outside the repo>
.venv/bin/python tools/hw_probe.py --phase C --pulse-v 20 --gain 40 --fake --out <temp dir outside the repo>
.venv/bin/python tools/hw_probe.py --phase RST --allow-rst --fake --out <temp dir outside the repo>
```

Report: what you did per section with the test names; the first dry-run output verbatim;
the step 12a and 12b summary lines of the default fake run; every deviation from this SPEC
and why; anything you consider unsafe or likely to fail on the real device, with file and
line.
