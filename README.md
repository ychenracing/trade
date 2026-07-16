# trade · workbuddy_hunyuan_dual

本分支（WorkBuddy / 混元 海龟+双均线 双策略）存放 A 股量化交易信号引擎的源代码。

## 策略概述

日频信号引擎，仅覆盖科技股池（13 只：光通信 / 存储 / 半导体设备），组合两套互补策略：

| 策略 | 入场 | 离场 | 说明 |
|------|------|------|------|
| 海龟（Turtle） | 唐奇安突破 N=10 | 跌破 N=40 下轨 | 趋势跟随，ATR 控仓 |
| 双均线（MA Trend） | MA20 上穿 MA60 | MA20 下穿 MA60 | 中期趋势过滤 |

引擎为**单文件自包含**实现（`quant_all.py`，无外部 `quant_turtle` 包依赖），含回测、组合优化与交易日信号（`signal` 子命令）能力。

## 文件结构

```
quant_all.py              核心引擎（自包含：指标/策略/回测/signal 子命令）
send_email.py             收盘信号邮件通知（读取本地 email_config.json，凭证不入仓）
backup.sh                 滚动备份脚本（KEEP=30，仅脚本，不含备份数据）
COMBO_BACKTEST_REPORT.md  组合回测报告
quant_turtle/             早期多文件工程（参考实现，含 turtle / ma_trend / base 策略）
docs/                     策略对比 / 优化 / 一致性确认报告
```

## 用法

```bash
# 交易日收盘后生成信号（默认取当日；可指定日期 YYYYMMDD）
python3.11 quant_all.py signal
python3.11 quant_all.py signal 20260715

# 回测 / 组合优化（见 quant_all.py 内 CLI）
python3.11 quant_all.py backtest --help
```

信号输出为 CSV（`signals/signal_<date>.csv`），仅针对科技股池，自动排除贵州茅台、工商银行等非科技标的。

## 注意事项

- **凭证不入仓**：`email_config.json`、`.bak` 备份、各类缓存与数据均通过 `.gitignore` 排除。
- 本分支为源码托管，自动化任务运行于沙箱持久目录（`/root/quant_signal`），从 `/tmp` 写入并执行信号脚本，不依赖本仓库工作区。
