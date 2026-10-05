## Summary

Enumeration queries answer with the mixed-case notation string of the manual, whatever form was written. The examples in `SCPI_COMMANDS.md` show `MASTER` and `INT`.

## Environment

- A1580-HF, firmware `1.16 (861f022a)` (4th field of `*IDN?`), Ethernet, static IP.
- SCPI over a plain TCP socket to port 5025, lines ended with `\r\n`. Replies below are shown without the trailing `\r\n`.
- Documentation: this repository at commit `6a9c3003` (`SCPI_COMMANDS.md`, `SCPI_Python/`).
- Measured on 2026-10-05.

## Steps to reproduce

```
> MODE MASTER
> MODE?
< MASTer
> TRIG:MODE INT
> TRIG:MODE?
< INTernal
```

## Expected (from `SCPI_COMMANDS.md`)

```
> MODE MASTER
> MODE?
< MASTER
> TRIG:MODE INT
> TRIG:MODE?
< INT
```

## Notes

- A client that compares the reply with `MASTER`, `INT` or `INTERNAL` fails. The usual SCPI convention is to return the short form in upper case (`MAST`, `INT`).
- `TRAN:TYPE?` -> `DUAL`, `TRAN:IMP?` -> `HIGH`, `GAIN:TGC:MODE?` -> `OFF` have no lower-case part and look normal; presumably `SINGle`, `SLAVe`, `ENCoder`, `LINear`, `ARBitrary` come back in the same template form (not measured).
- Please either change the replies or document the exact reply strings.
