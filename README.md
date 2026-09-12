# Quant Fusion

Quant Fusion 是面向 A 股 AI 硬件、光通信和半导体产业链的日线量化研究与人工决策支持系统。它不是自动交易或券商下单系统；核心使用方式是收盘后计算、下一可交易日执行。

## 快速开始

```bash
python -m scripts.fetch_daily_data
python -m scripts.run_daily_scan
```

日扫默认读取本地历史行情和风险数据，并输出候选、持仓建议、风险意见与治理证据。需要正式回放或诊断时，使用对应的 `quantfusion.application` 入口。

## 当前架构

系统以 `quantfusion/` 为唯一规范实现，日线输入经因果数据层进入融合策略、组合治理、账户风险与执行队列；所有策略信号只使用当日收盘及以前的信息，成交在下一可交易日发生。

- **组合融合**：趋势、反转和 ATR 通道等信号通过统一融合与组合配置生成目标权重。
- **账户连续性**：现金、持仓、HWM、账户锁、挂单和风险状态跨交易日连续保存；风险判断不得从局部袖套或单日重置推导。
- **执行因果性**：收盘后决策进入既有订单队列，下一可交易日按成交量、涨跌停和现金约束执行。
- **风险治理**：组合风险、市场状态、行业冲击、账户锁和可选账户风险预算共同约束新风险暴露。

## 决策支持输出

- **独立风险意见（P0-3）**：`RiskOpinion` 对象回答"当前风险环境如何"（等级、置信度、市场状态、健康牛市沉默、禁新开仓/冻结加仓语义、建议总敞口上限、最弱簇与原因代码），供人工决策或其他系统作为独立风险裁判意见消费，不直接驱动交易。
- **袖套共识证据（P1-1）**：逐日计算三袖套持仓共识（每只股票的平均持有袖套数）、按 3/2/1 袖套计数的分布、各袖套部署率、最弱袖套与连续退化计数。空仓簿按惯例记满共识、最弱袖套置空、退化计数清零。共识连续退化 3 日以上作为风险意见证据代码输出，不新增状态机。
- **风险篮覆盖置信度（P1-2）**：置信度 = 0.45 × 观察成分比例 + 0.35 × 行业覆盖比例 + 0.20 × 持仓子行业映射比例。覆盖不足时置信度下降，风险意见据此降低强度而不是继续输出同样的 L2/L3 确信度；低于 0.60 时标记 `low_basket_coverage`。
- **L1 冻结机会成本（P1-3）**：峰值停留在 L1（冻结加仓但未升级）的警报段记录其后 20 日组合收益中位数，用于度量 L1 冻结是否多数落在牛市正常回踩上。

以上输出随回测结果自动附出：`warmup_health`、`risk_opinion`、`sleeve_agreement`、`risk_governance_series`（逐日）与 `risk_event_calibration`（事件表与指标）。

## AB5 本次发布范围

原 PR63 选中的 `C6-Base+AB5` 在既有连续账户、订单队列和下一可交易日执行链上增加
账户风险预算，不改变账户锁或买卖撮合规则。它须显式使用
`account_risk_budget_enabled=True`；未指定 AB5 的日扫和旧研究命令不会自动启用。
现有公开引擎调用、正式命令、来源绑定及用户明确接受的验收例外，
见 [AB5 发布说明](docs/C6_AB5_RELEASE.md)；完整已封存 Base 的账本审计见
[AB5 账本审计](docs/C6_AB5_RELEASE_AUDIT.md)。

本次正式 958 场景已实际执行。已知 21.106217% 最大回撤、三个财富比较差异，以及随机场景
`date/symbol/side` bucket P90=185，均作为本次 **C6-Base+AB5** 的显式、来源绑定例外保留；
其中 P90 的通用 RELAX15 上限仍为 184，185 不是新的全局阈值。原始 18% 等判据、旧拒绝工件和
原生正式 gate 结果不被改写。这些已接受历史结果也不是未来回撤或收益保证；任何更差结果或
其他未豁免失败仍按原合同失败关闭。

## 默认策略参数

完整默认策略字段如下，具体默认值和校验分别以 `quantfusion.config.engine.default_engine_config()` 与 `validate_engine_config()` 为唯一事实来源：

策略参数：`entry_period`、`exit_period`、`adx_threshold`、`adx_period`、`atr_period`、`rsi_period`、`ma_short`、`ma_long`、`atr_multiplier`、`trail_atr_mult`、`channel_mult`、`channel_lower_mult`、`risk_pct`、`hard_stop`、`strategy_weight`、`max_symbol_weight`、`max_total_weight`、`max_units`、`max_drawdown`、`daily_loss_limit`、`sector_guard_enabled`、`sector_guard_min_symbols`、`sector_shock_return`、`sector_shock_breadth`、`sector_shock_ma`、`sector_shock_window`、`sector_shock_confirmations`、`sector_recovery_ma`、`sector_recovery_breadth`、`sector_recovery_confirmations`、`symbol_level_sell_veto`、`momentum_lookback`、`max_positions`、`group_min_slots`、`fusion_single_scale`、`fusion_double_scale`、`fusion_triple_scale`、`profit_lock_activation`、`profit_lock_giveback`、`reversal_break_giveback`、`reversal_exit_period`、`reversal_loss_cut`、`reversal_turtle_enabled`、`reversal_dual_ma_enabled`、`reversal_atr_channel_enabled`、`combined_group_weight_limits`、`liquidate_on_circuit_breaker`、`strict_unmapped`、`commission_rate`、`stamp_duty`、`slippage`、`min_commission`、`max_pending_buy_days`、`pyramid_add_atr`、`pyramid_risk_decay`、`atr_method`、`limit_price_epsilon`、`per_symbol_limit_pct`、`st_symbols`、`risk_free_rate`、`market_regime_enabled`、`regime_ewi_lookback`、`regime_breadth_ma_long`、`regime_adx_trend`、`regime_adx_choppy`、`regime_hurst_window`、`regime_hurst_trend`、`regime_hurst_choppy`、`regime_vol_lookback`、`regime_vol_extreme_pct`、`regime_ewi_slope_trend`、`regime_ewi_slope_choppy`、`regime_score_trend`、`regime_score_choppy`、`regime_choppy_confirmations`、`regime_trend_confirmations`、`regime_recovery_confirmations`、`regime_min_state_hold`、`regime_transition_scale`、`regime_transition_pyramid_scale`、`regime_transition_trim_confirmations`、`regime_trend_to_transition_confirmations`、`regime_choppy_exit_ratio`、`regime_transition_exit_ratio`、`enable_cm_overlay`、`cm_overlay_shock_trim`、`cm_independent_risk_basket`、`cm_trend_health_protection`、`cm_risk_continuous_confirm_days`、`cm_risk_level2_drawdown`、`cm_risk_level3_drawdown`、`cm_risk_severe_direct_return`、`dynamic_sleeve_weights`、`transition_fast_weight`、`transition_base_weight`、`transition_slow_weight`、`choppy_fast_weight`、`choppy_base_weight`、`choppy_slow_weight`、`adaptive_max_positions`、`transition_max_positions`、`choppy_max_positions`、`sticky_candidates`、`adaptive_sticky_candidates`、`sticky_min_score_gap`、`sticky_confirm_days`、`sticky_cycle_days`、`sticky_rotated_cooldown_days`、`concentrated_account_rearm_days`、`incomplete_reference_max_total_weight`、`established_expansion_min_score`、`subindustry_shrinkage`。

## 组合策略参数

完整组合字段如下，具体默认值以 `quantfusion.config.portfolio.PortfolioPolicy` 数据类为唯一事实来源：

组合参数：`allocation_mode`、`single_lookbacks`、`allocation_horizons`、`drawdown_alert`、`confirmed_drawdown`、`drawdown_confirmations`、`emergency_drawdown`、`adv_lookback`、`max_order_adv_ratio`、`candidate_lookbacks`、`candidate_horizons`、`rearm_trading_days`、`terminal_drawdown`、`concentration_drawdown_adjustment`、`candidate_reference_percentile`、`regime_symbols`、`market_regime_enabled`、`regime_ewi_lookback`、`regime_breadth_ma_long`、`regime_adx_trend`、`regime_adx_choppy`、`regime_hurst_window`、`regime_hurst_trend`、`regime_hurst_choppy`、`regime_vol_lookback`、`regime_vol_extreme_pct`、`regime_ewi_slope_trend`、`regime_ewi_slope_choppy`、`regime_score_trend`、`regime_score_choppy`、`regime_choppy_confirmations`、`regime_trend_confirmations`、`regime_recovery_confirmations`、`regime_min_state_hold`、`regime_transition_scale`、`regime_trend_to_transition_confirmations`、`regime_choppy_exit_ratio`、`regime_transition_exit_ratio`。

趋势路径的周期确认基准为 23%，紧急基准为 27%，终身峰值回撤线为 28%；确认和紧急阈值按 `concentration_drawdown_adjustment / N` 收紧。弱市路径使用独立的 15%/20%/23%/26% 风险梯度。

## 交易与数据契约

- 信号只使用当日收盘及以前的数据，唐奇安通道和反转低点滞后一日。
- A 股最小交易单位为 100 股；模拟佣金率 0.025%，最低佣金 5 元，卖出印花税 0.05%，单边滑点 0.1%。
- 买入受现金、单股权重、总仓位、行业权重、持仓数量和成交量容量共同约束；组合单日最多参与前 20 日平均成交量的 0.5%。
- 本地和在线数据都经过 OHLCV、日期、成交量单位和新鲜度校验；固定指数证据未知时失败关闭。
- `total_trades` / `sell_trades` 是实际 `TradeRecord` 成交记录数；`date_symbol_side_count` / `date_symbol_sell_side_count` 才是唯一日期、股票、方向桶数。桶数只是诊断统计，不是券商订单数。
