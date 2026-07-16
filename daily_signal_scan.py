#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日收盘信号扫描：对持仓 + 关注标的，用最新收盘数据跑三策略信号。
输出每个标的当天最新信号：买入(BUY) / 卖出(SELL) / 持有(HOLD)。

健壮性设计（应对 /workspace 被清空的自动任务环境）：
  - 量化策略代码优先从持久区 /root/.codebuddy/quant/ 加载；若该目录缺失则回退 /workspace。
  - 结果双写：持久区 /root/.codebuddy/quant/daily_signal_scan_result.md（永不丢失）
              + /workspace/daily_signal_scan_result.md（用户可见）。
  - 标的清单硬编码在下方 CODES，不依赖任何外部 csv 文件。
  - 不自动发邮件；按用户偏好由定时任务或手动执行触发。
"""
import os
import sys
os.environ.setdefault("REQUESTS_CA_BUNDLE", "/etc/ssl/certs/ca-certificates.crt")
os.environ.setdefault("PYTHONHTTPSVERIFY", "0")

import importlib.util
import pandas as pd

# 持久区（跨休眠保留）优先，/workspace 仅作回退与展示
QUANT_DIR = "/root/.codebuddy/quant"
CODE_PATH = os.path.join(QUANT_DIR, "aquant_v10_9symbols_improved.py")
if not os.path.exists(CODE_PATH):
    CODE_PATH = "/workspace/aquant_v10_9symbols_improved.py"

# 结果双写路径
OUT_PERSIST = os.path.join(QUANT_DIR, "daily_signal_scan_result.md")
OUT_VISIBLE = "/workspace/daily_signal_scan_result.md"

# 标的：当前持仓 + 关注（光通信default池 + 半导体semi池）
CODES = {
    # 持仓（default 池）
    "300308": "中际旭创", "300502": "新易盛", "300394": "天孚通信", "688008": "澜起科技",
    "603986": "兆易创新", "002409": "雅克科技", "688300": "联瑞新材", "300054": "鼎龙股份",
    "688535": "华海诚科", "688205": "德科立", "920045": "蘅东光", "300776": "帝尔激光",
    "688072": "拓荆科技",
    # 关注（semiconductor 池）
    "688249": "晶合集成", "688347": "华虹宏力", "300666": "江丰电子", "600206": "有研新材",
    "688409": "富创精密", "688361": "中科飞测", "300604": "长川科技", "688120": "华海清科",
    "688082": "盛美上海",
}


def main():
    spec = importlib.util.spec_from_file_location("aquant_daily", CODE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    asof = pd.Timestamp.today().normalize()
    # 实际取数会返回最近完整交易日；这里用 today 作为上界
    end = asof.strftime("%Y-%m-%d")

    rows = []
    for code, name in CODES.items():
        try:
            df = mod.DataFetcher.fetch_stock_data(code, "2024-01-01", end)
            df = df[df.index <= asof]
            if len(df) < 60:
                rows.append((name, code, "-", "数据不足", "-", "-", "HOLD"))
                continue
            cls = mod.BacktestEngine.classify_symbol(code, df=df, name=name)
            cls_disp = "semi" if cls == "semiconductor" else "default"
            cfg = mod.BacktestEngine.semiconductor_config() if cls == "semiconductor" else mod.BacktestEngine._default_config()
            ind = mod.Indicators.compute_all(df, cfg)
            i = len(df) - 1
            last_date = df.index[-1]
            ctx = mod.BarContext(i=i, df=df, current_assets=1_000_000,
                                 indicators=ind, available_cash=1_000_000,
                                 symbol=code, date=last_date.strftime("%Y-%m-%d"))
            sigs = []
            for S in [mod.TurtleBreakoutStrategy, mod.DualMAStrategy, mod.ATRChannelStrategy]:
                inst = S(cfg)
                s = inst.on_bar(ctx)
                sigs.append(s.direction if s else "-")
            close = df["close"].iloc[-1]
            if "buy" in sigs:
                comp = "🔵 BUY"
            elif "sell" in sigs:
                comp = "🔴 SELL"
            else:
                comp = "HOLD"
            rows.append((name, code, f"{close:.2f}", cls_disp, sigs[0], sigs[1], comp, sigs[2]))
        except Exception as e:
            rows.append((name, code, "-", f"ERR:{e}", "-", "-", "HOLD", "-"))

    # 构建 Markdown 报告
    buy_list = [r for r in rows if r[6] == "BUY"]
    sell_list = [r for r in rows if r[6] == "SELL"]

    def _cell(v):
        # 单策略方向列：buy→🔵 / sell→🔴 / 其他→—
        if v == "buy":
            return "🔵"
        if v == "sell":
            return "🔴"
        return "—"

    def _row_md(r):
        name, code, close, cls = r[0], r[1], r[2], r[3]
        if len(r) == 7:
            s0, s1, comp, s2 = r[4], r[5], r[6], "-"
        else:
            s0, s1, comp, s2 = r[4], r[5], r[6], r[7]
        mark = "★ " if ("BUY" in comp or "SELL" in comp) else ""
        comp_cell = comp  # 综合列已含 🔵 BUY / 🔴 SELL / HOLD
        return f"| {mark}{name} | {code} | {close} | {cls} | {_cell(s0)} | {_cell(s1)} | {_cell(s2)} | {comp_cell} |"

    today = pd.Timestamp.today().strftime("%Y-%m-%d")
    md = []
    md.append("# 每日收盘量化信号")
    md.append("")
    md.append(f"> 基准日（最近完整收盘）：**{today}**  ")
    md.append("> 量化策略：AQuant v10 三策略组合（海龟突破 + 双均线 + ATR通道），A股 T+1 执行，三层风控。  ")
    md.append("> 参数：default 池（光通信/紧凑趋势，entry=8/exit=3/atr_mult=1.0/risk_pct=0.03）与 "
              "semiconductor 池（半导体/宽波动慢趋势，entry=33/exit=28/atr_mult=2.0/risk_pct=0.015）。")
    md.append("")
    md.append("## 信号总览")
    md.append("")
    md.append("| 标的 | 代码 | 收盘 | 分类 | 海龟 | 双均线 | ATR通道 | 综合 |")
    md.append("|------|------|------|------|------|--------|---------|------|")
    for r in rows:
        md.append(_row_md(r))
    md.append("")
    md.append("## 信号汇总")
    md.append("")
    md.append(f"- **买入信号（{len(buy_list)}）**：" + (", ".join(f"{r[0]}({r[1]})" for r in buy_list) or "无"))
    md.append(f"- **卖出信号（{len(sell_list)}）**：" + (", ".join(f"{r[0]}({r[1]})" for r in sell_list) or "无"))
    md.append("")
    md.append("> ★ 标记行为触发买入/卖出信号的标的。🔵 BUY / 🔴 SELL / — 表示无信号。")
    report = "\n".join(md)

    # 控制台直接展示 Markdown
    print(report)

    # 结果双写：持久区（永不丢失）+ /workspace（用户可见）。二者任一失败都不影响另一个。
    for out_path in (OUT_PERSIST, OUT_VISIBLE):
        try:
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(report + "\n")
            print("\n[结果已写入 %s]" % out_path)
        except Exception as e:
            print("\n[结果写文件失败 %s: %s]" % (out_path, e))


if __name__ == "__main__":
    main()
