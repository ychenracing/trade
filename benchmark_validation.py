"""用于策略归因的可复现简单基准。"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd

import quant_fusion as qf
import regime_adaptive as ra


_SYMBOL_RE = re.compile(r"^\d{6}$")


def _validate_period(start: str, end: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    """校验起止日期并返回规范化时间戳。"""
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    if start_ts is pd.NaT or end_ts is pd.NaT:
        raise ValueError("start and end must be valid dates")
    if start_ts > end_ts:
        raise ValueError("start must not be later than end")
    return start_ts, end_ts


def _validate_symbols(symbols: dict[str, str]) -> dict[str, str]:
    """校验非空、无重复的六位股票代码映射。"""
    if not isinstance(symbols, dict) or not symbols:
        raise ValueError("symbols must be a non-empty mapping")
    normalized = {str(code): str(name) for code, name in symbols.items()}
    if any(_SYMBOL_RE.fullmatch(code) is None for code in normalized):
        raise ValueError("every symbol must be a six-digit stock code")
    return normalized


def _buy_hold_return(frame: pd.DataFrame, start: str, end: str) -> float:
    """计算首个可交易开盘买入、期末收盘估值的持有收益。"""
    start_ts, end_ts = _validate_period(start, end)
    sample = frame.loc[(frame.index >= start_ts) & (frame.index <= end_ts)]
    if len(sample) < 2:
        raise ValueError("insufficient buy-and-hold observations")
    initial_open = float(sample["open"].iloc[0])
    final_close = float(sample["close"].iloc[-1])
    if (
        not math.isfinite(initial_open)
        or not math.isfinite(final_close)
        or initial_open <= 0
        or final_close <= 0
    ):
        raise ValueError("buy-and-hold prices must be finite and positive")
    return final_close / initial_open - 1.0


def run_benchmarks(
    symbols: dict[str, str],
    *,
    data_dir: str,
    regime_data_dir: str,
    start: str,
    end: str,
) -> dict[str, Any]:
    """运行等权买入持有、因果 Top-3 和自适应策略三个基准。"""
    normalized_symbols = _validate_symbols(symbols)
    start_ts, _ = _validate_period(start, end)
    returns = {
        code: _buy_hold_return(
            qf.DataFetcher.load_stock_data(
                code,
                start,
                end,
                data_dir=data_dir,
            ),
            start,
            end,
        )
        for code in normalized_symbols
    }
    equal_weight = sum(returns.values()) / len(returns)
    boundary = str((start_ts - pd.Timedelta(days=1)).date())
    leaders = ra.select_positive_momentum_leaders(
        tuple(normalized_symbols),
        data_dir=data_dir,
        as_of=boundary,
    )
    top3 = (
        sum(returns[code] for code in leaders.selected_symbols)
        / len(leaders.selected_symbols)
        if leaders.selected_symbols
        else 0.0
    )
    adaptive = ra.RegimeAdaptiveBacktestEngine().run(
        normalized_symbols,
        start,
        end,
        data_dir=data_dir,
        regime_data_dir=regime_data_dir,
        leader_data_dir=data_dir,
        indicator_state="warm",
    )
    return {
        "period": {"start": start, "end": end},
        "symbols": sorted(normalized_symbols),
        "equal_weight_buy_hold_return": equal_weight,
        "causal_top3_buy_hold_return": top3,
        "adaptive_total_return": float(adaptive["total_return"]),
        "adaptive_max_drawdown": float(adaptive["max_drawdown"]),
        "adaptive_total_trades": int(adaptive["total_trades"]),
        "top3_symbols": list(leaders.selected_symbols),
    }


def _atomic_json(payload: dict[str, Any], path: Path) -> None:
    """原子写入严格 JSON 基准结果。"""
    content = (
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=".benchmark_",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def build_argument_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器，便于测试默认值和错误处理。"""
    parser = argparse.ArgumentParser(
        description="比较策略与简单买入持有基准",
    )
    parser.add_argument("--symbols", required=True, help="逗号分隔的六位股票代码")
    parser.add_argument("--data-dir", required=True, help="股票冻结行情目录")
    parser.add_argument(
        "--regime-data-dir",
        required=True,
        help="固定指数证据目录",
    )
    parser.add_argument("--start", required=True, help="开始日期 YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="结束日期 YYYY-MM-DD")
    parser.add_argument(
        "--output",
        default="benchmark_validation.json",
        help="输出 JSON 路径",
    )
    return parser


def main() -> int:
    """解析命令行、运行基准并写入结果文件。"""
    args = build_argument_parser().parse_args()
    codes = [item.strip() for item in args.symbols.split(",") if item.strip()]
    if len(codes) != len(set(codes)):
        raise ValueError("symbols must not contain duplicates")
    payload = run_benchmarks(
        {code: code for code in codes},
        data_dir=args.data_dir,
        regime_data_dir=args.regime_data_dir,
        start=args.start,
        end=args.end,
    )
    _atomic_json(payload, Path(args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
