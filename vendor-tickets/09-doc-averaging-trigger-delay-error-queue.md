## Summary

Four points that `SCPI_COMMANDS.md` leaves open or describes differently from what firmware 1.16 does. Please confirm and document.

## Environment

- A1580-HF, firmware `1.16 (861f022a)` (4th field of `*IDN?`), Ethernet, static IP.
- SCPI over a plain TCP socket to port 5025, lines ended with `\r\n`. Replies below are shown without the trailing `\r\n`.
- Documentation: this repository at commit `6a9c3003` (`SCPI_COMMANDS.md`, `SCPI_Python/`).
- Measured on 2026-10-05.

## 1. `AVER:COUN` is an exponent, the result is a mean

`SCPI_COMMANDS.md`: "Acquisitions per averaged vector", 0 to 8. `REST_API.md`: `accumulations` "0-8 (power of 2)".

Measured: with an open input the noise std per packet is 3.59 counts at `AVER:COUN 0` and 0.94 at `AVER:COUN 4` (ratio 0.26, a mean of 16 gives 0.25). With a real signal (transducer pair, 1350 counts peak) the amplitude is the same at `AVER:COUN 0` and `4` (ratio 0.999). So N means 2^N acquisitions and the samples are the mean, not the sum.

## 2. Automatic averaging delay

Document: computed "from the averaging count and sampling rate". Measured with `AVER:DEL:CONS:AUTO` on, `FREQ` 100 MHz:

| `TRIG:INT` | `AVER:COUN` | `AVER:DEL:CONS?` |
|---|---|---|
| 1 s | 0 | `0.99992925` |
| 10 ms | 0 | `0.00992925` |
| 10 ms | 2 | `0.00242925` |

All three fit `TRIG:INT / 2^AVER:COUN - 70.75 us`; the dependence on the trigger interval is not in the document.

## 3. `TRIG:DEL` and time zero

Document: "Delay between a trigger event and the acquisition"; `REST_API.md` says "in samples", `SCPI_COMMANDS.md` nanoseconds; what sample 0 corresponds to is not stated.

Measured (two 50 kHz transducers face to face, `FREQ 1 MHZ`): the received signal starts 3 us after sample 0 and stays on the same samples for `TRIG:DEL` 0, 1000 US and 2000 US. So the burst and the record are delayed together and sample 0 is the start of the burst. One sentence in the manual would settle it.

## 4. Error queue depth

Not documented. Measured: 16 entries; the 17th and last entry becomes `-350,"Queue overflow"`, later errors are dropped, `SYST:ERR:COUN?` stays at 17.
