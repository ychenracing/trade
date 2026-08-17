"""Compatibility API and CLI for :mod:`scripts.benchmark_validation`."""

from scripts.benchmark_validation import *  # noqa: F403
from scripts.benchmark_validation import main as _main


if __name__ == "__main__":
    raise SystemExit(_main())
