# 冻结行情数据

本目录集中保存可复现回测所需的两组只读行情证据。`market/` 是 2025—2026 年生产趋势回归基线，`regime/` 是外层路由和 2022—2024 年弱市验证证据。运行产生的缓存不得写入这里。

仓库命令只把 `data/market` 和 `data/regime` 作为默认路径。显式传入的数据目录始终按普通路径处理，不会按目录名称映射到其他位置；具体命令可以执行常规的 `expanduser()` / `resolve()` 规范化。

## 目录内容

- `market/`：22 只生产股票及寒武纪（`688256`）专项验证样本的前复权日线、`manifest.json` 与 `SHA256SUMS`；
- `regime/`：16 只验证股票的历史前复权日线、两个非交易指数、`MANIFEST.json` 与 `SHA256SUMS`；
- `regime/000300.csv` 和 `regime/000682.csv` 只提供路由证据，不会进入股票交易池。

历史前缀来自已审计的东方财富和腾讯验证快照。两组数据重叠的 2024 年以后区间以 `market/` 为生产基准；拼接差异记录在 `regime/MANIFEST.json`。成交量统一换算为股，包括 `688256` 的来源特定比例处理。

## 完整性检查

```bash
cd data/market && sha256sum -c SHA256SUMS
cd ../regime && sha256sum -c SHA256SUMS
```

`python -m scripts.run_regime_validation` 还会在 `artifacts/validation/regime_validation_results.json` 中独立记录 SHA-256、首尾日期和行数。

## 方法限制

- `regime/` 没有 2021 年股票历史，因此 2022 年开始时无法形成完整的 240 交易日动量证据。路由按设计持有现金，不会在观察结果后缩短窗口。
- 晚上市股票保留真实的较短历史。没有足够历史的股票会被排除，但不会导致整个股票池失效。
- 公共前复权历史可能在后续公司行动后被数据源重述。刷新任一目录等同于创建新的研究快照，必须重新生成清单、精确基线和验证工件，不能静默覆盖当前证据。
