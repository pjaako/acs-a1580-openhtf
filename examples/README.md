# Examples

`example_test.py` is a minimal OpenHTF test: one phase, `acquire_ascan`, with two measurements.
It applies the vendor example's settings with `apply_setup`, acquires one A-scan with `acquire(1)`
and records `num_points` (samples in the A-scan) and `peak_counts` (largest absolute raw count).

    .venv/bin/python examples/example_test.py --fake                       # no hardware
    .venv/bin/python examples/example_test.py --host 192.168.200.18        # real A1580
    A1580_HOST=192.168.200.18 .venv/bin/python examples/example_test.py    # same, from the environment

`--host` wins over `A1580_HOST`; with neither, the config key `a1580_host` applies (default `192.168.200.18`,
the address in the vendor examples). `--fake` swaps in the plug on `FakeA1580Resource`. The exit code is 0
when the test passes and 1 otherwise.

Safety: the settings are the vendor example's, except the pulser, which is set to 20 V (the vendor uses 100 V)
and switched on. When the test ends, `tearDown()` restores the settings the plug found at the start, writing
`TRAN:ENAB OFF` first, so the pulser ends in the state it was in before the test (off, if it was off).
Nothing has been run against a real A1580 yet.

Results: OpenHTF prints the outcome table to the console and the phase prints
`num_points=... peak_counts=...`. Nothing is written to disk; `examples/results/` is git-ignored for later use.

An operator-GUI station demo (as in rigol-dho-openhtf) is not written yet.
