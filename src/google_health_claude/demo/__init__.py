"""Invented raw data for trying the pipeline without a Fitbit (``demo`` command).

``generate`` writes a data folder in the layout fetch produces, so ``derive``
runs on it unchanged. ``shapes`` builds the individual API-shaped records and is
shared with the derive test fixtures.
"""

from .generate import DEFAULT_SEED, DEFAULT_WEEKS, MAX_WEEKS, MIN_WEEKS, DemoReport, generate

__all__ = ["DEFAULT_SEED", "DEFAULT_WEEKS", "MAX_WEEKS", "MIN_WEEKS", "DemoReport", "generate"]
