"""Compatibility alias for :mod:`quantfusion.data.contracts`."""

from importlib import import_module
import sys


sys.modules[__name__] = import_module("quantfusion.data.contracts")
