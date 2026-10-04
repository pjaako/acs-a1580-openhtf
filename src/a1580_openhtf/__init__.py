"""OpenHTF plug for the ACS A1580 ultrasonic pulser-receiver."""

from typing import TYPE_CHECKING, Any

from .fake_resource import FakeA1580Resource
from .plug import A1580Plug, AScan
from .stream import AScanHeader

if TYPE_CHECKING:
    from .capture import Capture, CaptureError, load_capture

__version__ = '0.1.0'

__all__ = [
    'A1580Plug',
    'AScan',
    'AScanHeader',
    'Capture',
    'CaptureError',
    'FakeA1580Resource',
    '__version__',
    'load_capture',
]

# The capture names are resolved on first use (PEP 562). Importing `.capture` here would
# make `python -m a1580_openhtf.capture` warn that the module is already in sys.modules.
_CAPTURE_NAMES = frozenset({'Capture', 'CaptureError', 'load_capture'})


def __getattr__(name: str) -> Any:
    if name in _CAPTURE_NAMES:
        from . import capture

        return getattr(capture, name)
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
