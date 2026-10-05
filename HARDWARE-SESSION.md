# HARDWARE-SESSION.md: protocol for sessions with the real A1580

Who: the project owner agent, with the human owner reachable. Coder agents never run this.
When: after v1 passes its hardware-free gates, and whenever a new unknown must be resolved.

## Preconditions (the human confirms each one before the session starts)

1. Which transducer, if any, is connected to the `IN` / `OUT` sockets, and the maximum
   pulser voltage that is safe for it. Default ceiling for the first session: **20 V**.
2. The device is reachable from the agent: either `A1580_HOST` forwarded to the cloud box
   (SCPI port 5025 and the data port, default 2758, both TCP) or the session runs on the
   local machine next to the device.
3. Nobody else is using the device. Each SCPI session may have its own state; we do not
   know yet, so one client at a time.
4. The agent has `A1580_HOST` set and has run `tools/hw_probe.py --dry-run` (prints the
   command list without connecting).

## Rules during the session

- Save first, change second: the first command block is `*IDN?`, then every header in
  `STATE_HEADERS` queried and written to `setups/<serial>-<date>.json` (git-ignored).
- Never change network or Wi-Fi settings over the network (`/config/dev.eth`, `dev.wlan`).
- Never exceed the agreed pulser voltage. Every `TRAN:PULS` write is logged before it is sent.
- Every experiment wraps its work in `try: ... finally: plug.tearDown()`.
- One unknown per experiment, in the order below. Record the result before the next one.
- An unexpected reply stops the session for a decision; do not improvise around it.
- Results go into three places: `README.md` ("measured <date>, fw <version>"),
  the fake (`fake_resource.py`) when behaviour differs from the model, and
  `PROTOCOL.md` is left untouched (it digests the vendor, not the device).

## Experiments, in order (numbers refer to `PROTOCOL.md` "Unknowns to verify on hardware")

Phase A, read-only, no pulser:
1. `*IDN?`, `SYST:VERS?`, `SYST:ERR:COUN?`. Record firmware.
2. Query every `STATE_HEADERS` entry. Note which ones answer, their exact reply format
   (case of enums, `0/1` vs `ON/OFF`, exponent notation), and which time out.
3. `DATA:PORT?` (unknown 1). Does the reply change between sessions or after `*RST`?
4. Does a bare `\n` terminator work (unknown 13)? Try one harmless query over a second
   raw socket with `\n` only. The tool runs it last, on its own connection, because on 2026-10-05
   (fw 1.16) the main connection died right after a second connection to port 5025 was
   opened and closed.
5. Error queue: send one undefined header, read `SYST:ERR?` twice. Depth: send 25 bad
   headers, count entries.

Phase B, pulser off, acquisition:
6. `TRAN:ENAB OFF`, `DATA:LENG 1024`, `FREQ 100 MHZ`, `TRIG:MODE INT`, `TRIG:INT 10 MS`.
   Open the data socket, `STAR AUTO`, read 10 packets, `STOP`. Record: packet rate,
   `recv` chunk sizes, header fields (`length_lo/hi`, `packet_number` increments,
   `ascan_count`, `buffer_fill`, `is_full`), noise floor in counts.
7. Repeat with `AVER:COUN 4`: does `ascan_count` change, does the amplitude of noise
   change like a mean (divided by 4 or 16) or like a sum (unknown 3)?
8. `STOP` while the socket is open: does the device close it, or keep it idle?
   Then connect the data socket after `STAR AUTO`: does data arrive (unknown 7)?
9. Change `GAIN` during acquisition: accepted, error, or ignored (unknown 7)?

Phase C, pulser on at the agreed voltage, transducer as confirmed:
10. `TRAN:PULS 20 V`, `TRAN:FREQ` at the transducer's frequency, `TRAN:ENAB ON`,
    acquire 10 packets. Where is the main bang in time? That fixes time zero and what
    `TRIG:DEL` shifts (unknown "time base").
    Run as `tools/hw_probe.py --phase C --pulse-v 20` (steps 10 and 10b of the tool).
11. `TRIG:DEL 0 NS` vs `TRIG:DEL 5 US`: does the echo move by 5 us?
    Step 11 of the same invocation (`--phase C`) measures it for `TRIG:DEL` 0 NS, 1000 US and 2000 US.
12. Count-to-volt scaling (unknown 2): with a known signal source if available, otherwise
    skip and record "not measured".
    Deferred on 2026-10-05 (signal generator available later).
13. `*RST`: query all `STATE_HEADERS` again and diff against the documented defaults
    (unknown 14). Then restore the saved setup and verify by read-back.
    Run as `tools/hw_probe.py --phase RST --allow-rst`.

Phase D, cleanup:
14. Restore the saved setup, `TRAN:ENAB OFF`, `STOP`, close. Verify by read-back that the
    device is exactly as found in step 2.

## After the session

- README section "Measured on the device" gets one entry per experiment, with date and
  firmware. Contradicted vendor statements are listed explicitly.
- Every behavioural difference becomes a change to the fake, with a test.
- `STATUS.md` is updated: resolved unknowns removed, new ones added.
