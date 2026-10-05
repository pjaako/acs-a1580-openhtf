## Summary

The SCPI server keeps one client. When a second TCP connection to port 5025 is accepted, the device closes the first connection at once, without any error message. The client on the first connection only notices at its next command (end of stream, then broken pipe, or a timeout depending on the library).

## Environment

- A1580-HF, firmware `1.16 (861f022a)` (4th field of `*IDN?`), Ethernet, static IP.
- SCPI over a plain TCP socket to port 5025, lines ended with `\r\n`. Replies below are shown without the trailing `\r\n`.
- Documentation: this repository at commit `6a9c3003` (`SCPI_COMMANDS.md`, `SCPI_Python/`).
- Measured on 2026-10-05.

## Steps to reproduce

1. Open TCP connection A to port 5025. `SYST:ERR:COUN?` -> `0` (works).
2. Open TCP connection B to port 5025 (accepted). Send nothing on it.
3. On A: `SYST:ERR:COUN?`

## Actual

Step 3: the device has closed A (`recv` returns 0 bytes; later sends fail with "broken pipe"). B works normally. Closing B does not bring A back. A new connection works at once. Reproduced 4 times out of 4, with and without traffic on B.

The data connection (port 2758) does not have this effect: it can be opened and closed many times during one SCPI session.

## Expected

Either several clients are served, or the second connection is refused, or the behaviour is documented. Silently dropping the older client is hard to diagnose: for example a monitoring tool that connects kills a running test.
