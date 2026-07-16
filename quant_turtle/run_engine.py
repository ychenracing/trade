"""自动托管入口（模拟模式）。

以「模拟券商」逐根回放行情，演示多策略自动下单闭环：
  python3.11 run_engine.py                       # 默认中际旭创
  python3.11 run_engine.py --symbol 300308 --start 20250101 --end 20260630

注意：默认 mode=paper，仅内存撮合、不产生任何真实交易。实盘需自行接入券商 API
并将 Config.mode 设为 'live'（当前 LiveBroker 为占位桩，会显式抛错，确保安全）。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from quant_turtle.config import Config                       # noqa: E402
from quant_turtle.data_feed import load_daily                # noqa: E402
from quant_turtle.engine import TradingEngine                # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="海龟法则 A 股版 · 模拟自动托管")
    parser.add_argument("--symbol", default="300308")
    parser.add_argument("--start", default="20250101")
    parser.add_argument("--end", default="20260630")
    parser.add_argument("--adjust", default="qfq")
    args = parser.parse_args()

    cfg = Config(universe=[args.symbol], start_date=args.start, end_date=args.end, adjust=args.adjust)
    cfg.validate()

    print(f"[托管] 模式={cfg.mode}  标的={args.symbol}  区间={args.start}~{args.end}")
    df = load_daily(args.symbol, args.start, args.end, adjust=args.adjust)
    if df.empty:
        print("无数据，退出。")
        return

    engine = TradingEngine(cfg)
    result = engine.run_paper({args.symbol: df})

    eq = result["equity_curve"]["equity"]
    initial = cfg.initial_capital
    total_return = eq.iloc[-1] / initial - 1.0
    peak = eq.cummax()
    max_dd = (eq / peak - 1.0).min()

    print("========== 模拟托管结果 ==========")
    print(f"初始资金   : {initial:,.0f} 元")
    print(f"期末权益   : {eq.iloc[-1]:,.0f} 元")
    print(f"总收益率   : {total_return*100:+.2f}%")
    print(f"最大回撤   : {max_dd*100:.2f}%")
    print(f"成交笔数   : {len(result['fills'])}")
    print(f"是否熔断   : {result['halted']}")

    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
    os.makedirs(out_dir, exist_ok=True)
    result["equity_curve"].to_csv(os.path.join(out_dir, f"paper_equity_{args.symbol}.csv"))
    print(f"[完成] 模拟托管权益曲线已保存至 {out_dir}")


if __name__ == "__main__":
    main()
