# Trade Alpha Attribution — 订单效率报告

## 先统一“订单数”口径

当前冻结回放中的 `645` 是内部 strategy/sleeve fills，不应解释为 645 笔券商订单。

更接近可净额化操作的是 date-symbol-side bucket：

- 645 个内部 fills
- 190 个 date-symbol-side buckets
- 404 个 sell fills
- 109 个 sell buckets

即使 190 也不是券商真实订单台账，只是比 sleeve fill 更合理的执行动作代理。未来如果要做“券商订单数”验收，应从真实 order ledger / execution batch 层直接计数，不能继续混用 fill 数。

## 操作结构

### 买入

81 个 buy buckets：

- 新建仓 33
- 加仓 48

买入后的 forward-return 质量总体较高，因此没有证据支持“大量买入本身是低质量噪声”。

### 卖出

109 个 sell buckets：

- 完整退出 33
- 减仓 76

也就是说，卖出动作中约 69.7% 是减仓而不是最终退出。结合 61.47% 的 sell bucket 在 60 日内重新买回，Trade 的高交易活动主要表现为 **仓位反复调节**。

## 哪些动作最值得审查

### 1. AB5 相关仓位调节

生产路径记录了 445 个 `account_budget_buy_reduced` order events。AB5 ON/OFF 的直接反事实显示：

- fills: 289 -> 645
- date-symbol-side buckets: 115 -> 190
- 总收益: +1265.11% -> +861.05%
- 最大回撤: -16.50% -> -15.10%

因此 AB5 不仅是收益损失候选，也是额外交易活动的最大因果候选。

### 2. 减仓后快速重进

109 个 sell bucket 中 40 个在 20 日内重新买回，67 个在 60 日内重新买回。对于长期趋势赢家，这种动作会同时产生：

- 机会成本；
- 滑点/手续费；
- 再入场时点风险；
- 更复杂的账户状态变化。

### 3. 小幅盈亏的卖出

109 个 sell bucket 中有 18 个绝对已实现 PnL 比例低于 2%。这类动作不能自动认定为低价值，因为可能是必要风险减仓；但它们是后续“交易合并”研究的优先审查对象。

## 直接交易成本

当前生产路径：

- commission + stamp duty: 74,192
- estimated slippage from open: 139,432
- total modeled direct cost: 213,625
- 相当于初始资金的约 10.68%

这是现金成本，不等于组合收益直接少 10.68 pct。更重要的证据是：AB5 OFF 的显式费用反而比 ON 高约 1,170，但收益高 404.06 pct；所以当前最大损失不是手续费本身，而是 **被干预后的暴露路径**。

## Order-event 诊断

生产回放还记录了大量订单决策事件：

- `rejected_no_shared_batch_capacity`: 1087
- `rejected_portfolio_symbol_limit`: 598
- `account_budget_buy_reduced`: 445
- `scaled_late_strategy_join`: 138
- `scaled_for_fair_batch_allocation`: 83
- `admitted_cross_sleeve_early_dual_transition`: 47
- `blocked_market_risk_pyramid`: 12
- `blocked_catastrophe_cooldown`: 7

这些是意图处理/缩放/拒绝事件，不是已成交订单。它们说明 Trade 的执行协调层非常活跃，但不能把 2429 个 order events 当成 2429 笔交易。

## 低价值交易的当前定义

本研究建议下一阶段只针对满足以下证据链的动作做“合并/减少”：

1. 同一标的刚减仓/退出，短期内重新进入；
2. 退出后的长期趋势没有被破坏；
3. 动作主要由非灾难性预算/仓位调节触发；
4. 去除或合并该动作的反事实不显著损害回撤、尾部风险或账户连续性。

反之，下列动作不能为了减少数量而机械删除：

- hard risk liquidation；
- 真正避免灾难性下跌的 stop；
- 大赢家的必要利润保护；
- 因流动性、组合上限和因果执行要求产生的合法约束。

因此目标应是 **减少低价值 churn，而不是减少交易数字本身**。
