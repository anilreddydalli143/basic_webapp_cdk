"""Reusable building blocks (CDK "constructs") for the web application.

Each module in this package owns one slice of the architecture and exposes a
small, documented interface. The top-level stack (``basic_webapp/webapp_stack.py``)
just wires them together. Splitting things this way means:

  * each file stays short enough to read in one sitting,
  * a slice can be unit-tested or reused in another stack on its own,
  * and the "who is allowed to talk to whom" wiring is explicit and visible.
"""

from .compute import WebAppCompute
from .database import Database
from .network import Network
from .storage import StaticAssets

__all__ = ["Network", "StaticAssets", "WebAppCompute", "Database"]
