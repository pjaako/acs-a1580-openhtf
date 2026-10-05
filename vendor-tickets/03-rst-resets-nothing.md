## Summary

`*RST` is accepted without an error but changes no setting: neither to the documented `DEFault` values nor to the power-on values.

## Environment

- A1580-HF, firmware `1.16 (861f022a)` (4th field of `*IDN?`), Ethernet, static IP.
- SCPI over a plain TCP socket to port 5025, lines ended with `\r\n`. Replies below are shown without the trailing `\r\n`.
- Documentation: this repository at commit `6a9c3003` (`SCPI_COMMANDS.md`, `SCPI_Python/`).
- Measured on 2026-10-05.

## Steps to reproduce

```
> GAIN 6
> TRIG:INT 10 MS
> AVER:COUN 2
> TRAN:FREQ 50 KHZ
> FILT:HPAS:IND 2
> TRIG:DEL 0 NS
(each read back: 6, 0.01, 2, 50000, 2, 0)
> *RST
> SYST:ERR?
< 0,"No error"
> GAIN?
< 6
> TRIG:INT?
< 0.01
> AVER:COUN?
< 2
> TRAN:FREQ?
< 50000
> FILT:HPAS:IND?
< 2
> TRIG:DEL?
< 0
```

All 27 settable parameters were read before and after `*RST` and again 2 s later: no value changed (also `DATA:LENG` stayed at 8192, `TRAN:ENAB` at 0). The first query after `*RST` was answered 59 ms later.

## Expected

`*RST` returns the instrument to a defined state (the `DEFault` column of `SCPI_COMMANDS.md`), or the document says what `*RST` does on this device.

Not tested: whether `*RST` stops a running acquisition or clears the error queue.
