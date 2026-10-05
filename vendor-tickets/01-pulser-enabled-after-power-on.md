## Summary

After power-on the transmitter is enabled at 20 V. `SCPI_COMMANDS.md` gives `OFF` as the default of `TRANsmitter:ENABle`. Until a client sends `TRAN:ENAB OFF`, the first `STAR AUTO` fires the pulser into whatever is connected to the OUT socket. (Whether pulses are emitted before any `STAR AUTO` was not measured.)

## Environment

- A1580-HF, firmware `1.16 (861f022a)` (4th field of `*IDN?`), Ethernet, static IP.
- SCPI over a plain TCP socket to port 5025, lines ended with `\r\n`. Replies below are shown without the trailing `\r\n`.
- Documentation: this repository at commit `6a9c3003` (`SCPI_COMMANDS.md`, `SCPI_Python/`).
- Measured on 2026-10-05.

## Steps to reproduce

1. Power-cycle the unit. Do not send any setting.
2. `TRAN:ENAB?`
3. `TRAN:PULS?`, `TRAN:TYPE?`, `TRIG:INT?`

## Actual

```
> TRAN:ENAB?
< 1
> TRAN:PULS?
< 20
> TRAN:TYPE?
< DUAL
> TRIG:INT?
< 1
```

Reproduced after three power cycles. `TRAN:ENAB OFF` works and reads back `0`, but the setting is not kept across a power cycle. That `TRAN:ENAB` = 1 really means an active output was checked separately: with a transducer pair connected and `TRAN:ENAB ON`, `STAR AUTO` produced a received signal; with `TRAN:ENAB OFF` it did not.

## Expected

`TRAN:ENAB?` -> off after power-on, as documented (`DEFault` = OFF). A pulser that starts by itself is a safety concern for whatever is connected.

## Other power-on values that differ from the documented defaults

| Command | After power-on | Documented default |
|---|---|---|
| `TRAN:ENAB?` | `1` | OFF |
| `TRAN:TYPE?` | `DUAL` | SINGle |
| `TRIG:INT?` | `1` (1 s) | 10 ms |
| `DATA:LENG?` | `114688` | 1024 (separate issue) |
