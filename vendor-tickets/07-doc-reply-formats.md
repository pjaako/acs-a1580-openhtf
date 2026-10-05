## Summary

Several reply formats shown in `SCPI_COMMANDS.md` do not match what firmware 1.16 sends. None of them is a functional problem, but a client written from the document fails on string comparison or parsing.

## Environment

- A1580-HF, firmware `1.16 (861f022a)` (4th field of `*IDN?`), Ethernet, static IP.
- SCPI over a plain TCP socket to port 5025, lines ended with `\r\n`. Replies below are shown without the trailing `\r\n`.
- Documentation: this repository at commit `6a9c3003` (`SCPI_COMMANDS.md`, `SCPI_Python/`).
- Measured on 2026-10-05.

## Differences

| Query | Document | Device |
|---|---|---|
| `TRAN:ENAB?` | `ON` / `OFF` | `1` / `0` |
| `TRAN:REV?` | example `ON` | `0` / `1` |
| `AVER:DEL:CONS:AUTO?` | example `ON` | `1` |
| `TRAN:DAMP?` | `0` / `1`, but the example shows `TRAN:DAMP ON` followed by reply `0` | `0` / `1` (the example is probably a typo) |
| `TRIG:INT?` after `TRIG:INT 100000 US` | `100.0E-3` | `0.1` (10 ms -> `0.01`, 1 s -> `1`) |
| `AVER:DEL:CONS?` | `50.0E-6` | shortest decimal, e.g. `0.00992925` |
| `AVER:DEL:RAND?` after `2 US` | `2.0E-6` | `2e-06` |
| `SYST:ERR?` after an undefined header | `-113,"Undefined header;Command: SYST:ERRrr"` | `-113,"Undefined header;ZZZ:NOPE 1"` (no `Command: `, the whole line as sent) |
| `SYST:ERR?`, empty queue | `0, "No error"` | `0,"No error"` (no space) |
| `DATA:PORT?` | example `5025` | `2758` (as in the Python example) |
| `GAIN:TGC:LIN?` before anything is set | example `20.0, 0.1` | `0,0` |
| `GAIN:TGC:ARB?` before anything is set | example list | empty line |
| `SYST:VERS?` | no example | `1999.0` |
| `*IDN?` firmware field | `1.6.b41` | `1.16 (861f022a)` (contains a space and brackets) |

Also undocumented: a bare `\n` is accepted as terminator; replies always end with `\r\n`.

## Request

Please update the examples in `SCPI_COMMANDS.md`, or state the reply format per command.
