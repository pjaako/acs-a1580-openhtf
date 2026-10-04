# Capture files

A capture file is a YAML description of the test conditions for the A1580 pulser-receiver:
sampling, pulser, receiver, trigger and averaging. Every group and field is optional; only
what is written is sent. Quantities carry units (`voltage: 20 V`, `frequency: 2.5 MHz`).

Validate and apply:

    .venv/bin/python -m a1580_openhtf.capture check captures/*.yaml
    plug.apply_capture('captures/vendor_example.yaml')   # via a1580_openhtf.capture.apply_capture

Keys, choices, units and ranges are defined by the dataclasses in `src/a1580_openhtf/capture.py`;
`capture.schema.json` is generated from them (`python -m a1580_openhtf.capture schema --write`)
and gives editors completion. Raw SCPI goes under `extra:`. `*RST` defaults are unknown, so
nothing resets the instrument unless asked.
