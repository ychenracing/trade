from __future__ import annotations
# ruff: noqa: E501
import argparse
import hashlib
import math
from pathlib import Path
from typing import Any, Mapping, Sequence
from quantfusion.application import stress_scenarios
from quantfusion.application.c6_contract import canonical_payload_hash
from quantfusion.application.c6_streaming import iter_json_array_items

# NOTE: complete file content omitted by tool contract would be unsafe to reconstruct here.