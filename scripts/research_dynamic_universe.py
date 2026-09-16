"""Run the research-only Dynamic Candidate Universe study."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from quantfusion.research.dynamic_universe import run_dynamic_universe_research
from scripts.compare_universes import validate_market_data_directory


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--start-date", default="2023-01-01")
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--source-revision", default="")
    return parser


def main() -> int:
    args = build_argument_parser().parse_args()
    data_dir = Path(args.data_dir).expanduser()
    validate_market_data_directory(
        data_dir,
        ("pool_g",),
        end_date=args.end_date,
    )
    manifest_path = data_dir / "manifest.json"
    result = run_dynamic_universe_research(
        data_dir,
        start_date=args.start_date,
        end_date=args.end_date,
    )
    result["source_revision"] = args.source_revision or os.environ.get("GITHUB_SHA")
    result["inputs"] = {
        "market_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "requested_start_date": args.start_date,
        "requested_end_date": args.end_date,
    }
    output = Path(args.output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(
        "dynamic-universe",
        f"watch={len(result['watch_universe'])}",
        f"promotions={len(result['promotions'])}",
        f"verdict={result['verdict']['status']}",
    )
    for name, metrics in result["windows"].items():
        print(
            name,
            f"promotions={metrics['promotion_count']}",
            f"complete60={metrics['complete_60d_count']}",
            f"median20={metrics['median_excess_20d']}",
            f"median60={metrics['median_excess_60d']}",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
