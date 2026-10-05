## Summary

The header fields of the binary A-scan packet are named but not explained in `ascan_websocket.py` / `REST_API.md`, and the two descriptions disagree (CTP "timing array" vs "X,Y,Z coordinates"). Below is what firmware 1.16 sends; please confirm and document.

## Environment

- A1580-HF, firmware `1.16 (861f022a)` (4th field of `*IDN?`), Ethernet, static IP.
- SCPI over a plain TCP socket to port 5025, lines ended with `\r\n`. Replies below are shown without the trailing `\r\n`.
- Documentation: this repository at commit `6a9c3003` (`SCPI_COMMANDS.md`, `SCPI_Python/`).
- Measured on 2026-10-05.

## Measured (TRIG:MODE INT, raw TCP data port 2758)

- Packet size = 28 + 2 x `DATA:LENG` bytes at every length tried (1024, 2048, 8192, 36864).
- `length_lo` / `length_hi` form a 24-bit value = `DATA:LENG` + 16: 1040, 2064, 8208, 36880. What do the extra 16 stand for? A client that takes the field as the sample count reads 16 samples too many.
- `ctp[0]` is a running packet counter with the same value as the one-byte `packet_number`; `ctp[1]` = `ctp[2]` = 0. What are the three values in the other trigger modes?
- `packet_number` goes on across `STOP` / `STAR AUTO` (it is not reset); the first packet after power-on has number 2.
- `ascan_count` is 1 with `AVER:COUN 0` and with `AVER:COUN 4`. The document calls it "Number of A-scans accumulated".
- `telemetry_a/b/c` = 120, 86, 52 in every packet; `is_full` = 0, `buffer_fill` = 0, reserved bytes 0. Meaning?

## STOP and the data socket

- After `STOP`, two more packets arrive (within 20 ms at `TRIG:INT 10 MS`); the device keeps the data socket open.
- A data socket connected after `STAR AUTO` receives packets as well (the documents disagree on the required order: `SCPI_COMMANDS.md` says connect first, `REST_API_Python/README.md` starts first).
- Once, the first stream after a `DATA:LENG` change began with about 98 kB of bytes that did not frame at the new length. That was after a stream at the power-on length 114688 (see the separate issue about that value). It did not happen in three trials with legal lengths (stream at 36864, `STOP`, socket closed at once, `DATA:LENG 8192`, new stream), with or without `MEM:CLEar`. The Python example sends `MEM:CLEar` before `STAR AUTO`; what is it needed for? Please document.
- A setting change during acquisition (`GAIN 6`) is accepted and the stream continues.
