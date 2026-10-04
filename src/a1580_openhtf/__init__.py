"""OpenHTF plug for the ACS A1580 ultrasonic pulser-receiver."""

# TODO(plug agent): once plug.py and stream.py exist, re-export from this package:
#   from .plug import A1580Plug, AScan
#   from .stream import AScanHeader
# and add 'A1580Plug', 'AScan', 'AScanHeader' to __all__. Importing the package must
# not require pyvisa (lazy-import it inside plug.py).

__version__ = '0.1.0'
