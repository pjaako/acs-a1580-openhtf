# A1580 protocol digest

- Transport is TCP/IP only. Every vendor file reaches the device through IP sockets (EX:182-183, CF:26-27, RX:63, WS:132).
- SCPI runs on TCP port 5025. Lines end with CRLF. Setters send no reply. Errors are read from the error queue with `SYSTem:ERRor?` (EX:179-187, SC:44-66).
- The binary A-scan stream is a separate TCP connection to the port returned by `DATA:PORT?`. The vendor code default is 2758. The vendor doc example shows 5025 (CONFLICT 1) (EX:180,229-230; SC:155-173).
- REST on port 80 (`/api/v1/`, `/config/`) and a WebSocket with subprotocol `server-websocket` on port 80 are an alternative control and data path (RA:5-8, WS:134).
- No USB, serial, GPIB or other bus appears anywhere in the vendor material (all files at the commit below were searched).

| Item | Value |
|---|---|
| Vendor repo | `https://github.com/Acoustic-Control-Systems/a1580_examples` (the `origin` remote of the local clone) |
| Source commit | `6a9c3003b015743da1488f7585824177af743e3c` |
| Commit date and subject | 2026-07-16 12:31:18 +0200, "Add detailed SCPI command descriptions and new commands for error handling and averaging" |
| How obtained | `git -C /home/user/acoustic-control-systems/a1580_examples rev-parse HEAD`. The folder `/home/user/acoustic-control-systems` itself is not a git repo; the repo is its subfolder `a1580_examples`. The working tree was clean. |
| Digest date | 2026-10-04 |

## How to read this file

Every claim carries a citation of the form `FILE:line` or `FILE:first-last`. Paths are relative to the vendor repo root.

| Tag | Vendor file |
|---|---|
| `RM` | `README.md` |
| `SC` | `SCPI_COMMANDS.md` |
| `RA` | `REST_API.md` |
| `EX` | `SCPI_Python/example_scpi_protocol.py` |
| `CF` | `SCPI_Python/common_functions.py` |
| `RQ-S` | `SCPI_Python/requirements.txt` (UTF-16 encoded; decode with `iconv -f UTF-16 -t UTF-8`) |
| `RX` | `REST_API_Python/example_rest_api.py` |
| `WS` | `REST_API_Python/ascan_websocket.py` |
| `RR` | `REST_API_Python/README.md` |
| `RQ-R` | `REST_API_Python/requirements.txt` |

Markers used in this file:

* `UNKNOWN`: the vendor material does not say. Do not fill it in. Verify on hardware (see the checklist near the end).
* `CONFLICT n`: two vendor sources disagree. Both sides are shown. The numbered list is in the section "Conflicts between vendor documents".
* `(inferred)`: not stated by the vendor. It is a reading of the examples or code, and it is labelled so.
* "Doc" means a vendor Markdown file. "Code" means a vendor Python file.

## Transport

| Item | Value | Source |
|---|---|---|
| SCPI command port | TCP 5025. Only the code states it (variable comment "scpi command port"). `SC` never names the command port. | EX:179,183 |
| A-scan data port (raw TCP) | Read it with `DATA:PORT?` on the SCPI session. Code initial value 2758, then overwritten by the reply. `SC` example shows 5025. CONFLICT 1 | EX:180,229-230; RR:91; SC:166-170 |
| REST HTTP port | 80 by default, "configurable". The curl examples for `/api/v1/` use 8080 and for `/config/` use 80. CONFLICT 9 | RA:5,543; RX:56; RR:37; RA:182,205 |
| WebSocket | `ws://<ip>:80`, no URL path, subprotocol `server-websocket`. Whether it shares the REST listener is UNKNOWN (code has a separate `WEBSOCKET_PORT` variable that is also 80). | RX:59,71; WS:83,134; RR:38 |
| Default IP | 192.168.200.18. It is the default in all vendor code and examples. No document says it is the factory default. | EX:178; RX:53; RR:36; RA:91,387 |
| IP configuration | Read and write `ip_address` with `GET/POST /config/dev.eth` or the parameter `ip_address`. DHCP versus static, netmask and gateway are UNKNOWN. Whether a change needs a reboot, and whether the connection drops, is UNKNOWN. | RA:132,384-389 |
| SCPI terminators | Write `\r\n`. Read until `\r\n`. A bare `\n` is UNKNOWN. | EX:186-187 |
| Data stream framing | Binary, no terminator. See "Acquisition and data stream". | WS:191-245 |
| Text encoding | `iso-8859-1` | EX:184 |
| SCPI timeout used by vendor | 5000 ms | EX:185 |
| Data socket timeouts | `recv` timeout 1.0 s, set after `connect`. No explicit connect timeout. | CF:26-30 |
| REST timeout used by vendor | 5 s per request | RX:103,180 |
| WebSocket connect timeout used by vendor | 5 s | WS:132-136 |
| VISA resource string | `tcpip::192.168.200.18::5025::SOCKET` (lower-case `tcpip` exactly as written) | EX:183 |
| VISA stack | `pyvisa.ResourceManager()` with no backend named. The requirements list `pyvisa` and `pyvisa-py`. A `::SOCKET` resource is a raw TCP socket; the vendor uses no VXI-11 or HiSLIP. A plain TCP socket client does the same job (note, not a vendor statement). | EX:182; RQ-S |
| Wi-Fi access point | The device has a Wi-Fi AP with a 2G or 5G band. The only values shown are in an example payload: `ap_address` `192.168.210.1`, `ap_type` `2G`, SSID `SSID_2G` and `SSID_5G`, password `PASSWORD`. They look like placeholders. They are not stated as defaults. Real defaults are UNKNOWN. | RA:133-138,391-407 |
| Authentication | REST: none, "intentionally". SCPI: none seen in code. The REST API can read the Wi-Fi passwords (`pass_2g`, `pass_5g`). | RA:136,138,532 |
| Discovery | None documented. No mDNS, broadcast, LXI or similar is described. `zeroconf` and `psutil` in `RQ-S` are listed as `pyvisa-py` extras and do not show that the device announces itself. UNKNOWN. | RQ-S |

## SCPI session protocol

### Write and query semantics

* One command is one text line ended by CRLF (EX:187). The vendor uses pyvisa `write` (send line) and `query` (send line, then read one line up to CRLF, terminator stripped).
* Setters get no reply (inferred). Every setter in `SC` is shown with a `>` line and no `<` line, for example SC:120,150,222,506. The vendor script never reads after a write. No vendor text states this.
* Queries end in `?` and return exactly one line. No multi-line reply or binary block is documented.
* Mixed case and short forms are used by the vendor: `FREQ`, `TRAN:PULS`, `TRAN:FREQ 2500 KHz`, `TRIG:MODE INTERNAL`, `MODE MASTer` (EX:15,25,76,91). Case-insensitivity is therefore implied but not stated.
* Numbers are sent with a unit suffix in nearly all vendor examples (`FREQ 100 MHZ`, `TRIG:INT 100000 US`, `TRIG:DEL 0 NS`, `TRAN:PULS 100 V`). Bare numbers are used for `GAIN 10`, `DATA:LENG 8192`, `TRAN:DUR 1`, `AVER:COUN 0` (EX:15,81,96,31,61,20,41,71). The unit assumed for a bare number is UNKNOWN for the commands that take a unit.
* The keywords `MINimum`, `MAXimum`, `DEFault`, `UP`, `DOWN` are documented for most numeric commands (see the table). The vendor script never uses them.
* Multiple commands on one line (`;`): not documented and never used. UNKNOWN.

### Error queue

* Read one entry with `SYSTem:ERRor?` (long form `SYSTem:ERRor[:NEXT]?`). Reply format is `<numeric>,<string>` (SC:46-57).
* `*CLS` clears the error queue (SC:31). `SYSTem:ERRor:COUNt?` returns the number of stored entries (SC:68-79).
* Vendor parsing: split on the first comma; first part is the integer; second part is kept as is, including quotes and any leading space (CF:17-21).
* Whitespace after the comma is inconsistent in the doc: `0, "No error"` (SC:62) versus `-113,"Undefined header;Command: SYST:ERRrr"` (SC:65). A driver must accept both.
* An empty queue answers number 0 with text `No error` (SC:62).
* Queue depth, overflow behaviour and the full list of codes are UNKNOWN.

Error numbers seen in the vendor material (these are the only ones):

| Number | Text | Context | Source |
|---|---|---|---|
| 0 | `No error` | queue empty | SC:62 |
| -113 | `Undefined header;Command: SYST:ERRrr` | after a mistyped header `SYSTem:ERRrr?`. The text carries the offending header in short form, as typed `SYSTem:ERRrr`. | SC:63-65 |
| -221 | `Settings conflict` | setting the constant averaging delay while `AVERage:DELay:CONStant:AUTO` is ON | SC:864 |

The numbers -113 and -221 coincide with standard SCPI error numbers. That is general SCPI knowledge, not a vendor statement.

The doc transcript for the error queue (SC:59-66), copied as written. Note the doc shows no reply line for the mistyped query:

```
> SYSTem:ERRor?
< 0, "No error"
> SYSTem:ERRrr?
> SYSTem:ERRor?
-113,"Undefined header;Command: SYST:ERRrr"
```

### Identification

`*IDN?` returns four comma-separated fields: manufacturer, model, serial number, firmware version (SC:16-17). Doc example (SC:21-22):

```
> *IDN?
< ACS-Solutions GmbH,A1580-HF,100500,1.6.b41
```

The vendor script logs the reply and does not parse it (EX:196-197).

### Synchronisation commands

`*OPC`, `*OPC?` and `*WAI` are listed as registered (SC:27,35-36,42). Their meanings are in the common-command table below. Whether any command runs asynchronously, so that synchronisation is needed, is UNKNOWN.

### What the vendor example does and does not do

Does (EX line numbers):

* Opens the socket, sets encoding, timeout and terminators (EX:182-187).
* Drains the error queue in a loop of `SYSTem:ERRor?` until the number is 0, before any other command. The loop has no iteration limit (EX:189-193).
* Sends `*IDN?`, `STOP`, `MEM:CLEar` (EX:196,201,204).
* Writes each parameter, then queries it back and logs it (EX:13-122).
* Asserts read-back only for the optional TGC helpers: exact float equality of the linear TGC reply (EX:134) and exact string equality of the TGC mode reply, `LINear` or `ARBitrary` (EX:139,155).

Does not:

* Never reads the error queue after configuring. The only `SYSTem:ERRor?` calls are at EX:190-193, before the first setter.
* Never uses `*OPC`, `*OPC?`, `*WAI`, `*CLS`, `*RST`, `*ESR?`, `*STB?`, `*TST?`, `SYSTem:ERRor:COUNt?` or `SYSTem:VERSion?`.
* Never compares read-back values with what it wrote, apart from the TGC asserts above.
* Never checks that `STAR AUTO` took effect and never queries the device after `STOP`.
* Never uses `MINimum`, `MAXimum`, `DEFault`, `UP`, `DOWN`.
* Never reconnects. It opens one data connection per run (EX:244).

## SCPI command reference

How to read the table:

* The Command column copies the vendor syntax line, including optional parts in square brackets. Capital letters mark the short form, as the vendor writes it. Add `?` for the query form, where a query exists.
* "Default" is the value the doc labels `DEFault`. It is not proven to be the power-on or `*RST` state (UNKNOWN, see checklist).
* "UP/DOWN step" is what the doc says. Where the doc gives no step, the cell says UNKNOWN.
* Argument units: the vendor writes units after the number (`MHZ`, `KHZ`, `US`, `NS`, `V`, `DB`, `S`, `MS`). Query replies are plain numbers in the units shown in the reply column.
* The `MINimum`, `MAXimum`, `DEFault`, `UP`, `DOWN` keywords are listed only where the doc lists them.
* The Source column gives the doc lines first, then the vendor script lines that use the command.

### Instrument commands

| Command | Meaning | Argument values / range | Default | Query reply format and units | UP/DOWN step | Source |
|---|---|---|---|---|---|---|
| `[SOURce:]FREQuency` | Sampling frequency for AD conversion of the input signal | Numeric 1, 2, 5, 10, 25, 50 or 100 (MHz). Keywords: MINimum (1 MHz), MAXimum (100 MHz), DEFault, UP, DOWN | 100 MHz | Integer in Hz, example `100000000`. The vendor script calls `int()` on it. | UNKNOWN ("increases/decreases the current value") | SC:96-123; EX:15-17 |
| `[SOURce:]DATA:LENGth` | Number of samples in one A-scan vector | Numeric 1024 to 36864. Keywords: MINimum (1024), MAXimum (36864), DEFault, UP, DOWN. Whether values that are not a multiple of some block size are accepted is UNKNOWN. | 1024 | Numeric, samples, example `1024` | 1 | SC:125-153; EX:19-22,224-226 |
| `[:SOURce]:DATA:PORT?` | TCP port of the A-scan data stream. Query only. | none | UNKNOWN | Numeric 0 to 65535. Example `5025`. CONFLICT 1. The vendor script calls `int()` on it. | n/a | SC:155-173; EX:228-230 |
| `[:SOURce]:MODE` | Master or slave operation | `MASTer` or `SLAVe` | UNKNOWN | Doc format `MASTer` or `SLAVe`. Doc example sends `MASTER` and shows reply `MASTER`. CONFLICT 14 | n/a | SC:175-197; EX:90-93 |
| `[SOURce:]TRANsmitter:ENABle` | Whether the transmitter generates a pulse | OFF, ON, 0, 1 | OFF | Doc format `OFF` or `ON`. Example `ON`. | n/a | SC:199-225; EX:35-38 |
| `[SOURce:]TRANsmitter:TYPE` | Transducer type. DUAL: dual-crystal or through transmission, burst on the "OUT" socket. SINGle: single-crystal, burst on the "IN" socket. | `DUAL` or `SINGle` | SINGle | Doc format `DUAL` or `SINGle`. Example `DUAL`. | n/a | SC:227-252; EX:55-58 |
| `[SOURce:]TRANsmitter:REVerse` | Initial burst polarity. OFF or 0: starts positive. ON or 1: starts negative. | OFF, ON, 0, 1 | OFF | Doc format lists OFF, ON, 0, 1. Example `ON`. | n/a | SC:254-279; EX:45-48 |
| `[SOURce:]TRANsmitter:PULSe[:LEVel]` | Burst amplitude | Numeric 5 to 100 (V). Keywords: MINimum (5 V), MAXimum (100 V), DEFault, UP, DOWN | 20 V | Numeric, volts, example `20` | 5 V | SC:281-309; EX:30-33 |
| `[SOURce:]TRANsmitter:FREQuency` | Burst frequency | 10 to 20000 KHZ. Keywords: MINimum (10 KHZ), MAXimum (20 MHZ), DEFault, UP, DOWN | 5 MHz | Hertz, example `100000`. The vendor script calls `int()` on it. | 1 KHZ | SC:311-339; EX:24-28 |
| `[SOURce:]TRANsmitter:DURation` | Number of periods in the burst. The vendor code comment questions this: "halfperiods?" (EX:40). | Numeric 0.5 to 16. Keywords: MINimum (0.5), MAXimum (16), DEFault, UP, DOWN. CONFLICT 2 (REST doc says 0.5 to 8) | 1 | Numeric, example `5` | 0.5 | SC:341-369; EX:40-43 |
| `[:SOURce]:TRANsmitter:GAP` | Gap between transmitter pulses. The vendor script labels it a "debug parameter" (EX:113-114). | 0 to 500 NS. Keywords: MINimum (0 NS), MAXimum (500 NS), DEFault, UP, DOWN | 5 NS | Nanoseconds, example `200` | 5 NS | SC:371-399; EX:113-117 |
| `[SOURce:]TRIGgering:MODe` | Which event starts an acquisition. INTernal: periodic, one acquisition every Triggering Interval. CTP, ENCoder, TTL: trigger from that input. | `INTernal`, `CTP`, `ENCoder`, `TTL`. CONFLICT 5 (REST doc says SENSOR instead of ENCoder) | UNKNOWN | Doc format lists the four names. Doc example sends `INT` and shows `INT`. The vendor script sends `INTERNAL`. CONFLICT 14 | n/a | SC:401-427; EX:75-78 |
| `[SOURce:]TRIGgering:INTerval` | Time between two acquisitions in INTernal mode | 100 US to 10 s. Keywords: MINimum (100 US), MAXimum (10 S), DEFault, UP, DOWN | 10 ms | Seconds. Doc example: `TRIG:INT 100000 US` then reply `100.0E-3` | 100 US | SC:429-458; EX:80-83 |
| `[:SOURce]:TRIGgering:DELay` | Delay between a trigger event and the acquisition. Stored in nanoseconds. | 0 to 2147483647 NS. Keywords: MINimum (0 NS), MAXimum (2147483647 NS), DEFault, UP, DOWN. CONFLICT 3 (REST doc says samples) | 15000 NS | Nanoseconds. Doc example: `TRIG:DEL 15 US` then reply `15000`. The vendor script logs it as seconds (CONFLICT 4). | 1 NS | SC:460-489; EX:95-98 |
| `[SOURce]:STARt` | Start a single acquisition or a sequence of acquisitions | `AUTO` is the only documented argument. A single-shot form is UNKNOWN. | n/a | No query. | n/a | SC:491-508; EX:247 |
| `[SOURce:]STOP` | Stop a sequence of measurements. The doc says "Stop is not needed in the single ascan mode" but documents no single-scan command. | none | n/a | No query. | n/a | SC:510-524; EX:200-201,254 |
| `[SOURce:]GAIN[:LEVel]` | Constant analog amplification. Base level for the TGC modes. | Numeric 0 to +80 (dB). Keywords: MINimum (0 dB), MAXimum (+80 dB), DEFault, UP, DOWN | 0 dB | Numeric, dB, example `10` | 1 dB | SC:526-554, 735, 767; EX:60-63 |
| `[SOURce]:TRANsmitter:DAMP[:ENABle]` | Pulse damping on or off | OFF, ON, 0, 1 | OFF | Doc format `0` or `1`. Doc example sends `ON` and shows reply `0`. CONFLICT 6 | n/a | SC:556-581; EX:50-53 |
| `[:SOURce]:TRANsmitter:DAMP:GAP` | Timing gap used by the damping circuit. Stored in nanoseconds. | 10 to 500 NS. Keywords: MINimum (10 NS), MAXimum (500 NS), DEFault, UP, DOWN | 10 NS | Nanoseconds, example `20` | 5 NS | SC:583-612; EX:119-122 |
| `[SOURce]:TRANsmitter:IMPedance` | Input impedance | `HIGH`, `200`, `1000` (ohm). HIGH means HIGH_Z. | HIGH | Doc format `HIGH`, `200` or `1000`. Example `HIGH`. | n/a | SC:614-640; EX:85-88 |
| `[:SOURce]:GAIN:PREamp:COMBined` | Combined preamplifier path, adds 20 dB, for low signal levels | OFF, ON, 0, 1 | UNKNOWN | Doc format `0` or `1`. Example `1`. | n/a | SC:642-664 |
| `[:SOURce]:GAIN:PREamp:SPLIT` | Split preamplifier path, adds 20 dB, for low signal levels. Whether COMBined and SPLIT can both be on, and whether they then add 40 dB, is UNKNOWN. | OFF, ON, 0, 1 | UNKNOWN | Doc format `0` or `1`. Example `1`. | n/a | SC:666-688 |
| `[:SOURce]:GAIN:TGC:MODE` | Whether and how TGC is applied: OFF is constant gain, LINear is gain rising linearly with time, ARBitrary is a curve of time and gain points | `OFF`, `LINear`, `ARBitrary` | OFF | Doc format `OFF`, `LINear` or `ARBitrary`. Doc example `OFF`. The vendor script asserts the exact strings `LINear` and `ARBitrary`. CONFLICT 14 | n/a | SC:690-716; EX:65-68,136-140,152-156 |
| `[:SOURce]:GAIN:TGC:LINear` | Linear TGC parameters: two floats, offset in microseconds then slope in dB per microsecond, separated by a comma. Gain is constant (the `GAIN` level) from 0 to offset, then changes linearly. The slope may be positive, negative or zero. Gain stops at the system limits. | `<offset_us>, <slope_dB_per_us>`. Example `20, 0.1`. | UNKNOWN | "The same as input". Doc example sends `20, 0.1` and shows `20.0, 0.1`. The vendor script compares the two parsed floats with exact equality. | n/a | SC:718-748; EX:124-140 |
| `[:SOURce]:GAIN:TGC:ARBitrary` | Arbitrary TGC curve: a comma-separated list of 2N floats, pairs of time in microseconds and gain in dB. The gains are added to the `GAIN` level. Worked example in the doc: constant gain 40 dB and points `10,5,20,10` give 40 dB until 10 us, a ramp 45 to 50 dB from 10 to 20 us, then 50 dB. Maximum N, point ordering rules and limits are UNKNOWN. | `t1,g1,t2,g2,...` (see CONFLICT 17 about the doc's order typo) | UNKNOWN | "The same as input". Doc example `0,5,2,20,5,20,10,40,30,10`. The vendor script builds the same string without spaces. | n/a | SC:750-782; EX:142-156 |
| `MEMory:CLEar` | Clean internal data buffers | none | n/a | No query. | n/a | SC:786-799; EX:203-204 |
| `SENSe:AVERage:COUNt` | Acquisitions per averaged vector. The device pulses and acquires several times in a row and averages internally when the value is greater than 0. CONFLICT 10 about whether the number is a count or an exponent. | Numeric 0 to 8 | UNKNOWN | Numeric, example `5` | n/a | SC:803-825; EX:70-73 |
| `[:SENSe]:AVERage:DELay:CONStant[:VALue]` | Constant part of the pause between acquisitions in averaging mode. Pause is FixedDelay (hardware, 22 us) plus this value plus a random part. | 0 to 2147483647 NS. Keywords: MINimum (0 NS), MAXimum (2147483647 NS), DEFault, UP, DOWN | 10 US | Seconds (the argument is in NS, the reply in seconds). Doc example sends `50 US`, reply `50.0E-6`. | 1 NS | SC:827-857; EX:105-106 (query only) |
| `[:SENSe]:AVERage:DELay:CONStant:AUTO` | Automatic computing of the constant averaging delay from the averaging count and sampling rate. While ON, setting the constant delay is ignored and error -221 `Settings conflict` is raised. | `ON` or `OFF` | UNKNOWN | `<char>`, example `ON` | n/a | SC:859-886; EX:100-103 |
| `[:SENSe]:AVERage:DELay:RANDom` | Upper bound of the random part of the pause between averaged acquisitions (random value from 0 to this bound) | 0 to 32767 NS. Keywords: MINimum (0 NS), MAXimum (32767 NS), DEFault, UP, DOWN | 2 US | Seconds. Doc example sends `2 US`, reply `2.0E-6`. | 1 NS | SC:888-918; EX:108-111 |
| `[:SENSe]:FILTer:HPASs:INDex` | Analog high-pass filter selected by index: 0 is 1 kHz, 1 is 100 kHz, 2 is 500 kHz, 3 is 1000 kHz cut-off | `0`, `1`, `2`, `3` | UNKNOWN | `<char>`, example `1`. The doc example sends the query without `?` (SC:947), a doc typo. | n/a | SC:920-949 |
| `SYSTem:ERRor[:NEXT]` | Read the next entry of the error queue. Query only. | none | n/a | `<numeric>,<string>`. See "Error queue". | n/a | SC:46-66; CF:12-15; EX:190-193 |
| `SYSTem:ERRor:COUNt` | Number of entries in the error queue. Query only. | none | n/a | `<numeric>` | n/a | SC:68-79 |
| `SYSTem:VERSion` | SCPI standard version implemented. Query only. | none | n/a | `<numeric>`. No example reply is given; the value is UNKNOWN. | n/a | SC:81-92 |

Every command the vendor script sends is in this table (checked against EX:13-262). The short forms the script uses (`FREQ`, `TRAN:PULS`, `TRIG:INT`, `AVER:COUN`, `SENS:...`) follow the capital-letter rule in the Command column. The doc itself uses `SENS:AVER:COUNT` (SC:822), the long form of the last node.

### Common commands

| Command | Meaning | Reply | Source |
|---|---|---|---|
| `*IDN?` | Identification: manufacturer, model, serial number, firmware version | Four comma-separated fields. Example `ACS-Solutions GmbH,A1580-HF,100500,1.6.b41` | SC:3-23; EX:196 |
| `*CLS` | Clear status data structures and the error queue | none | SC:31 |
| `*ESE <numeric>` | Set the Standard Event Status Enable register | none | SC:32 |
| `*ESE?` | Query that register | `<numeric>` | SC:33 |
| `*ESR?` | Query and clear the Standard Event Status register | `<numeric>` | SC:34 |
| `*OPC` | Set the Operation Complete event when pending operations finish | none | SC:35 |
| `*OPC?` | Wait for pending operations to finish | `1` | SC:36 |
| `*RST` | Reset the instrument. What it resets is UNKNOWN. | none | SC:37 |
| `*SRE <numeric>` | Set the Service Request Enable register | none | SC:38 |
| `*SRE?` | Query that register | `<numeric>` | SC:39 |
| `*STB?` | Query the Status Byte register | `<numeric>` | SC:40 |
| `*TST?` | Run the self-test | `0` when the test passes | SC:41 |
| `*WAI` | Wait for pending operations before processing later commands | none | SC:42 |

Only `*IDN?` is used by the vendor script.

## Acquisition and data stream

### Start and stop

| Path | Start | Stop | Source |
|---|---|---|---|
| SCPI | `STAR AUTO` (`[SOURce]:STARt AUTO`). Starts a sequence of acquisitions. | `STOP` | SC:491-524; EX:247,254 |
| REST | `POST /api/v1/start_auto_ascan` | `POST /api/v1/stop_auto_ascan`. The vendor code also stops with `start_auto_ascan` set to 0 (CONFLICT 11). | RA:174-175; RX:364,370,801,809 |

* Single acquisition: `SC` says `STARt` "will start a single acquisition or a sequence" (SC:494) and that `STOP` "is not needed in the single ascan mode" (SC:513). It documents only the argument `AUTO`. A single-shot command is UNKNOWN.
* In INTernal trigger mode one acquisition happens every `TRIG:INT` seconds (SC:414). How averaging (`AVER:COUN`), trigger delay and packet rate combine is UNKNOWN.

### Rule: open the data socket before `STAR AUTO`

* `SC` says the controller "should establish a connection to the instrument using this port number before starting acquisition" and that the instrument then sends acquired data to that connection (SC:158-159).
* The vendor script does it in that order: read `DATA:PORT?`, start the reader thread (it connects), then write `STAR AUTO` (EX:229-247).
* The REST and WebSocket examples connect the WebSocket first, then start (RX:362-364), but `RR` and the quick reference start first, then connect (RR:102-112; RX:1072-1081). CONFLICT 12.
* The vendor script stops the reader (and closes the data socket) before it sends `STOP` (EX:252-254). What the device does when its data peer disappears while still running is UNKNOWN.

### Packet layout

Everything below applies to the raw TCP data port and to WebSocket messages. `RR` states that the raw socket gives "the same binary data stream" (RR:91). The SCPI script's `shorts[14:]` (EX:166) cuts the same 28-byte header.

* Header struct format: `'<4s3IH2B6B2B'` (WS:279). Little-endian, no padding. Size 28 bytes (checked with `struct.calcsize`: 28). It yields 15 values.
* Magic: `FtH1` = bytes `0x46 0x74 0x48 0x31` (WS:98).
* Samples: signed 16-bit little-endian integers, `length` of them, starting at offset 28 (WS:299-305).
* Packet size in bytes = `28 + 2 * length`, where `length` is the `DATA:LENG` value (WS:206-208; EX:232-233). The client must know `length`; the packet does not self-describe in any way the vendor code uses (WS:74-76 says "MUST match"). Example: `DATA:LENG 8192` gives 16412 bytes (EX:233).

| Offset | Size | struct code | Field | Vendor description | Source |
|---|---|---|---|---|---|
| 0 | 4 | `4s` | `magic` | Magic bytes `FtH1`, packet start marker | WS:254,284 |
| 4 | 12 | `3I` (three uint32) | `ctp[0..2]` | "CTP timing array" in the table and in `RX`. The diagram says "CTP coordinate (12 bytes), X,Y,Z coordinates". CONFLICT 13. Real meaning and units UNKNOWN. | WS:255,285; WS:62; RX:994 |
| 16 | 2 | `H` (uint16) | `length_lo` | "Length low word". Meaning UNKNOWN. | WS:256,286 |
| 18 | 1 | `B` | `length_hi` | "Length high byte". If it pairs with `length_lo` as a 24-bit value, the unit (samples or bytes) is UNKNOWN. | WS:257,287 |
| 19 | 1 | `B` | `packet_number` | "Sequential packet counter". It is one byte; wrap behaviour UNKNOWN. | WS:258,288 |
| 20 | 1 | `B` | `telemetry_a` | "Telemetry byte A". UNKNOWN. | WS:259,289 |
| 21 | 1 | `B` | `telemetry_b` | "Telemetry byte B". UNKNOWN. | WS:260,290 |
| 22 | 1 | `B` | `telemetry_c` | "Telemetry byte C". UNKNOWN. | WS:261,291 |
| 23 | 1 | `B` | `is_full` | "Buffer full flag" (0 or 1). Meaning of the buffer UNKNOWN. | WS:262,292 |
| 24 | 1 | `B` | `buffer_fill` | "Buffer fill level (0-255)". UNKNOWN. | WS:263,293 |
| 25 | 1 | `B` | `ascan_count` | "Number of A-scans" (table) and "Number of A-scans accumulated" (code comment, `RX`). UNKNOWN. | WS:264,294; RX:995 |
| 26 | 1 | `B` | `reserved_b` | Reserved | WS:265,295 |
| 27 | 1 | `B` | `reserved_c` | Reserved | WS:265,296 |
| 28 | `2 * length` | `<{length}h` | samples | Int16 sample array | WS:266,299-305 |

The class docstring diagram (WS:58-72) lists fields of 4, 12, 3, 1, 3, 4 and 2 bytes. That sums to 29, not 28. The table and the struct format sum to 28 and agree with each other and with `EX:166`. This digest uses the table and the struct format. The firmware truth is UNKNOWN.

### Resynchronisation algorithm (from `ascan_websocket.py`, WS:191-245)

The client keeps one byte buffer for the whole stream and does the following each time a message (or chunk) arrives.

1. Append the new bytes to the buffer.
2. Compute `packet_size = 28 + 2 * ascan_length`, with `ascan_length` set by the caller to match the device.
3. Search the buffer for the first occurrence of `FtH1`.
4. If there is none: if the buffer is longer than `2 * packet_size`, keep only its last `packet_size` bytes. Then stop and wait for more data.
5. If it is found at an index above 0: throw away the bytes before it.
6. If the buffer now holds fewer than `packet_size` bytes: stop and wait for more data.
7. Otherwise cut exactly `packet_size` bytes as one packet, parse it, hand it to the callbacks, remove it from the buffer, and go back to step 3.

Observations from reading the code (analysis, not vendor statements):

* The parser does not use `length_lo` or `length_hi`, does not check that the next packet also begins with `FtH1` before consuming, and does not check `packet_number` continuity.
* The client clears its buffer on connect (WS:140), so joining mid-stream is handled by step 5.
* A wrong `ascan_length` is not detected. If it is too small, the rest of each packet is discarded as garbage up to the next magic. If it is too large, the next packet's header is swallowed as samples. Neither raises the parse-error counter.
* The sample bytes could by chance contain `46 74 48 31` (as int16 pair 29766, 12616). That only matters when the stream is already misaligned.
* The WebSocket client passes `timeout=5` to `create_connection` (WS:135). A receive timeout in the library would end its loop through the generic `except` at WS:185-189. This is untested.
* The docstring says one WebSocket message "may contain one or more complete packets" and that the device "may send partial packets" (WS:173,195). So message boundaries do not match packet boundaries.

### Known weakness of `common_functions.read_binary_data` (CF:23-50)

* It calls `s.recv(length_bytes)` (CF:35), where `length_bytes = 2 * DATA:LENG + 28` (EX:233), and stores each return value as one packet in a dict (CF:42).
* TCP is a byte stream. `recv(n)` returns any number of bytes from 1 to `n`. The code assumes one `recv` returns exactly one whole packet. A packet that does not fit in one TCP segment will usually arrive in pieces. Chunks can start mid-packet or contain the end of one packet and the start of the next.
* `unpack_and_plot_data` then cuts a fixed 14 int16 values as the header from every chunk (EX:166). That is wrong for any chunk that does not start at a packet boundary.
* An odd-length chunk makes `struct.unpack` raise `struct.error` (checked: `unpack('<hh', b'12345')` raises), because the format built at EX:165 is `len//2` shorts and the buffer is longer.
* Any exception other than a timeout is logged and the loop continues (CF:47-49). A persistent error therefore spins.
* Design note for the plug (not vendor): implement the framing of `ascan_websocket.py` on the raw socket, or read exactly 28 header bytes, check `FtH1`, then read exactly `2 * length` bytes, and resync on a magic mismatch.

### Time base

* Sampling frequency `fs` is the `FREQ?` reply in Hz (SC:115-116). Sample index `n` starts at 0. Time of sample `n` after the start of the vector is `t[n] = n / fs` seconds.
* The REST example computes microseconds per sample as `1.0 / sampling_freq` with `sampling_freq` in MHz and then `peak_index * time_per_sample` (RX:398-399,423).
* Vector duration is `DATA:LENG / fs`. Arithmetic only: 1024 samples at 100 MHz is 10.24 us, and 36864 samples at 100 MHz is 368.64 us.
* What time zero means is UNKNOWN: trigger event, pulse start, or end of `TRIG:DEL`. Whether `TRIG:DEL` shifts sample 0 is UNKNOWN. The relation between TGC times (microseconds) and sample time is UNKNOWN.

### Amplitude scale

Count-to-volt scaling, ADC bit depth and full-scale input range are UNKNOWN. Nothing in the vendor material states them. `int16` is only the transport type (WS:303). `SC` says only that `FREQuency` is the frequency "for AD conversion" (SC:98). The input gain range is 0 to +80 dB (SC:539-545). Whether the device applies the gain and TGC digitally or in analogue before the ADC is UNKNOWN.

## Vendor example call sequence (SCPI)

This is the exact order of calls in `example_scpi_protocol.py` for its default run (both TGC flags False, EX:212,217). `W` is `inst.write`, `Q` is `inst.query`. Text after `#` is a source note and not part of the string. Each string is sent with `\r\n` appended. `DATA:LENG 8192` is the expansion of the f-string `DATA:LENG {data_length}` with `data_length = 8*1024` (EX:207).

```
(connect)                       # TCP 192.168.200.18:5025, iso-8859-1, timeout 5000 ms, CRLF both ways (EX:182-187)
Q SYSTem:ERRor?                 # repeated until the reply number is 0 (EX:190-193, CF:12-15)
Q *IDN?                         # EX:196
W STOP                          # EX:201
W MEM:CLEar                     # EX:204
W FREQ 100 MHZ                  # EX:15
Q FREQ?                         # EX:16, parsed with int()
W DATA:LENG 8192                # EX:20
Q DATA:LENG?                    # EX:21
W TRAN:FREQ 2500 KHz            # EX:25
Q TRAN:FREQ?                    # EX:26, parsed with int()
W TRAN:PULS 100 V               # EX:31
Q TRAN:PULS?                    # EX:32
W TRAN:ENAB ON                  # EX:36
Q TRAN:ENAB?                    # EX:37
W TRAN:DUR 1                    # EX:41
Q TRAN:DUR?                     # EX:42
W TRAN:REVerse OFF              # EX:46
Q TRAN:REVerse?                 # EX:47
W TRAN:DAMP:ENAB ON             # EX:51
Q TRAN:DAMP:ENAB?               # EX:52
W TRAN:TYPE SINGle              # EX:56
Q TRAN:TYPE?                    # EX:57
W GAIN 10                       # EX:61
Q GAIN?                         # EX:62
W GAIN:TGC:MODE OFF             # EX:66
Q GAIN:TGC:MODE?                # EX:67
W AVER:COUN 0                   # EX:71
Q AVER:COUN?                    # EX:72
W TRIG:MODE INTERNAL            # EX:76
Q TRIG:MODE?                    # EX:77
W TRIG:INT 100000 US            # EX:81
Q TRIG:INT?                     # EX:82
W TRAN:IMP 200                  # EX:86
Q TRAN:IMP?                     # EX:87
W MODE MASTer                   # EX:91
Q MODE?                         # EX:92
W TRIG:DEL 0 NS                 # EX:96
Q TRIG:DEL?                     # EX:97
W AVERage:DELay:CONStant:AUTO ON    # EX:101
Q AVERage:DELay:CONStant:AUTO?      # EX:102
Q AVERage:DELay:CONStant?           # EX:105 (no write before it)
W AVERage:DELay:RANDom 2000 NS      # EX:109
Q AVERage:DELay:RANDom?             # EX:110
W TRAN:GAP 5 NS                 # EX:115
Q TRAN:GAP?                     # EX:116
W TRAN:DAMP:GAP 30 NS           # EX:120
Q TRAN:DAMP:GAP?                # EX:121
Q DATA:LENG?                    # EX:225
Q DATA:PORT?                    # EX:229, parsed with int()
(open TCP connection to <ip>:<DATA:PORT? reply>, recv size 16412)   # EX:244, CF:26-35
W STAR AUTO                     # EX:247
(wait 5 s, polling every 0.1 s; read data)   # EX:239,248-250
(stop reader, close data socket, join thread)   # EX:252-253, CF:26,50
W STOP                          # EX:254
(close SCPI session)            # EX:261-262
```

Optional steps, off by default (EX:212-222). If switched on they run after the parameter block and before `Q DATA:LENG?`:

```
W SOURce:GAIN:TGC:LINear 10, 0.22        # EX:127 with offset 10, slope 0.22 (EX:215)
Q SOURce:GAIN:TGC:LINear?                # EX:128
W SOURce:GAIN:TGC:MODE LINear            # EX:137
Q SOURce:GAIN:TGC:MODE?                  # EX:138
```

```
W SOURce:GAIN:TGC:ARBitrary 0,5,2,20,5,20,10,40,30,10   # EX:147 with points from EX:220-221
Q SOURce:GAIN:TGC:ARBitrary?                            # EX:149
W SOURce:GAIN:TGC:MODE ARBitrary                        # EX:153
Q SOURce:GAIN:TGC:MODE?                                 # EX:154
```

The arbitrary string equals the doc example at SC:779.

Reply constraints the vendor script imposes (a fake must satisfy these):

* Every reply is one line ended by CRLF. pyvisa strips the terminator.
* `FREQ?`, `TRAN:FREQ?` and `DATA:PORT?` replies must be plain integer text. The script calls `int()` (EX:17,27,230). A reply such as `1.0E8` would raise.
* `SYSTem:ERRor?` must return text whose part before the first comma is an integer (CF:17-21). The loop at EX:190-193 ends only when that integer is 0.
* If the linear TGC step is enabled, the `SOURce:GAIN:TGC:LINear?` reply must be two comma-separated floats equal to the two sent values (EX:130-134). The `SOURce:GAIN:TGC:MODE?` reply must equal exactly `LINear` or `ARBitrary` (EX:139,155).
* All other replies are only logged, so any single line is accepted. The formats seen in the doc are in the command table.
* The data port must already accept a connection when `STAR AUTO` is written, and each packet must be exactly `28 + 2 * 8192 = 16412` bytes for the script's `recv` size to fit (EX:232-233).

Safety note on the example values: the script sets `TRAN:PULS 100 V` and `TRAN:ENAB ON` (EX:31,36), the maximum amplitude and an enabled pulser. Do not copy them as plug defaults.

## REST and WebSocket API (reference only)

Not used by the plug in v1 unless the project decides otherwise. Kept so that a fake or a later driver can reuse it.

### Envelope

* Base URLs: `http://<ip>:<rest_port>/api/v1/` for runtime parameters and `http://<ip>:<rest_port>/config/` for configuration objects (RA:7-8). Default `rest_port` is 80 (RA:5). CONFLICT 9 about 8080 in examples.
* Methods: `GET` reads. `POST` sets, and `PUT` is an alias of `POST`. `OPTIONS` is for CORS preflight (RA:69-76).
* Set request body for `/api/v1/*`: `{"value": "<string|number>"}` (RA:22-29). The vendor code sends numbers and strings (RX:172,677,710). `/config/*` takes object payloads (RA:29).
* Success reply, `200 OK` in examples (RA:12-20,31-39,217-225):

```json
{"status": "success", "data": {"<parameter>": "<value>"}}
```

* The examples show values as JSON strings, for example `"sampling_freq": "100"` (RA:222). The vendor code reads `data[<parameter>]` (RX:113,190).
* Error reply (RA:41-53). Examples show HTTP `400 Bad Request`:

```json
{"status": "error", "message": "<text>", "details": {"code": -2, "field": "<parameter>", "expected": "<range or list>", "received": "<value>"}}
```

* Some error examples have no `details`: unknown parameter (`{"status":"error","message":"parameter not found"}`, RA:341-346) and missing `Content-Length` (`"missing Content-Length header"`, RA:359-365). CONFLICT 16.
* CORS headers sent: `Access-Control-Allow-Origin: *`, `Access-Control-Allow-Methods: GET, POST, PUT, OPTIONS`, `Access-Control-Allow-Headers: Content-Type` (RA:521-524).
* No authentication (RA:532). "Each REST client gets isolated SCPI state and buffers" (RA:528). What that isolates (parameters or only parser state) is UNKNOWN.

### Error codes (RA:59-67)

| Code | Name | Meaning |
|---|---|---|
| 0 | `REST_ERR_SUCCESS` | Operation successful |
| -1 | `REST_ERR_INVALID_VALUE` | Value not in the allowed list |
| -2 | `REST_ERR_OUT_OF_RANGE` | Numeric value outside the valid range |
| -3 | `REST_ERR_MISSING_FIELD` | Required field missing, for example no `value` in the JSON body |
| -4 | `REST_ERR_NOT_FOUND` | Parameter does not exist |
| -5 | `REST_ERR_READ_ONLY` | Attempt to modify a read-only parameter |
| -6 | `REST_ERR_PROTOCOL_FAILED` | Internal protocol error |

### Parameters

The SCPI equivalent column is inferred from names and units. The vendor states no mapping, except that `sampling_freq` refers the reader to the SCPI frequency list (RA:141). "Access" is as stated by the doc; blank means the doc does not say.

| Parameter | Stated values / units | Access | SCPI equivalent (inferred) | Source |
|---|---|---|---|---|
| `system.hostname` | Device hostname | read-only | UNKNOWN | RA:124 |
| `system.version` | System version | read-only | UNKNOWN | RA:125 |
| `device_type` | Device type identifier | read-only | UNKNOWN | RA:126 |
| `serial_number` | Serial number | read-only | `*IDN?` field 3 | RA:127; SC:17 |
| `protocol_version` | Protocol version | read-only | UNKNOWN (`SYSTem:VERSion?` is the SCPI version; relation unknown) | RA:128 |
| `firmware_version` | Firmware version | read-only | `*IDN?` field 4 | RA:129; SC:17 |
| `ip_address` | Ethernet IP address | | none (`/config/dev.eth`) | RA:132 |
| `ap_address` | Wi-Fi AP IP address | | none (`/config/dev.wlan`) | RA:133 |
| `ap_type` | `2G` or `5G` | | none | RA:134,233-236 |
| `ssid_2g`, `pass_2g`, `ssid_5g`, `pass_5g` | SSID and password of each AP band | | none | RA:135-138 |
| `sampling_freq` | MHz. CONFLICT 8 | | `FREQuency` (reply in Hz) | RA:141,63 |
| `ascan_length` | Samples | | `DATA:LENGth` | RA:142 |
| `accumulations` | "0-8 (power of 2)". CONFLICT 10 | | `SENSe:AVERage:COUNt` | RA:143; RX:689-690 |
| `current_mode` | Master or Slave | | `MODE` | RA:144 |
| `ping_pong` | "set to 0 to disable" | | UNKNOWN | RA:145 |
| `tvg_mode` | `OFF`, `LINEAR`, `ARBITRARY`. CONFLICT 7 | | `GAIN:TGC:MODE` | RA:62,148 |
| `tvg_bypass` | dB, 0 to 80 (RA:264). Scope CONFLICT 15 | | `GAIN[:LEVel]` | RA:149,264; RX:1018 |
| `tvg_linear` | "Linear TVG parameters". JSON value format UNKNOWN | | `GAIN:TGC:LINear` | RA:150 |
| `tvg_arbitrary` | "Arbitrary TVG curve data". JSON value format UNKNOWN | | `GAIN:TGC:ARBitrary` | RA:151 |
| `triggering_mode` | `INTERNAL`, `CTP`, `TTL`, `SENSOR`. CONFLICT 5 | | `TRIGgering:MODe` | RA:154; RX:1021 |
| `triggering_interval` | Microseconds | | `TRIGgering:INTerval` (reply in seconds) | RA:155; RX:732-734 |
| `trigger_delay` | "in samples". CONFLICT 3 | | `TRIGgering:DELay` | RA:156 |
| `constant_delay` | "Constant delay value for averaging". Unit UNKNOWN | | `AVERage:DELay:CONStant` | RA:157 |
| `random_delay` | "Random delay range for averaging". Unit UNKNOWN | | `AVERage:DELay:RANDom` | RA:158 |
| `zonder_frequency` | kHz | | `TRANsmitter:FREQuency` (reply in Hz) | RA:161; RX:746-748 |
| `zonder_periods` | 0.5 to 8. CONFLICT 2 | | `TRANsmitter:DURation` | RA:162; RX:751-753,1026 |
| `zonder_amplitude` | V | | `TRANsmitter:PULSe` | RA:163 |
| `zonder_enable` | `ON` or `OFF` | | `TRANsmitter:ENABle` | RA:164 |
| `zonder_reverse_polarity` | `ON` or `OFF` | | `TRANsmitter:REVerse` | RA:165 |
| `zonder_mode` | `COMBINED` or `SPLIT` | | UNKNOWN (the preamp paths have their own REST names below) | RA:166 |
| `zonder_damp_enable` | `ON` or `OFF` | | `TRANsmitter:DAMP[:ENABle]` | RA:167 |
| `high_pass_filter` | "High-pass filter setting". Index or kHz UNKNOWN | | `FILTer:HPASs:INDex` | RA:168 |
| `split_preamp` | `ON` or `OFF`, +20 dB | | `GAIN:PREamp:SPLIT` | RA:169 |
| `combined_preamp` | `ON` or `OFF`, +20 dB | | `GAIN:PREamp:COMBined` | RA:170 |
| `impedance` | `HIGH`, `200`, `1000` | | `TRANsmitter:IMPedance` | RA:171 |
| `clean_memory` | write-only | write-only | `MEMory:CLEar` | RA:173 |
| `start_auto_ascan` | write-only. The value to send is not documented; the vendor code sends 1 (and 0 to stop, CONFLICT 11). | write-only | `STARt AUTO` | RA:174; RX:364,370 |
| `stop_auto_ascan` | write-only. The vendor code sends 1. | write-only | `STOP` | RA:175; RX:809 |

SCPI commands with no REST parameter in the doc: `TRANsmitter:TYPE`, `TRANsmitter:GAP`, `TRANsmitter:DAMP:GAP`, `AVERage:DELay:CONStant:AUTO`, `DATA:PORT?`, the error queue commands and all `*` commands.

The REST doc says some parameters may be read-only and points to a firmware field `haveSetter` (RA:555) that is not part of this repo. Which parameters are writable beyond the ones marked here is UNKNOWN.

### Config endpoints

| Method and path | Purpose | Body and reply | Source |
|---|---|---|---|
| `GET /config/dev.eth` | Read Ethernet configuration | Reply `data`: `{"ip_address": "192.168.200.18"}` | RA:377,384-389,411-428 |
| `POST /config/dev.eth` | Update Ethernet configuration | Body `{"ip_address": "..."}`. Reply echoes `data`. | RA:378,430-450 |
| `GET /config/dev.wlan` | Read Wi-Fi AP configuration | Reply `data`: `ap_address`, `ap_type`, `ap_value` with `2G` and `5G` objects each holding `ssid` and `pass` | RA:379,391-407,452-480 |
| `POST /config/dev.wlan` | Update Wi-Fi AP configuration | Same object as body. Reply echoes `data`. | RA:380,482-513 |
| `GET /config/user-data` | Read an opaque user payload | Reply `application/octet-stream`. If nothing is stored: `200 OK` with an empty body. | RA:80-86 |
| `POST /config/user-data` | Replace the payload | Body `application/octet-stream`, at most 262144 bytes (256 KiB). An empty body is rejected. Reply `{"status": "success"}` with no `data`. | RA:83-86,98-117 |

`/config` errors use the same envelope and code set (RA:515-517). The vendor example re-posts the values it just read, to keep the demo safe (RX:229-265).

### WebSocket

* URL `ws://<ip>:80` with no path. Subprotocol `server-websocket`. Connect timeout 5 s in the vendor client (WS:83,132-136).
* The device sends binary messages that carry the packet stream described above. Control messages from the client are not documented. Whether the server accepts text frames is UNKNOWN.
* Acquisition is started and stopped over REST (`start_auto_ascan` and `stop_auto_ascan`), not over the WebSocket (RX:362-364,801-809).
* The client needs `ascan_length` read from the device first (`GET /api/v1/ascan_length`), because packet size depends on it (WS:74-76; RX:326,334).
* Packet parsing: see "Resynchronisation algorithm". The `timestamp` in the parsed packet is the host receive time from `time.time()` (WS:312), not a device time.
* Python libraries in the vendor requirements: `requests>=2.25.0` and `websocket-client>=1.0.0` (RQ-R). Python 3.6 or higher (RR:159).

## Conflicts between vendor documents

Each item shows both sides. No item is resolved here. The plug and the fake must follow the rule given in the last sentence of an item where there is one, until hardware checks settle it.

1. **Data port value.** `SC` example for `DATA:PORT?` replies `5025` (SC:166-170). The vendor script has `data_port = 2758` as its initial value (EX:180) and `RR` says "raw TCP socket (port 2758)" (RR:91). Port 5025 is also the SCPI command port (EX:179). The script then overwrites its value with the `DATA:PORT?` reply (EX:229-230). Rule: always query `DATA:PORT?`; never hard-code.
2. **Burst periods maximum.** `SC`: `TRAN:DUR` is 0.5 to 16, MAXimum 16 (SC:353,355). `RA` and `RX`: `zonder_periods` is "from 0.5 to 8" (RA:162; RX:1026). The code comment at EX:40 also asks whether the unit is half-periods.
3. **Trigger delay unit.** `SC`: `TRIG:DEL` is in nanoseconds, 0 to 2147483647 NS, default 15000 NS (SC:463,472,477,482). `RA`: `trigger_delay` is "Trigger delay in samples before acquisition" (RA:156).
4. **`TRIG:DEL?` reply unit.** `SC`: the reply is in nanoseconds, and the example shows `15000` for `15 US` (SC:482,486-488). `EX` logs the reply as seconds: `Trigger delay: {trigger_delay} s` (EX:98). The other time queries do reply in seconds (`TRIG:INT?`, `AVERage:DELay:CONStant?`, `AVERage:DELay:RANDom?`: SC:451,850,911) and `EX` labels those correctly (EX:83,106,111).
5. **Trigger source name.** `SC`: `INTernal`, `CTP`, `ENCoder`, `TTL` (SC:413). `RA` and `RX`: `INTERNAL`, `CTP`, `TTL`, `SENSOR` (RA:154; RX:1021). Whether `ENCoder` and `SENSOR` are the same input is UNKNOWN.
6. **`TRAN:DAMP?` after ON.** `SC` documents the reply as `0` or `1` (SC:574) and then shows `TRAN:DAMP ON` followed by `TRAN:DAMP?` replying `0` (SC:578-580). Either a doc typo or the setter did not apply. `EX` sets `TRAN:DAMP:ENAB ON` and only logs the reply (EX:51-53).
7. **`tvg_mode` expected values.** `RA` lists `OFF`, `LINEAR`, `ARBITRARY` (RA:62,148; RX:1017). The error example for an invalid value shows `"expected": "ON, BYPASS"` (RA:295). The SCPI side is `OFF`, `LINear`, `ARBitrary` (SC:702).
8. **`sampling_freq` unit.** `RA` and `RX` say MHz (RA:141; RX:892,1012) and the vendor sets `100` (RA:204; RX:677). The error table example says "Setting sampling_freq to 500000000 when max is 100000000" (RA:63), and the missing-field example uses `12000000` (RA:306-315), which look like Hz. The SCPI query replies in Hz (SC:116).
9. **REST port.** Default 80 (RA:5,543; RX:56; RR:37). The `/api/v1/` curl examples use `:8080` (RA:182,205,230,245,276,307,335,356). The `/config/` examples use `:80` (RA:413). Rule: make the port a setting.
10. **`accumulations` versus `AVER:COUN`.** `RA`: `accumulations` is "0-8 (power of 2)" (RA:143) and the vendor code comment says "4 (2^4 = 16 accumulations)" (RX:689-690). `SC`: `AVER:COUN` is "Acquisitions per averaged vector", range 0 to 8, example 5 (SC:806,815,822). No document says the two are the same parameter. If they are, the count is either direct or an exponent. UNKNOWN.
11. **Stopping acquisition over REST.** `RA` lists `stop_auto_ascan` (RA:175). `RX` main flow, the quick reference and `RR` stop with `stop_auto_ascan` = 1 (RX:809,1086; RR:124). The three WebSocket examples stop with `start_auto_ascan` = 0 (RX:370,471,538). The effect of `start_auto_ascan` = 0 is UNKNOWN. The value to send to the write-only parameters is not documented.
12. **Order of data connection and start.** Connect first: `SC` (SC:158-159), `EX` (EX:244-247), and the WebSocket examples (RX:362-364,529-531). Start first, then connect: `RR` typical workflow (RR:102-112) and the quick reference (RX:1072-1081). Rule: connect first.
13. **Header description.** The class docstring diagram has "CTP coordinate (12 bytes), X,Y,Z coordinates", "Length fields (3 bytes)", "Telemetry (3 bytes)", "Flags (4 bytes)", "Reserved (2 bytes)" and sums to 29 bytes (WS:57-72). The table and struct format sum to 28 and call the 12 bytes a "CTP timing array" (WS:251-266,279; RX:994). The table and struct also agree with `EX:166`.
14. **Reply form of enumerations and booleans.** Doc formats and doc examples differ, so the reply may be the echo of what was sent, a short form, or a long form. UNKNOWN. Cases:
    * `MODE`: format `MASTer` or `SLAVe` (SC:190), example sends `MASTER`, reply `MASTER` (SC:194-196).
    * `TRIG:MODE`: format `INTernal` (SC:420), example sends `INT`, reply `INT` (SC:424-426), vendor sends `INTERNAL` (EX:76).
    * `GAIN:TGC:MODE`: format `LINear` and `ARBitrary` (SC:709). The vendor script asserts the exact strings `LINear` and `ARBitrary` (EX:139,155). `RA` uses `LINEAR` for the REST parameter (RA:148).
    * Boolean replies: `TRAN:ENAB?` `OFF` or `ON` (SC:218), `TRAN:REV?` `OFF`, `ON`, `0`, `1` (SC:272), `TRAN:DAMP?` and the two preamp commands `0` or `1` (SC:574,656,680). The driver must accept `ON`, `OFF`, `1`, `0` in any case.
15. **Scope of `tvg_bypass`.** `RA`: "Bypass gain in dB (when TVG is OFF)" (RA:149). `RX` quick reference: "base level for all modes" (RX:1018). `SC` says the constant gain is the base of the linear and arbitrary modes too (SC:735,767).
16. **Unknown parameter error.** The error table gives `REST_ERR_NOT_FOUND` code -4 (RA:65). The example shows HTTP 400 with only `"message": "parameter not found"` and no `details` or code (RA:341-346). The troubleshooting list says 404 for a bad path (RA:536-539). Which status the device returns for which case is UNKNOWN.
17. **Arbitrary TGC point order.** The syntax text says the format is `time1, gain1, time2, gain2, ..., gainN, timeN` (SC:765). The last pair is reversed in that line. The parameter description (SC:762-764), the worked example (SC:769-772) and the vendor script (EX:144) all use time first, then gain.
18. **Minor doc defects** (no effect on the protocol, listed so nobody trips on them):
    * `HPASs:INDex` example sends the query without `?` (SC:947).
    * `SC` text contains the unresolved template placeholders `<%INSTR%>` and `%INSTR%` (SC:806,923). Read as "the instrument" (inferred).
    * Several commands list `DEFault - <value>` in their text but not in their `<char>` set (for example SC:211-212,239-240,266-267,568-569). Whether `DEFault` is accepted there is UNKNOWN.
    * The REST doc curl comment at RA:201 says 12 MHz while the value is 100. The error example at RA:240-245 is described as a sampling frequency but uses `tvg_bypass`.
    * `SC` error-queue example deliberately mistypes `SYSTem:ERRrr?` to show -113 (SC:63).

## Unknowns to verify on hardware

Each item is an experiment on a real unit. Record the exact bytes of every reply, including terminators. Read `SYSTem:ERRor?` until it returns 0 after each step. Run each experiment on a fresh power-up unless noted.

1. [ ] **Actual `DATA:PORT?` value (CONFLICT 1).** Send `DATA:PORT?` on a fresh connection and record the reply. Open TCP connections to that port, to 2758 and to 5025, and note which accept. Repeat after a reboot and after changing `ip_address` over `/config/dev.eth`, to see whether it stays stable.
2. [ ] **Terminators.** Send `*IDN?` followed by `\r\n`, then `\n`, then `\r`. Observe whether a reply comes and whether it ends with `\r\n` or `\n`. The vendor sets only `read_termination = '\r\n'` (EX:186).
3. [ ] **`*RST` defaults.** Query every settable command in the command table, send `*RST`, query again, and compare with the `DEFault` column. Check whether `*RST` stops a running acquisition, closes the data socket, or clears the error queue. Check also the power-on values before any `*RST`.
4. [ ] **`AVER:COUN` semantics (CONFLICT 10).** Set `AVER:COUN` to 0, 1, 2, 3, 4, 5 and 8 in turn with a stable input. Start `STAR AUTO` and record header `ascan_count` and the packet rate for each. Test whether the effective number of averages is N or 2^N. Compare noise on a steady signal. Send 9 and read the error queue. Repeat the same through REST `accumulations`.
5. [ ] **Header fields.**
    * `length_lo` and `length_hi`: capture packets with `DATA:LENG` 1024, 8192 and 36864. Compare `(length_hi << 16) | length_lo` with the length and with `2 * length + 28`.
    * `telemetry_a`, `telemetry_b`, `telemetry_c`: capture while varying `GAIN`, `TRAN:PULS`, `TRAN:ENAB`, `TRAN:IMP` and unit temperature (warm-up). Log which bytes change.
    * `ctp`: capture in `TRIG:MODE INT` and in `TRIG:MODE CTP` with a known external signal on the CTP input. Check whether the three uint32 values are counters, timestamps or positions, and in which units.
    * `buffer_fill` and `is_full`: stop reading the data socket for several seconds while streaming, then read. Log both fields.
    * `packet_number`: capture more than 600 packets. Confirm the wrap at 255 to 0. Check whether the counter advances per packet or per averaged acquisition.
    * `ascan_count`: see item 4.
    * `reserved_b` and `reserved_c`: check whether they are always 0.
6. [ ] **Single-shot acquisition.** `SC` documents only `STAR AUTO`. Send `STAR` with no argument, then candidates `STAR SING` and `STAR 1` (guesses, not documented). Read the error queue after each. Also run `STAR AUTO`, capture one packet, send `STOP`, and count how many more packets arrive. With `TRIG:MODE TTL` and one external pulse, see if exactly one packet arrives.
7. [ ] **Parameter changes during acquisition.** While `STAR AUTO` runs, send `GAIN 20`, `TRAN:PULS 50 V`, `FREQ 50 MHZ` and `DATA:LENG 2048`. For each, query the value, read the error queue, and watch the stream: does the packet size change mid-stream, how many old-format packets follow, does the stream stop or close, is the change refused with an error?
8. [ ] **Per-connection or global parameters.** Open two SCPI connections A and B. Set `GAIN 33` on A and query `GAIN?` on B. Provoke an error on A (`FOO`) and read `SYSTem:ERRor?` on B. Repeat with a set via REST and a query via SCPI, and the reverse.
9. [ ] **Count-to-volt scaling, ADC depth and full scale.** Feed a known sine wave (known frequency and amplitude) into the input with `TRAN:ENAB OFF`, `GAIN 0`, preamps off, and one impedance setting. Step the amplitude and record counts versus volts, and fit the slope. Short the input and record the baseline mean and noise. Overdrive the input and record the clipping count. Count distinct code values on a slow ramp to estimate bit depth. Repeat for `GAIN` 0 to 80 in steps, for the preamp paths and for each `TRAN:IMP` value. Repeat with TGC `LINear` to learn whether the gain is applied before the ADC.
10. [ ] **Concurrency of SCPI, REST and WebSocket.** Start with SCPI `STAR AUTO`. Then connect the WebSocket and the raw data port at the same time: do both receive identical packets? Do REST `GET` and `POST` calls work while streaming, and how long do they take? Start with REST and stop with SCPI, and the reverse. Check that a REST `POST` is visible in an open SCPI session and the reverse.
11. [ ] **Multiple connections.** Open 2, 3, 4 and more connections to port 5025 and note which are accepted, refused or closed, and whether earlier ones stay alive. Repeat with several connections to the data port. Leave an SCPI connection idle for 1 min, 10 min and 1 h, then query, to find any idle timeout.
12. [ ] **Error queue depth.** Send 100 invalid commands (for example `FOO`) without reading. Read `SYSTem:ERRor:COUNt?`, then drain with `SYSTem:ERRor?` and count entries. Look for an overflow entry. Check that `*CLS` empties the queue. Record the full set of error numbers and texts seen, including the reply to a bad query (SC:63 shows none).
13. [ ] **Settling times.** Stream a stable echo or an injected test signal. Step `TRAN:PULS` from 20 to 100 V and back, `GAIN` from 0 to 80 dB and back, and toggle `TRAN:ENAB`, `TRAN:IMP` and the TGC mode. Record the peak amplitude per packet against time since the command. Report the time until the amplitude stays within a chosen tolerance. State the tolerance used.
14. [ ] **Echo case of `GAIN:TGC:MODE?` (CONFLICT 14).** Send `GAIN:TGC:MODE LINear`, `LINEAR`, `linear`, `LIN` and `lin`, each followed by `GAIN:TGC:MODE?`. Record the exact reply bytes. Repeat for `ARBitrary` and `OFF`, and for `MODE`, `TRIG:MODE`, `TRAN:TYPE` and `TRAN:IMP`. Note whether the reply is an echo, a short form or a fixed long form.
15. [ ] **Boolean reply form (CONFLICT 6).** For each ON and OFF command, send `ON`, `OFF`, `1` and `0` and record the reply. Include `TRAN:DAMP:ENAB ON` then `TRAN:DAMP:ENAB?`.
16. [ ] **Units of bare numbers.** Send `FREQ 100`, `TRAN:FREQ 2500`, `TRIG:INT 100000`, `TRIG:DEL 0` and `TRAN:PULS 100` with no unit. Query each and see which unit the device assumed.
17. [ ] **Reply number formats.** Record the exact text of every query reply (`100.0E-3`, `50.0E-6`, `20.0, 0.1`, integers) after values that need rounding. Check that `FREQ?`, `TRAN:FREQ?` and `DATA:PORT?` are always plain integers, as the vendor script needs (EX:17,27,230).
18. [ ] **Invalid and out-of-range input.** Send `FREQ 3 MHZ`, `DATA:LENG 100`, `DATA:LENG 5000`, `TRAN:PULS 4`, `GAIN 81`, `TRAN:DUR 9`, `TRAN:DUR 16`, `TRIG:INT 50 US`. For each: error number and text, and whether the old value was kept, clamped or replaced.
19. [ ] **UP, DOWN and keyword forms.** Send `FREQ UP` from 100 MHz and from 50 MHz, `DATA:LENG UP`, `GAIN MAX`, `TRAN:PULS DEF`. Find the `FREQ` step. Check whether `DEFault` works on the enumeration commands (SC:211-212).
20. [ ] **Time zero and trigger delay unit (CONFLICT 3).** With a fixed reflector, send `TRIG:DEL 1000 NS` and `TRIG:DEL 0 NS`. Measure the echo shift in samples at 100 MHz. 100 samples means nanoseconds. Check also what sample 0 corresponds to (trigger, pulse, or end of delay).
21. [ ] **Data socket lifetime.** After `STOP`, check whether the device closes the data socket, whether it stays usable for the next `STAR AUTO`, and whether packets already in flight still arrive. Check whether `MEM:CLEar` before or after connecting changes stale packets at the start. Check whether a second data connection is accepted and what it receives.
22. [ ] **TGC limits.** Send arbitrary curves of 1, 5, 16, 64 and 256 points, unsorted times, negative gains and gains above 80 dB. Record accepted point counts, errors and the reply text (spaces, float formatting). Check the effective time range.
23. [ ] **Preamp combination.** Set `GAIN:PREamp:COMBined ON` and `GAIN:PREamp:SPLIT ON` alone and together. Measure the gain change with a known input to see if it is +20 dB or +40 dB, and whether the commands are mutually exclusive.
24. [ ] **Averaging delay with AUTO.** With `AVERage:DELay:CONStant:AUTO ON`, send `AVERage:DELay:CONStant 50 US` and confirm error -221 appears. Read the auto-computed value for several `AVER:COUN` and `FREQ` settings. Find the `AUTO` default.
25. [ ] **Multiple commands per line.** Send `GAIN 10;GAIN?` and `GAIN 10; GAIN?` and note the reply or error.
26. [ ] **Discovery and Wi-Fi.** On a unit with default network settings, read `/config/dev.eth` and `/config/dev.wlan`. Record real defaults (IP, DHCP or static, netmask, gateway, AP address, SSIDs). Scan the LAN for mDNS and other announcements. Check whether ports 5025, the data port and 80 answer on the AP address. State how the unit was reset to defaults.
27. [ ] **Trigger source names (CONFLICT 5).** Send `TRIG:MODE ENCoder`, then read it back over SCPI and over REST. Send `triggering_mode` `SENSOR` over REST and read it back over SCPI.
28. [ ] **Burst period unit and range (CONFLICT 2).** Send `TRAN:DUR 8`, `8.5`, `9`, `16` and read back. Check with a scope whether `TRAN:DUR 1` is one period or one half-period.
29. [ ] **`SYSTem:VERSion?` value.** Record the reply.
30. [ ] **REST write-only parameters.** Send `start_auto_ascan` with values 1, 0, `"AUTO"` and `stop_auto_ascan` with 1 and 0 (CONFLICT 11). Check which start and stop the stream.

## Licence of the vendor material

* `REST_API_Python/README.md` ends with a section "License" saying "MIT - Free to use and modify for your integration needs." (RR:170-172).
* `REST_API_Python/ascan_websocket.py` states "License: MIT" and "Author: A1580 Integration Examples" in its module docstring (WS:34-35).
* No other file states a licence: not `README.md`, `SCPI_COMMANDS.md`, `REST_API.md`, nor the files in `SCPI_Python/`. A search of the repo for "licen", "copyright" and "MIT" finds only the two places above.
* There is no `LICENSE`, `COPYING` or similar file in the repo (the tracked files at the source commit are `.gitignore`, the three top-level docs, `REST_API_Python/` with four files, and `SCPI_Python/` with five files, two of them under `.vscode/`). No copyright holder or year is named, and the standard MIT text is not included.
* So the MIT grant is stated for the REST Python examples only. For the SCPI documents, the SCPI Python example and the REST document the licence is UNKNOWN.
* This file paraphrases the vendor material and quotes short command strings, field names and doc sentences for interoperability. It does not copy vendor code. Do not copy vendor source files into the plug without settling the licence. This is a note for the project, not legal advice.
