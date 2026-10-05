# SPEC-phaseC: pulser-on experiments in `tools/hw_probe.py`

Decision (project owner): `tools/hw_probe.py` gets phase C of `HARDWARE-SESSION.md`
(experiments 10, 11 and 13; experiment 12 is deferred) with the same safety design as
phases A and B: every SCPI string through `GuardedResource`, `--dry-run` and `--fake`,
everything inside `try/finally`, final diff, JSON report.

**The real instrument is NOT available to you.** Work and test with
`FakeA1580Resource` only. Do not open network sockets (except localhost ones made by
tests). Do not commit. Do not edit `PROTOCOL.md`, `STATUS.md`, `HANDOFF.md`.

Read first: `AGENTS.md`, `HARDWARE-SESSION.md`, README section "Measured on the device",
then `tools/hw_probe.py` and `tests/test_hw_probe.py` completely.

Files you may change: `tools/hw_probe.py`, `tests/test_hw_probe.py`,
`src/a1580_openhtf/fake_resource.py` and `tests/test_fake.py` (section 5 only),
`HARDWARE-SESSION.md` (section 6 only).

## 0. Setup on the bench (facts, not guesses)

Two single-element 50 kHz transducers pressed face to face, one on `OUT`, one on `IN`
(through transmission). The received signal comes early and strong; it is not an echo.
Pulser voltage 20 V, which is also the power-on value. Firmware 1.16: the device powers on
with the pulser enabled and `DATA:LENG` 114688, which its setter refuses (`-224`).

## 1. Command line

- `--phase` accepts `A`, `B`, `AB`, `C`, `RST` (case-insensitive as now). `C` runs steps 1, 2,
  the safety check, the pulser-off check, then 10, 10b, 11. `RST` runs steps 1, 2, the safety
  check, the pulser-off check, then 13. Existing phases are unchanged.
- `--pulse-v FLOAT`: required for phase C (usage error `EXIT_USAGE` without it, also in
  `--dry-run`). Must be `>= 5` and `<= --max-pulse-v`, otherwise `EXIT_USAGE` before connecting.
- `--allow-rst`: required for phase RST (usage error without it, also in `--dry-run`).
- Settings of phase C, each with the default for this bench:
  `--tran-freq '50 KHZ'`, `--sample-freq '1 MHZ'`, `--length 8192`, `--interval '100 MS'`,
  `--gain 0`. They are written through `apply_setup` exactly like `STEP6_SETTINGS`.

## 2. Guard

- `TRAN:PULS` writes and `*RST` stay refused by `GuardedResource` by default. Phase C opens
  one gate: a `TRAN:PULS` write is let through only if its numeric argument is `<= --max-pulse-v`
  and equals `--pulse-v`; phase RST opens `*RST`. Anything else is refused as now. `/config`
  stays refused always.
- `TRAN:ENAB ON` (or `1`) is let through only in phase C and only from the function in 3.2.
  In every other phase a `TRAN:ENAB ON` write is refused by the guard (new rule; today nothing
  sends it). Test all of these refusals.
- Every `TRAN:PULS` write and every `TRAN:ENAB ON` is printed (`PULSER: about to send ...`)
  before it is sent.

## 3. Steps

### 3.1 Common preparation (phase C, after the pulser-off check)

`apply_setup` with: `DATA:LENG <--length>`, `FREQ <--sample-freq>`, `TRIG:MODE INT`,
`TRIG:INT <--interval>`, `TRAN:TYPE DUAL`, `TRAN:FREQ <--tran-freq>`, `TRAN:DUR 1`,
`TRAN:IMP HIGH`, `GAIN <--gain>`, `GAIN:TGC:MODE OFF`, `AVER:COUN 0`, `FILT:HPAS:IND 0`,
`TRIG:DEL 0 NS`. Record every read-back (as step 6 does).
Then the voltage: query `TRAN:PULS?`. If it equals `--pulse-v`, nothing is written. Otherwise
write `TRAN:PULS <v> V` (through the gate, announced), read it back, and abort the run
(`EXIT_CEILING`) unless the read-back equals `--pulse-v`.

### 3.2 `pulsed_acquire(p, n) -> packets`

The only place where the pulser is switched on. Order: open the data stream as the other
steps do; announce and send `TRAN:ENAB ON` with `write_checked`; read back `TRAN:ENAB?`;
read `n` packets; then **in a `finally`** send `TRAN:ENAB OFF` (unchecked first, as
`tearDown` does), `STOP`, close the socket, and verify `TRAN:ENAB?` is off; if that
read-back is not off raise `PulserCheckError` (run aborted, `EXIT_PULSER`). The pulser is
therefore on only while packets are being read. Record how long it was on.

### 3.3 Step 10: where is the signal

1. Baseline: 5 packets with the pulser off (existing acquisition helper). Per packet mean and std.
2. `pulsed_acquire(10)`.
3. Analysis on the int16 samples (numpy is available): baseline mean `m` and std `s`
   (median over the baseline packets); for the pulsed packets: min, max, peak `|x - m|`, index
   of the peak, index of the first sample with `|x - m| > max(10 * s, 20)` ("onset"), the same
   in microseconds (`index / FREQ`), and the packet-to-packet spread of onset and peak.
4. Validity checks, printed as `WARNING` lines and stored in the JSON, never silently:
   - saturation: any sample `>= 32767` or `<= -32768`, or the largest value repeated in a run
     of 4 or more consecutive samples (flat top). Say how many samples.
   - no signal: peak `<= max(10 * s, 20)` in every pulsed packet.
   - signal already present in the baseline (pulser off): baseline peak above the threshold.
   - signal still large at the end of the record (last 5 % of samples above the threshold):
     the window is too short or the ringing runs into the next shot.
5. Save the raw samples of all baseline and pulsed packets to
   `<out>/phaseC-<serial>-<stamp>-step10.npz` (arrays `baseline`, `pulsed`, int16, plus the
   settings as a JSON string). Never store floats.

### 3.4 Step 10b: averaging with a real signal

`AVER:COUN 4`, `pulsed_acquire(5)`, then `AVER:COUN 0` again. Compare with step 10: ratio
of the peak amplitudes (1.0 means a mean, 16 a sum) and ratio of the noise std in the
pre-onset part of the record if there are at least 50 samples before the onset, else
"no pre-onset samples". Header `ascan_count` values. Save to `...-step10b.npz`.

### 3.5 Step 11: what `TRIG:DEL` shifts

For `TRIG:DEL` in `0 NS`, `1000 US`, `2000 US` (constant list in the tool): write with
read-back, `pulsed_acquire(5)`, onset index and peak index as in step 10. Then the shift of
the mean waveform against the `0 NS` one by cross-correlation (integer lag with the largest
correlation, numpy), in samples and in microseconds, and the verdict: record moves earlier
or later in the record, by the delay (within 2 samples), by something else (say what), or
the signal left the window. Put `TRIG:DEL 0 NS` back at the end. Save to `...-step11.npz`.

### 3.6 Step 13 (phase RST): `*RST`

1. Announce and send `*RST` (plain `write`, then drain the error queue and record it).
2. At once query `TRAN:ENAB?` and record it; then send `TRAN:ENAB OFF` and verify (same
   failure handling as the pulser-off check). The pulser may come on with `*RST`, as it does
   at power-on: that is a result, not an error.
3. Query every `STATE_HEADERS` entry, `DATA:PORT?`, `SYST:ERR:COUN?`. Print a three-column
   table: value after `*RST`, snapshot value (step 2), vendor `DEFault` (take the table the
   fake uses for `*RST`; headers without a documented default show `-`). Mark each row
   `= snapshot`, `= vendor default`, both, or neither.
4. Restore through the normal teardown (phase RST restores, like B).

### 3.7 Teardown and exit code

Phases C and RST restore the snapshot (as B). A restore failure of `DATA:LENG` whose
snapshot value is outside 1024 to 36864 is printed as
`RESTORE FAILED (known firmware defect: power-on DATA:LENG is refused by the setter): ...`,
goes into the JSON with `"expected": true`, and neither it nor the resulting `DATA:LENG`
difference in the final diff makes the exit code non-zero. Any other failure does, as now.
A `WARNING` of 3.3.4 does not change the exit code (it is a measurement result) but is
repeated in a `WARNINGS` block at the end of the output.

## 4. Dry-run

`--phase C --dry-run --pulse-v 20` and `--phase RST --dry-run --allow-rst` print the full
command list in the order sent, like the other phases, including the conditional
`TRAN:PULS` write (`only if TRAN:PULS? differs from --pulse-v`), each `TRAN:ENAB ON` /
`TRAN:ENAB OFF` pair, and the closing `NEVER SENT` line adjusted to the phase. A test
compares the dry-run list with what a `--fake` run really sends, as the existing test does
for the teardown block (conditional lines excepted).

## 5. Fake (smallest change that lets the tool run; mark everything not measured)

The fake must give phase C something to find: with `TRAN:ENAB` on, its signal contains a
burst whose position moves with `TRIG:DEL`; with the pulser off the same record has no
burst. Look at what `signal='burst'` does today and extend it or add `signal='transmission'`;
keep the default behaviour of existing tests. Comment: "not measured, invented so that
hw_probe phase C can run; replace after the hardware run". `*RST` in the fake stays as it
is. Tests in `tests/test_fake.py`.

## 6. `HARDWARE-SESSION.md`

One sentence each under experiments 10, 11, 13 naming the tool invocation; experiment 12
gets "deferred on 2026-10-05 (signal generator available later)". Nothing else.

## 7. Tests (`tests/test_hw_probe.py`, existing style, fake only)

- usage errors: phase C without `--pulse-v`, `--pulse-v` above `--max-pulse-v`, below 5;
  phase RST without `--allow-rst`.
- guard: `TRAN:ENAB ON` refused in phases A, B, AB, RST; `TRAN:PULS 25 V` refused in phase C
  with `--pulse-v 20`; `*RST` refused in phase C; `/config` refused everywhere.
- phase C on the fake: exit 0; in the sent-command log every `TRAN:ENAB ON` is followed by a
  `TRAN:ENAB OFF` before the next `TRAN:ENAB ON` and before the end; the last `TRAN:ENAB`
  write of the run is OFF; no `TRAN:PULS` write when the fake already has 20 V; exactly one
  `TRAN:PULS 15 V`-style write when `--pulse-v` differs (use a fake at 20 V and `--pulse-v 15`).
- an exception while reading packets in `pulsed_acquire` (inject it) still sends
  `TRAN:ENAB OFF` and `STOP`.
- pulser read-back not off after `pulsed_acquire` (inject) -> `EXIT_PULSER`.
- step 10 analysis functions on synthetic arrays: onset and peak index, each of the four
  warnings, no warning on a clean record.
- step 11 on the fake: the reported shift equals the fake's shift.
- phase RST on the fake: `*RST` sent once, `TRAN:ENAB OFF` right after the `TRAN:ENAB?` that
  follows it, table printed, restore done, exit 0.
- power-on fake (`power_on=True`): the `DATA:LENG` restore failure is marked expected and the
  exit code is 0.
- `.npz` files are written to `--out` and contain int16 arrays.

## 8. Gates (run all, report the results)

```
.venv/bin/python -m pytest          # baseline 692 passed
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy
.venv/bin/python tools/hw_probe.py --phase C --pulse-v 20 --dry-run
.venv/bin/python tools/hw_probe.py --phase RST --allow-rst --dry-run
.venv/bin/python tools/hw_probe.py --phase C --pulse-v 20 --fake --out <temp dir outside the repo>
.venv/bin/python tools/hw_probe.py --phase RST --allow-rst --fake --out <temp dir outside the repo>
.venv/bin/python tools/hw_probe.py --phase AB --fake --out <temp dir outside the repo>
```

Report: what you did per section with the test names; the two dry-run outputs verbatim;
every deviation from this SPEC and why; anything you consider unsafe or likely to fail on
the real device, with file and line.
