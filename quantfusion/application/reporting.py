"""Human-readable and persisted backtest reporting."""

from __future__ import annotations

import math
from pathlib import Path

import pandas as pd


class PerformanceReport:
    """Render and persist deterministic backtest results."""

    @staticmethod
    def print_report(result: dict, symbols_dict: dict[str, str]) -> None:
        """Print the standard human-readable performance summary."""
        if "error" in result:
            print(f"Backtest failed: {result['error']}")
            return
        print(f"\n{'═' * 60}")
        print("  Quant Fusion performance report")
        print(f"{'═' * 60}")
        print(f"  Symbols: {', '.join((f'{v}({k})' for k, v in symbols_dict.items()))}")
        print(f"  Initial capital:   {result['initial_capital']:>15,.0f}")
        print(f"  Final assets:       {result['final_assets']:>15,.0f}")
        print("  ────────────────────────────────")
        print(f"  Total return:   {result['total_return']:>15.2%}")
        print(f"  Annualized return: {result['annual_return']:>15.2%}")
        print(f"  Maximum drawdown:   {result['max_drawdown']:>15.2%}")
        print(f"  Sharpe ratio:   {result['sharpe']:>15.2f}")
        print(f"  Calmar ratio:   {result['calmar']:>15.2f}")
        print(f"  Win rate:       {result['win_rate']:>15.2%}")
        pf = float(result["profit_factor"])
        pf_str = (
            "N/A (no losing trades)" if math.isinf(pf) else f"{pf:.2f}"
        )
        print(f"  Profit factor:     {pf_str:>15}")
        print(f"  Open positions:   {result.get('open_positions', 0):>15d}")
        print(f"  Trade records: {result['total_trades']:>15d}")
        print(f"  Sell records:  {result['sell_trades']:>15d}")
        print(
            "  Date/symbol/side buckets:"
            f"{result.get('date_symbol_side_count', 0):>10d}"
        )
        print(f"  Average exit giveback:{result.get('avg_exit_from_peak', 0.0):>15.2%}")
        print(f"  Worst exit giveback:{result.get('worst_exit_from_peak', 0.0):>15.2%}")
        print(f"{'═' * 60}\n")
        pending = result.get("pending_signals", [])
        if pending:
            print(
                "  Signals pending for the next trading day (subject to a tradable opening price):"
            )
            for signal in pending:
                print(
                    f"  {signal.signal_date} {symbols_dict.get(signal.symbol, signal.symbol)}({signal.symbol}) {signal.strategy_name} {signal.direction.upper()} {signal.target_shares} shares | {signal.fusion_label} | {signal.reason}"
                )
        trades = result.get("trades", [])
        if trades:
            print("  Trade details (latest 20):")
            print(
                f"  {'Date':<12} {'Symbol':<8} {'Strategy':<20} {'Side':<6} {'Shares':>8} {'Price':>10} {'PnL':>12} {'Reason'}"
            )
            print(f"  {'─' * 100}")
            for t in trades[-20:]:
                pnl_str = f"{t.pnl:>+10,.0f}" if t.direction == "sell" else ""
                print(
                    f"  {t.date:<12} {t.symbol:<8} {t.strategy_name:<20} {t.direction:<6} {t.shares:>8} {t.price:>10.2f} {pnl_str}   {t.reason}"
                )

    @staticmethod
    def save_result(result: dict, output_dir: str) -> None:
        """Persist audited tabular and JSON result artifacts."""
        if "error" in result:
            raise ValueError(f"Cannot save a failed backtest result: {result['error']}")
        out = Path(output_dir).expanduser()
        out.mkdir(parents=True, exist_ok=True)
        result["equity_curve"].to_csv(out / "equity_curve.csv", encoding="utf-8-sig")
        result["drawdown_series"].rename("drawdown").to_csv(
            out / "drawdown.csv", encoding="utf-8-sig"
        )
        pd.DataFrame([vars(t) for t in result.get("trades", [])]).to_csv(
            out / "trades.csv", index=False, encoding="utf-8-sig"
        )
        pd.DataFrame([vars(s) for s in result.get("pending_signals", [])]).to_csv(
            out / "latest_signals.csv", index=False, encoding="utf-8-sig"
        )
        summary_keys = [
            "initial_capital",
            "final_assets",
            "total_return",
            "annual_return",
            "max_drawdown",
            "sharpe",
            "calmar",
            "win_rate",
            "profit_factor",
            "total_trades",
            "sell_trades",
            "sleeve_fill_count",
            "sleeve_sell_fill_count",
            "date_symbol_side_count",
            "date_symbol_sell_side_count",
            "open_positions",
            "reversal_exit_trades",
            "avg_exit_from_peak",
            "worst_exit_from_peak",
        ]
        pd.DataFrame([{k: result.get(k) for k in summary_keys}]).to_csv(
            out / "summary.csv", index=False, encoding="utf-8-sig"
        )

    @staticmethod
    def plot_equity_curve(result: dict, save_path: str = "equity_curve.png") -> None:
        """Plot portfolio equity and drawdown in a deterministic layout."""
        if "error" in result:
            print(f"Backtest failed; cannot plot: {result['error']}")
            return
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        eq = result["equity_curve"]
        dd = result["drawdown_series"]
        _, axes = plt.subplots(
            2, 1, figsize=(14, 8), gridspec_kw={"height_ratios": [3, 1]}
        )
        axes[0].plot(eq.index, eq["assets"] / 10000, linewidth=1.5, color="#1a73e8")
        axes[0].set_title("Quant Fusion Portfolio Equity Curve", fontsize=14)
        axes[0].set_ylabel("Assets (CNY 10k)")
        axes[0].grid(True, alpha=0.3)
        axes[0].axhline(
            y=result["initial_capital"] / 10000, color="gray", linestyle="--", alpha=0.5
        )
        axes[1].fill_between(dd.index, dd * 100, 0, color="#dc3545", alpha=0.4)
        axes[1].set_title("Drawdown (%)", fontsize=12)
        axes[1].set_ylabel("Drawdown %")
        axes[1].grid(True, alpha=0.3)
        plt.tight_layout()
        plt.savefig(save_path, dpi=150, bbox_inches="tight")
        plt.close()
        print(f"\n  Equity curve saved: {save_path}")
