from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected one match, got {count}: {old!r}")
    p.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "README.md",
    "| `summary.buys_suppressed` | 结合 `risk_state_identity_mismatch`、`current_route_mismatch`、`warmup_not_ready` 判断本次为什么不允许新增买入。 |",
    "| `summary.buys_suppressed` | 结合 `risk_state_identity_mismatch`、`current_route_mismatch` 与 `warmup_health.warmup_status` 判断本次为什么不允许新增买入。 |",
)
replace_once(
    "README.md",
    "| `warmup_health.warmup_status` | `NOT_READY` 抑制全部新增买入；`DEGRADED` 本身仅提示，不覆盖其他限制，也不证明数据完全适用。 |",
    "| `warmup_health.warmup_status` | `INVALID` 抑制全部新增买入；`DEGRADED` 本身仅提示，不覆盖其他限制，也不证明数据完全适用；`READY` 表示该健康检查通过。 |",
)
replace_once(
    "README.md",
    "但模拟日扫**另外消费** `warmup_health`：`NOT_READY` 抑制全部新增买入，`DEGRADED` 本身只提示。因此不能笼统称治理输出“完全不进入决策路径”。",
    "但模拟日扫**另外消费** `warmup_health`：`INVALID` 抑制全部新增买入，`DEGRADED` 本身只提示，`READY` 表示该健康检查通过。因此不能笼统称治理输出“完全不进入决策路径”。",
)
replace_once(
    "docs/ARCHITECTURE.md",
    "治理计算读取既有状态形成健康、共识和风险意见，不直接改写交易账本；独立 `risk_opinion` 也不直接产生交易。模拟日扫另外消费 `warmup_health`，当 `warmup_status=NOT_READY` 时抑制全部新增买入，`DEGRADED` 本身只提示。因而“治理输出完全不影响任何决策”不是准确描述。风险事件后续收益和校准窗口属于事后分析，不回流为当时的因果输入。",
    "治理计算读取既有状态形成健康、共识和风险意见，不直接改写交易账本；独立 `risk_opinion` 也不直接产生交易。模拟日扫另外消费 `warmup_health`，当 `warmup_status=INVALID` 时抑制全部新增买入，`DEGRADED` 本身只提示，`READY` 表示该健康检查通过。因而“治理输出完全不影响任何决策”不是准确描述。风险事件后续收益和校准窗口属于事后分析，不回流为当时的因果输入。",
)
replace_once(
    "docs/ARCHITECTURE.md",
    "模拟回测、模拟日扫、优化器和压力验证复用规范引擎或 `ProductionReplayEngine`。研究层只产生候选配置和评价请求，不复制信号、费用、涨跌停、成交量容量、T+1 或资金核算。生产回放的路由变化保留同一个模拟账户中的现金、持仓、挂单、袖套、峰值、风险锁和冷却，不在边界重新建账。",
    "模拟回测、模拟日扫、优化器和压力验证复用规范引擎或 `ProductionReplayEngine`。研究层只产生候选配置和评价请求，不复制信号、费用、涨跌停、成交量容量、T+1 或资金核算。生产回放只接收通用 `ReplayRuntimePolicy` 能力，不识别 C6 intervention 身份；冻结 C6 请求的校验、身份到通用能力的映射以及非 canonical 诊断入口归 `quantfusion/research/c6_runtime.py` 所有。生产回放的路由变化保留同一个模拟账户中的现金、持仓、挂单、袖套、峰值、风险锁和冷却，不在边界重新建账。",
)
replace_once(
    "docs/VALIDATION.md",
    "治理模块读取状态形成预热健康、独立风险意见、袖套共识和覆盖置信度，不直接修改交易账本；应用层另行消费预热状态，`NOT_READY` 抑制新增买入，`DEGRADED` 本身只提示。风险事件的 1／3／5／10／20 日结果和机会成本是事后分析，禁止回填为当时输入。",
    "治理模块读取状态形成预热健康、独立风险意见、袖套共识和覆盖置信度，不直接修改交易账本；应用层另行消费预热状态，`INVALID` 抑制新增买入，`DEGRADED` 本身只提示，`READY` 表示该健康检查通过。风险事件的 1／3／5／10／20 日结果和机会成本是事后分析，禁止回填为当时输入。",
)
