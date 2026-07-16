"""回测运行脚本：对中际旭创（300308）2025-01 至 2026-06 运行多策略海龟回测。

用法：
    python3.11 run_backtest.py                       # 默认：中际旭创 全区间
    python3.11 run_backtest.py --symbol 300308 --start 20250101 --end 20260630
"""
import argparse
import json
import os
import sys

import pandas as pd
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from quant_turtle.config import Config                     # noqa: E402
from quant_turtle.data_feed import load_daily              # noqa: E402
from quant_turtle.backtest import run_backtest, compute_metrics  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="海龟法则 A 股版多策略回测")
    parser.add_argument("--symbol", default="300308", help="A 股代码，默认中际旭创")
    parser.add_argument("--start", default="20250101")
    parser.add_argument("--end", default="20260630")
    parser.add_argument("--adjust", default="qfq")
    args = parser.parse_args()

    cfg = Config(
        universe=[args.symbol],
        start_date=args.start,
        end_date=args.end,
        adjust=args.adjust,
    )
    cfg.validate()

    print(f"[数据] 加载 {args.symbol} {args.start}~{args.end} ...")
    df = load_daily(args.symbol, args.start, args.end, adjust=args.adjust)
    if df.empty:
        print("无数据，退出。")
        return
    print(f"[数据] 共 {len(df)} 根日线，区间 {df['date'].min().date()} ~ {df['date'].max().date()}")

    result = run_backtest(df, cfg, symbol=args.symbol)
    metrics = compute_metrics(result, cfg)

    # 控制台输出
    print("\n========== 回测结果 ==========")
    print(f"标的            : {args.symbol}")
    print(f"初始资金        : {metrics['initial_capital']:,.0f} 元")
    print(f"期末权益        : {metrics['final_equity']:,.0f} 元")
    print(f"总收益率        : {metrics['total_return']*100:+.2f}%")
    print(f"年化收益率      : {metrics['annual_return']*100:+.2f}%")
    print(f"最大回撤        : {metrics['max_drawdown']*100:.2f}%")
    print(f"交易天数        : {metrics['n_bars']}")
    print(f"买入成交笔数    : {metrics['n_buy_orders']}")
    print(f"卖出成交笔数    : {metrics['n_sell_orders']}")
    print(f"触发熔断清仓    : {metrics['n_halt_liquidations']} 笔（halted={metrics['halted']}）")

    # 落盘
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
    os.makedirs(out_dir, exist_ok=True)
    result["equity_curve"].to_csv(os.path.join(out_dir, f"equity_{args.symbol}.csv"))
    fills_df = pd.DataFrame(
        [(f.date, f.symbol, f.strategy, f.side, f.shares, round(f.price, 3), f.reason) for f in result["fills"]],
        columns=["date", "symbol", "strategy", "side", "shares", "price", "reason"],
    )
    fills_df.to_csv(os.path.join(out_dir, f"fills_{args.symbol}.csv"), index=False)
    with open(os.path.join(out_dir, f"metrics_{args.symbol}.json"), "w", encoding="utf-8") as fh:
        json.dump(metrics, fh, ensure_ascii=False, indent=2)
    print(f"\n[完成] 结果已保存至 {out_dir}")


if __name__ == "__main__":
    main()
