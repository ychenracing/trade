# Quant Fusion A股科技趋势决策系统

## 项目定位

Quant Fusion 是面向 A 股 AI 硬件、光通信和半导体产业链的日线量化研究与人工决策支持项目。它用于收盘后回测、组合验证、日常信号扫描和真实账户持仓审视，不连接券商、不自动下单，也不承诺未来收益。

生产趋势引擎位于 `quant_fusion.py`，外层因果路由位于 `regime_adaptive.py`。项目提供冻结行情、哈希校验、严格 JSON 工件、参数搜索、账户时点建议和持续集成门禁。

## 最重要的使用边界

1. 只处理日线数据，信号在收盘后形成，最早在后续可交易日开盘执行。
2. 回测结果来自固定历史样本，不代表未来收益。
3. `--account` 只生成真实账户建议，不把持仓注入历史回测。
4. 默认 `strict_unmapped=True`，未映射股票失败关闭。
5. 趋势路由默认要求请求股票全部具有可观察数据；只有研究调用显式设置 `allow_unavailable_symbols=True` 时才允许过滤缺失股票。

## 当前策略结构

趋势生产引擎由唐奇安突破、双均线趋势和 ATR 通道组成，三类策略保留独立子持仓和审计轨迹。默认 `allocation_mode=ensemble`，总资本（如 200 万元）在 fast、base、slow 三个固定虚拟子账户之间分配，三个子账户合计资本不超过总资本，不使用杠杆；卖出优先于买入，同一股票卖出默认否决新增买入。股票池超过 6 只时使用固定参考篮子进行候选过滤，单股不超过 60%，总仓位不超过 100%，最多持有 6 只。

内部状态机使用等权指数斜率、均线广度、ADX、Hurst 和波动率，将市场标记为 `TREND`、`TRANSITION` 或 `CHOPPY`。

外层路由只使用部署边界前可见的数据：

- 沪深 300 `000300` 与科技风险指数 `000682` 都有完整、新鲜证据且 MA20 高于 MA60：使用趋势引擎；
- 两只指数证据完整但趋势未确认：在正 240 日动量股票中，按多因子弱市评分（240 日动量、120 日相对强度、60 日动量、回撤韧性和趋势修复）选出前三名；
- 任一指数缺失、不可解析、历史不足或超过 10 个自然日陈旧：持有现金，不进行个股动量选股；
- 没有正动量股票：持有现金。

弱市策略使用 22% 灾难止损、5 ATR 初始止损、80 个交易日时间止损和盈利后 3 ATR 吊灯止损，同时启用 15% 回撤预警、20% 周期确认、23% 紧急回撤、26% 终身峰值回撤线和 12% 单日损失保护。风险订单在下一可交易开盘执行，跳空或连续跌停仍可能使实际损失超过阈值。

穿越牛熊叠加层（`cross_market_overlay.py`）默认开启，是叠放在 ensemble 之上的 bull-silent 防御层：持仓采用分层保护止损（P0-1），灾变止损（自持仓峰值回撤超过 28%）始终待命；成本止损（低于成本 18%）、ATR 吊灯（6 ATR）和盈利分层保护在早期行业风险预警且账户整体偏离峰值（≥5%）时按各自阈值独立触发，任一已待命保护线被收盘价跌破即全额退出。触发后标的进入 10 个交易日冷却期，期内所有买入路径（三个趋势袖套、弱市、恢复、账户时点候选）都被阻断（P0-4）。结构冲击快速降仓（`cm_overlay_shock_trim`）默认关闭，仅在显式开启时生效。干净牛市（无风险预警、账户贴近峰值）只有灾变地板待命，因此 1、3、5、13 只股票池结果与纯 ensemble 完全一致；更大股票池若出现单票深跌，叠加层会封住该标的的最大损失并释放资金。

子行业参数收缩（`subindustry_shrinkage`，默认 0.5 开启）：细分子行业参数画像（光模块、光器件、存储接口、芯片设计、设备、测试、材料、封装等）在解析时被拉向其粗粒度父画像，只对"允许细分"的参数（最大单票权重、ATR 倍数、风险预算）做小幅收缩，入场/出场周期、盈利保护、加仓与路由参数沿层级共享不收缩，从而降低薄样本过拟合，同时保留已验证的粗粒度趋势结构。

## 默认策略参数

完整默认策略字段如下，具体默认值以 `_CoreBacktestEngine._default_config()` 为唯一事实来源：

策略参数：`entry_period`、`exit_period`、`adx_threshold`、`adx_period`、`atr_period`、`rsi_period`、`ma_short`、`ma_long`、`atr_multiplier`、`trail_atr_mult`、`channel_mult`、`channel_lower_mult`、`risk_pct`、`hard_stop`、`strategy_weight`、`max_symbol_weight`、`max_total_weight`、`max_units`、`max_drawdown`、`daily_loss_limit`、`sector_guard_enabled`、`sector_guard_min_symbols`、`sector_shock_return`、`sector_shock_breadth`、`sector_shock_ma`、`sector_shock_window`、`sector_shock_confirmations`、`sector_recovery_ma`、`sector_recovery_breadth`、`sector_recovery_confirmations`、`symbol_level_sell_veto`、`momentum_lookback`、`max_positions`、`group_min_slots`、`fusion_single_scale`、`fusion_double_scale`、`fusion_triple_scale`、`profit_lock_activation`、`profit_lock_giveback`、`reversal_break_giveback`、`reversal_exit_period`、`reversal_loss_cut`、`reversal_turtle_enabled`、`reversal_dual_ma_enabled`、`reversal_atr_channel_enabled`、`combined_group_weight_limits`、`liquidate_on_circuit_breaker`、`strict_unmapped`、`commission_rate`、`stamp_duty`、`slippage`、`min_commission`、`max_pending_buy_days`、`pyramid_add_atr`、`pyramid_risk_decay`、`atr_method`、`limit_price_epsilon`、`per_symbol_limit_pct`、`st_symbols`、`risk_free_rate`、`market_regime_enabled`、`regime_ewi_lookback`、`regime_breadth_ma_long`、`regime_adx_trend`、`regime_adx_choppy`、`regime_hurst_window`、`regime_hurst_trend`、`regime_hurst_choppy`、`regime_vol_lookback`、`regime_vol_extreme_pct`、`regime_ewi_slope_trend`、`regime_ewi_slope_choppy`、`regime_score_trend`、`regime_score_choppy`、`regime_choppy_confirmations`、`regime_trend_confirmations`、`regime_recovery_confirmations`、`regime_min_state_hold`、`regime_transition_scale`、`regime_trend_to_transition_confirmations`、`regime_choppy_exit_ratio`、`regime_transition_exit_ratio`、`enable_cm_overlay`、`cm_overlay_shock_trim`、`sticky_candidates`、`subindustry_shrinkage`。

## 组合策略参数

完整组合字段如下，具体默认值以 `PortfolioPolicy` 数据类为唯一事实来源：

组合参数：`allocation_mode`、`single_lookbacks`、`allocation_horizons`、`drawdown_alert`、`confirmed_drawdown`、`drawdown_confirmations`、`emergency_drawdown`、`adv_lookback`、`max_order_adv_ratio`、`candidate_lookbacks`、`candidate_horizons`、`rearm_trading_days`、`terminal_drawdown`、`concentration_drawdown_adjustment`、`candidate_reference_percentile`、`regime_symbols`、`market_regime_enabled`、`regime_ewi_lookback`、`regime_breadth_ma_long`、`regime_adx_trend`、`regime_adx_choppy`、`regime_hurst_window`、`regime_hurst_trend`、`regime_hurst_choppy`、`regime_vol_lookback`、`regime_vol_extreme_pct`、`regime_ewi_slope_trend`、`regime_ewi_slope_choppy`、`regime_score_trend`、`regime_score_choppy`、`regime_choppy_confirmations`、`regime_trend_confirmations`、`regime_recovery_confirmations`、`regime_min_state_hold`、`regime_transition_scale`、`regime_trend_to_transition_confirmations`、`regime_choppy_exit_ratio`、`regime_transition_exit_ratio`。

趋势路径的周期确认基准为 23%，紧急基准为 27%，终身峰值回撤线为 28%；确认和紧急阈值按 `concentration_drawdown_adjustment / N` 收紧。弱市路径使用独立的 15%/20%/23%/26% 风险梯度。

## 交易与数据契约

- 信号只使用当日收盘及以前的数据，唐奇安通道和反转低点滞后一日。
- A 股最小交易单位为 100 股；模拟佣金率 0.025%，最低佣金 5 元，卖出印花税 0.05%，单边滑点 0.1%。
- 买入受现金、单股权重、总仓位、行业权重、持仓数量和成交量容量共同约束；组合单日最多参与前 20 日平均成交量的 0.5%。
- 本地和在线数据都经过 OHLCV、日期、成交量单位和新鲜度校验；固定指数证据未知时失败关闭。
- `requested_symbols` 表示请求股票，`selected_symbols` 表示实际入选股票，`unavailable_symbols` 只表示数据不足或陈旧，不再把有数据但未入选的股票误报为不可用。

## 快速开始

```bash
python -m pip install -r requirements.txt
python quant_fusion.py --start 2025-04-01 --end 2026-07-20 \
  --capital 2000000 --data-dir market_data --indicator-state warm --no-plot
python daily_signal_scan.py --end-date 2026-08-04 \
  --cache-dir data_cache --output-dir daily_signals
python daily_signal_scan.py --account account.json --end-date 2026-08-04 \
  --cache-dir data_cache --output-dir daily_signals
```

## 已验证基线

初始资金 200 万元、2025-04-01 至 2026-07-20、前复权冻结数据、预热模式：

| 股票数量 | 总收益 | 最大回撤 | 交易次数 |
|---:|---:|---:|---:|
| 1 | 530.8950% | -18.3414% | 24 |
| 3 | 1124.7964% | -17.1707% | 200 |
| 5 | 1183.3466% | -16.2478% | 229 |
| 13 | 1038.8287% | -18.6791% | 252 |
| 22 | 1021.3383% | -17.6583% | 242 |

上表为穿越牛熊叠加层默认开启（`enable_cm_overlay=True`）下的生产基线，采用分层保护止损（P0-1）。叠加层在 1 只股票池全程零触发；3/5 只池于 2025-11-24 对 `300502` 触发灾变止损，13 只池于 2026-06-08 对 `688072` 触发盈利分层止损，22 只池触发两次分层止损（`688249`/`600206` 于 2025-11-21、`688249` 于 2026-03-23）。22 只池关闭叠加层时为 898.1569%、-17.2103%、259 笔。弱市风险行为本轮已改变，旧 `regime_validation_results.json` 只作为修改前对照，新的弱市统计必须重新运行 `python run_regime_validation.py --workers 4` 后发布。

## 优势

因果执行边界明确，A 股手数、费用、滑点、涨跌停和成交量容量较完整；数据与风险状态失败关闭；交易、拒单、融合、风险事件和有效配置可审计；趋势基线有精确回归保护。

## 缺点和已知限制

核心文件较大且状态机复杂；参数画像包含行业先验；股票池组成会显著影响结果；日线模型不能重现盘中路径和排队成交；弱市样本存在幸存者偏差和相关性；前复权数据可能被数据源重述；高历史收益不能外推为长期稳定收益。

## 适用行情

适合 AI 硬件和半导体产业链的中长期趋势、高波动趋势延续和龙头强弱分化行情，以及收盘后人工复核、次日执行的低频决策场景。

## 不适用行情

不适合窄幅反复震荡、连续一字涨跌停、长期停牌、分钟级止损、未重新建立参数画像的非科技股票池，以及需要自动托管账户或保证最大亏损的场景。

## 健壮性与灵活性

健壮性来自输入校验、行情哈希、成交量单位契约、严格映射、因果测试、精确回归和失败关闭。灵活性来自逐股票配置、行业画像、单袖套或三袖套、冷启动或预热、外层部署模式、参数优化和本地或在线数据路径。任何参数、参考篮子、费用、映射或冻结数据变化都必须重新通过完整回归。

## 还能继续提升的方向

1. 在保持公共接口和精确回归不变的前提下拆分核心文件。
2. 建立时间点可得的行业、上市、退市、停牌和交易规则数据库。
3. 增加滚动样本外、区块自助法、多成本情景和真实人工执行对账。
4. 扩大弱市验证年份和低相关股票池，并提高核心模块静态类型覆盖率。

## 仓库结构

`quant_fusion.py` 为趋势、执行、组合和风险核心；`regime_adaptive.py` 为外层路由和弱市策略；`daily_signal_scan.py` 为日扫工件；`account_signal_engine.py` 为真实账户建议；`market_data_contracts.py` 为指数与数据契约；`quant_fusion_optimizer.py` 为走步优化；`BACKTEST_RESULTS.md` 记录当前有效验证结论。

## 开发与质量门禁

```bash
python -m compileall -q .
ruff check --select=E,F,W --ignore=E501,E402,E731,E741 .
python -m pytest -q
pyright quant_fusion_optimizer.py daily_signal_scan.py regime_adaptive.py \
  account_signal_engine.py market_data_contracts.py benchmark_validation.py \
  run_regime_validation.py
bandit -r quant_fusion.py regime_adaptive.py daily_signal_scan.py \
  account_signal_engine.py market_data_contracts.py -ll
pip-audit --strict -r requirements-lock.txt
```
