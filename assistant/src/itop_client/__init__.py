"""
Async Python library for iTop REST API.
Forked from itoptop (github.com/jonatasrenan/itoptop) and rewritten with httpx.

Vendored external library: keep it self-contained and generic — no imports
from this application, and do not remove functionality that this service
happens not to use.
"""

from .exceptions import ItopError as ItopError
from .itop import Itop as Itop
