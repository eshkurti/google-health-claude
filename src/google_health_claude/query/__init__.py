"""Typed, read-only access to derive's CSV output.

Depends on nothing in the package but derive/schema.py (tests/test_architecture.py
checks this), so it works on a copy of the derived folder alone.
"""

from .health import Freshness, HealthData, Night, Run

__all__ = ["Freshness", "HealthData", "Night", "Run"]
