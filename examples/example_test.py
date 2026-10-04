# Adapted from rigol-dho-openhtf example_test.py (one phase, FakePlug, --fake switch).
"""Minimal OpenHTF test using the A1580 plug.

Usage:
    python examples/example_test.py                      # real instrument (A1580_HOST or --host)
    python examples/example_test.py --fake               # no hardware
    python examples/example_test.py --host 192.168.200.18

The settings are the vendor example's, except the pulser: 20 V instead of the vendor's 100 V,
until the owner says otherwise.
"""

import argparse
import os
import sys

import openhtf as htf
from openhtf.util import configuration

from a1580_openhtf import A1580Plug

EXAMPLE_SETUP = {
    'FREQ': '100 MHZ',
    'DATA:LENG': 8192,
    'TRAN:FREQ': '2500 KHz',
    'TRAN:PULS': '20 V',  # the vendor example uses 100 V
    'TRAN:ENAB': 'ON',
    'TRAN:DUR': 1,
    'TRAN:REVerse': 'OFF',
    'TRAN:DAMP:ENAB': 'ON',
    'TRAN:TYPE': 'SINGle',
    'GAIN': 10,
    'GAIN:TGC:MODE': 'OFF',
    'AVER:COUN': 0,
    'TRIG:MODE': 'INTERNAL',
    'TRIG:INT': '100000 US',
    'TRAN:IMP': 200,
    'MODE': 'MASTer',
    'TRIG:DEL': '0 NS',
    'AVERage:DELay:CONStant:AUTO': 'ON',
    'AVERage:DELay:RANDom': '2000 NS',
    'TRAN:GAP': '5 NS',
    'TRAN:DAMP:GAP': '30 NS',
}


@htf.measures(htf.Measurement('num_points'), htf.Measurement('peak_counts'))
@htf.plug(pr=A1580Plug)
def acquire_ascan(test, pr):
    """Apply the setup, acquire one A-scan, record its length and peak count."""
    pr.apply_setup(EXAMPLE_SETUP)
    (ascan,) = pr.acquire(1)
    test.measurements.num_points = len(ascan.raw)
    test.measurements.peak_counts = int(abs(ascan.raw.astype(int)).max())
    print(f'num_points={test.measurements.num_points} peak_counts={test.measurements.peak_counts}')


class FakePlug(A1580Plug):
    def __init__(self):
        from a1580_openhtf.fake_resource import FakeA1580Resource

        super().__init__(resource=FakeA1580Resource())


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--fake', action='store_true', help='use the fake instrument')
    parser.add_argument('--host', help='instrument address (default: $A1580_HOST or the config)')
    args = parser.parse_args()
    host = args.host or os.environ.get('A1580_HOST')
    if host:
        # Declared by importing a1580_openhtf.plug above, so the value is not lost.
        configuration.CONF.load(a1580_host=host, _override=True)
    phase = acquire_ascan.with_plugs(pr=FakePlug) if args.fake else acquire_ascan
    passed = htf.Test(phase).execute(test_start=lambda: 'example_dut')
    sys.exit(0 if passed else 1)


if __name__ == '__main__':
    main()
