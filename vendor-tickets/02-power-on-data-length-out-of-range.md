## Summary

After power-on `DATA:LENG?` returns `114688`. The documented and enforced range is 1024 to 36864. The device therefore starts with a value its own setter refuses, and packets acquired at that length are only partly valid.

## Environment

- A1580-HF, firmware `1.16 (861f022a)` (4th field of `*IDN?`), Ethernet, static IP.
- SCPI over a plain TCP socket to port 5025, lines ended with `\r\n`. Replies below are shown without the trailing `\r\n`.
- Documentation: this repository at commit `6a9c3003` (`SCPI_COMMANDS.md`, `SCPI_Python/`).
- Measured on 2026-10-05.

## Steps to reproduce (setter)

```
(power-cycle)
> DATA:LENG?
< 114688
> DATA:LENG MAX
> DATA:LENG?
< 36864
> DATA:LENG 114688
> SYST:ERR?
< -224,"Illegal parameter value"
> DATA:LENG?
< 36864
```

Also measured: `MIN` and `DEF` -> `1024`; `36864` accepted; `36865`, `65536`, `1023` -> `-224`, previous value kept; `1025` accepted.

Consequence: a client that reads the settings, changes them and writes them back cannot return the device to its power-on state; only a power cycle does.

## Steps to reproduce (stream)

1. Power-cycle. `TRAN:ENAB OFF`, `TRIG:INT 10 MS` (nothing connected to IN/OUT).
2. `DATA:PORT?` -> `2758`; open a TCP connection to that port.
3. `STAR AUTO`, read for one second, `STOP`.

## Actual (stream)

- 100 packets per second of 229404 bytes (= 28 + 2 x 114688), 22.9 MB/s.
- Header: `length_lo` = 49168, `length_hi` = 1, i.e. 114704 = 114688 + 16.
- Samples 81906 to 114687 are exactly 0 in every packet (81906 = 5 x 16384 - 14).
- Inside the first 65522 samples there is a long stretch of one constant value (30 counts) instead of noise; it starts at a different sample in every packet (about 41000 to 57000) and always ends at sample 65521.
- Elsewhere: normal noise (mean about 11 counts, std 3.6 counts).

## Expected

A power-on `DATA:LENG` inside the documented range (the `DEFault` is 1024), or a documented larger range with valid data.
