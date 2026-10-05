## Summary

The SCPI input buffer holds 256 bytes. A single command line of 256 bytes or more (terminator included) is discarded with `-363,"Input buffer overrun"`, and so is a burst of short commands that arrives faster than it is parsed; a query that was part of the burst is never answered, so the client blocks until its timeout. The limit is not documented, and it is low for `GAIN:TGC:ARBitrary`, whose point list can easily be longer.

## Environment

- A1580-HF, firmware `1.16 (861f022a)` (4th field of `*IDN?`), Ethernet, static IP.
- SCPI over a plain TCP socket to port 5025, lines ended with `\r\n`. Replies below are shown without the trailing `\r\n`.
- Documentation: this repository at commit `6a9c3003` (`SCPI_COMMANDS.md`, `SCPI_Python/`).
- Measured on 2026-10-05.

## Steps to reproduce (single line)

Send one line of L bytes in total (an undefined header padded with letters, `\r\n` included), then `SYST:ERR?`:

| L | Result |
|---|---|
| 100, 150, 200, 250, 254, 255 | `-113,"Undefined header;..."` (the line was parsed) |
| 256, 257, 258, 260, 300, 400, 600 | `-363,"Input buffer overrun"` (the line was discarded) |

So the longest accepted line is 253 characters plus `\r\n`.

## Steps to reproduce (burst)

1. Write these 26 lines one after another without reading (about 320 bytes): `ZZZ:NOPE01` ... `ZZZ:NOPE25`, then `SYST:ERR:COUN?`.
2. Wait for the reply.
3. Read `SYST:ERR?` until `0,"No error"`.

(`ZZZ:NOPExx` is an undefined header chosen so that nothing is changed.)

## Actual (burst)

- No reply to `SYST:ERR:COUN?` (5 s timeout).
- The error queue holds two entries: `-113,"Undefined header;ZZZ:NOPE01"` and `-363,"Input buffer overrun"`. Lines 02 to 25 and the query are lost.
- The connection stays usable.
- Bursts of 2, 3, 5 and 10 lines of 16 bytes (up to 160 bytes) are processed completely.

## Expected

Flow control through TCP (do not read from the socket while the parser is busy) and a buffer large enough for the longest legal command, in particular a full `GAIN:TGC:ARBitrary` list; or at least the documented maximum line length and the maximum number of TGC points. Losing a query silently makes a client hang.
