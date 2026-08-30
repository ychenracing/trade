# Quant Fusion A股科技趋势决策系统

## 项目定位

Quant Fusion 是面向 A 股 AI 硬件、光通信和半导体产业链的日线量化研究与人工决策支持项目。它用于收盘后回测、组合验证、日常信号扫描和真实账户持仓审视，不连接券商、不自动下单，也不承诺未来收益。

规范实现位于 `quantfusion/` 模块化单体，根目录的 `quant_fusion.py`、`regime_adaptive.py` 等文件仅保留兼容接口与命令行入口。项目提供冻结行情、哈希校验、严格 JSON 工件、参数搜索、账户时点建议和持续集成门禁。

## 最重要的使用边界

1. 只处理日线数据，信号在收盘后形成，最早在后续可交易日开盘执行。
2. 回测结果来自固定历史样本，不代表未来收益。
3. `--account` 只生成真实账户建议，不把持仓注入历史回测。
4. 默认 `strict_unmapped=True`，未映射股票失败关闭。
5. 趋势路由默认要求请求股票全部具有可观察数据；只有研究调用显式设置 `allow_unavailable_symbols=True` 时才允许过滤缺失股票。

## 当前策略结构

趋势生产引擎由唐奇安突破、双均线趋势和 ATR 通道组成，三类策略保留独立子持仓和审计轨迹。默认 `allocation_mode=ensemble`，总资本（如 200 万元）在 fast、base、slow 三个独立袖套之间分配；`dynamic_sleeve_weights=True` 只在确认的内部状态变化后迁移未使用现金，不合并持仓、策略、挂单、峰值或冷却状态。三个袖套合计资本不超过总资本，不使用杠杆；卖出优先于买入，同一股票卖出默认否决新增买入。股票池从 5 只起使用固定参考篮子过滤候选，参考门槛按 1—8、9—12、13 只以上分为 50%/55%/65%；6—8 股池的新候选需连续两个交易日、9—12 股池需连续四个交易日保留可执行买入意图，证据中断即重置，固定五股在扩展区间保持既有核心身份。固定 13 股生产池扩展到 14 股时，核心仍走既有评分准入；核心外新增标的必须连续两个交易日同时保留可执行买入意图且固定参考分数至少为 0.80，任一条件中断即重置。单股不超过 60%，总仓位不超过 100%，最多持有 6 只。

内部状态机使用等权指数斜率、均线广度、ADX、Hurst 和波动率，将市场标记为 `TREND`、`TRANSITION` 或 `CHOPPY`。

`ProductionReplayEngine` 默认逐日回放外层路由，沿用同一套生产撮合和同一个持续账户。路由日志、风险峰值、持仓、挂单、三个袖套、粘性候选和弱市冷却不会在状态切换时重置。路由只使用当日收盘及以前可见的数据：

- 沪深 300 `000300` 与科技风险指数 `000682` 的 MA60/MA120 中期证据经连续确认后，在 `trend`、`transition_to_weak`、`weak`、`transition_to_trend` 和 `cash` 间低频迁移；
- 已持有趋势账本时，外层路由不重复清仓或重复迁移现金，由跨市场叠加层作为唯一风险执行者；空仓进入弱市时才启用正动量龙头账本；
- 弱市龙头按成熟与新兴双通道评分，弱市周期内冻结入选名单，避免每日重排变成隐性轮动；
- 任一指数缺失、不可解析、历史不足或超过 10 个自然日陈旧：持有现金，不进行个股动量选股；
- 没有正动量股票：持有现金。

3—4 股组合以及包含完整五股固定参考篮子的 5—6 股组合，从回放开始就对“合并账户”使用 14% 预警、18% 确认、20% 紧急回撤线和 252 个交易日再武装期。参考篮子完整时，7—8 股使用 12%/14%/18% 梯度，9 股以上使用 14%/17.5%/18%/22% 梯度；5 股以上但参考篮子不完整时，候选分数缺少固定比较锚，预先保留 15% 现金，并统一改用 10% 预警、11% 两日确认、13% 紧急、20% 终态梯度。以上均不改变各袖套策略。9 股以上组合在账户回撤达到 18% 后还会临时把各袖套风险梯度收紧为 14%/18%/22%/24%，保持正常的 10 个交易日再武装期；账户回撤恢复到 10% 以内后还原原策略。切换只替换风险策略对象，不重置持仓、峰值、锁、挂单或冷却。

弱市策略使用 22% 灾难止损、5 ATR 初始止损、80 个交易日时间止损和盈利后 3 ATR 吊灯止损，同时启用 15% 回撤预警、20% 周期确认、23% 紧急回撤、26% 终身峰值回撤线和 12% 单日损失保护。风险订单在下一可交易开盘执行，跳空或连续跌停仍可能使实际损失超过阈值。

穿越牛熊叠加层（`cross_market_overlay.py`）默认开启，是叠放在 ensemble 之上的 bull-silent 防御层。它从独立的 23 股、子行业等权风险篮子连续采样，支持预警后再冲击、连续恶化和严重多证据直达升级；一级冻结加仓，二级冻结新开仓并只削减最弱非核心持仓，三级进一步降风险。持仓同时采用分层保护止损：灾变止损（自持仓峰值回撤超过 28%）始终待命；成本止损（低于成本 18%）、ATR 吊灯（6 ATR）和盈利分层保护只在相应风险证据与账户回撤门槛同时满足时触发。趋势健康度只在确认风险中将盈利回吐线有界调整 3 个百分点。触发后标的进入 10 个交易日冷却期。专用弱市/现金路由持有执行权时，叠加层继续更新风险状态但不重复卖出或阻断。

子行业参数收缩（`subindustry_shrinkage`，默认 0.5 开启）：细分子行业参数画像（光模块、光器件、存储接口、芯片设计、设备、测试、材料、封装等）在解析时被拉向其粗粒度父画像，只对"允许细分"的参数（最大单票权重、ATR 倍数、风险预算）做小幅收缩，入场/出场周期、盈利保护、加仓与路由参数沿层级共享不收缩，从而降低薄样本过拟合，同时保留已验证的粗粒度趋势结构。

## 风险治理观测层

`risk_governance.py`（默认随引擎自动开启）在既有决策路径之上输出一套与交易动作分离的风险治理证据。全部为纯函数/纯数据结构，不读取、不修改任何交易决策状态，因此对既有回测路径零行为漂移（五池黄金指标逐位不变）：

- **预热健康契约（P0-1）**：每次运行输出 `READY` / `DEGRADED` / `NOT_READY` 三级预热健康报告。逐股统计回测开始日前的可用交易日数，新上市股票不静默获得与成熟股票相同的置信度；独立风险篮（23 股）就绪度按篮内实际可观察帧统计；regime 证据完全缺失时判 `NOT_READY`（风险层失明，失败关闭），存在但陈旧时降级 `DEGRADED` 并输出 `regime_index_stale` 原因。日扫中 `NOT_READY` 自动抑制全部新增买入（fail-closed），`DEGRADED` 仅显著提示。
- **风险事件校准（P0-2）**：事后独立检测"已实现冲击"（未来 20 日组合回撤超过 8%），对每次 L1/L2/L3 警报计算 1/3/5/10/20 日组合最低收益、风险篮最低收益、最大回撤与恢复状态，并汇总 Shock Precision / Recall、领先天数、误报机会成本、漏报冲击深度、牛市沉默率与 L1→L2 升级精度。
- **独立风险意见（P0-3）**：`RiskOpinion` 对象回答"当前风险环境如何"（等级、置信度、市场状态、健康牛市沉默、禁新开仓/冻结加仓语义、建议总敞口上限、最弱簇与原因代码），供人工决策或其他系统作为独立风险裁判意见消费，不直接驱动交易。
- **袖套共识证据（P1-1）**：逐日计算三袖套持仓共识（每只股票的平均持有袖套数）、按 3/2/1 袖套计数的分布、各袖套部署率、最弱袖套与连续退化计数。空仓簿按惯例记满共识、最弱袖套置空、退化计数清零。共识连续退化 3 日以上作为风险意见证据代码输出，不新增状态机。
- **风险篮覆盖置信度（P1-2）**：置信度 = 0.45 × 观察成分比例 + 0.35 × 行业覆盖比例 + 0.20 × 持仓子行业映射比例。覆盖不足时置信度下降，风险意见据此降低强度而不是继续输出同样的 L2/L3 确信度；低于 0.60 时标记 `low_basket_coverage`。
- **L1 冻结机会成本（P1-3）**：峰值停留在 L1（冻结加仓但未升级）的警报段记录其后 20 日组合收益中位数，用于度量 L1 冻结是否多数落在牛市正常回踩上。

以上输出随回测结果自动附出：`warmup_health`、`risk_opinion`、`sleeve_agreement`、`risk_governance_series`（逐日）与 `risk_event_calibration`（事件表与指标）。

## 默认策略参数

完整默认策略字段如下，具体默认值和校验分别以 `quantfusion.config.engine.default_engine_config()` 与 `validate_engine_config()` 为唯一事实来源；旧 `_CoreBacktestEngine._default_config()` 仅委托到该公共接口：

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
- 袖套持仓和成交保持独立；系统不模拟券商级净额。同日相反袖套成交可以双边执行，并分别承担费用与滑点。
- `requested_symbols` 表示请求股票，`selected_symbols` 表示实际入选股票，`unavailable_symbols` 只表示数据不足或陈旧，不再把有数据但未入选的股票误报为不可用。

## 快速开始

```bash
# 安装运行依赖
python -m pip install -r requirements.txt

# 单轮回测（趋势引擎）
python quant_fusion.py --start 2025-04-01 --end 2026-07-20 \
  --capital 2000000 --data-dir data/market --indicator-state warm --no-plot

# 收盘后日扫（生成候选信号）
python daily_signal_scan.py --end-date 2026-08-04 \
  --cache-dir data_cache --output-dir daily_signals

# 真实账户建议（先填入持仓）
cp examples/account.json account.json
python daily_signal_scan.py --account account.json --end-date 2026-08-04 \
  --cache-dir data_cache --output-dir daily_signals
```

安装开发依赖与完整质量门禁见「开发与质量门禁」章节。

## 典型工作流

### 收盘后决策

每个交易日收盘后按以下顺序执行：

1. **数据刷新**：运行 `python daily_signal_scan.py --end-date <TODAY>` 拉取最新日线并生成候选信号。
2. **路由判断**：查看输出中的 `route` 字段（`trend` / `weak` / `cash`），确认今日走哪条路径。
3. **候选审视**：检查 `candidates` 列表，关注排名、触发原因（`reasons`）和建议权重。
4. **持仓建议**：若传入了 `--account`，查看 `actions` 中的买入/卖出/持有建议，结合实际账户人工决策。
5. **次日执行**：信号在次日开盘执行，涨跌停板、成交量容量和滑点可能使实际成交价偏离模拟。

### 参数探索

1. 按 `risk` → `turnover` → `return` 三阶段运行 `quant_fusion_optimizer.py --stage <阶段> --data-dir data/market --regime-data-dir data/regime`，避免一个参数族掩盖另一个参数族的退化。优化器和部署都使用 `ProductionReplayEngine`。
2. 选择同时使用收益、回撤和交易次数三目标 Pareto 前沿，再在近似收益档内优先更低回撤和更少交易。
3. 候选必须通过普通及压力 holdout 推广门：财富不得落后基线超过 1%，回撤不得恶化超过 0.5 个百分点，交易不得增加超过 3%，除非财富至少提高 5%。
4. 任何参数变更都必须重新通过五组趋势基线精确回归，且必须能解释收益、回撤和交易次数变化的原因。

### 股票池扩展

1. 新增标的必须在 `quantfusion.config.universe` 与引擎配置的行业映射中找到对应画像，否则 `strict_unmapped=True` 会直接失败。
2. 新行业需重新建立参数画像和基线，不能直接套用现有科技参数。
3. 使用 `stress_test_prefixes.py` 检查全部前缀、留一、逐一加入、随机子集和顺序置换；默认使用 3 个固定种子，每个种子的每种随机规模与顺序各抽样 50 次，共 983 次生产逐日回放，并每 10 个场景原子检查点续跑。
4. 任何 cross-market / 风险层改动晋级前，还必须通过相对既有正式压力基线的晋级门（P0-4）：固定前缀牛市财富不低于基线 99%，随机子集回撤 P90/P95 最多恶化 0.5 个百分点，全场景最差回撤最多恶化 1 个百分点、最差收益最多恶化 2 个百分点，最差逐一加入财富最多下降 3 个百分点，成交记录 P90/最差最多增加 5/10 笔，风险减仓中位数最多增加 2 笔，且同一 seed 的全排列场景指标必须完全一致。

## 日扫信号与账户建议

日扫输出（`daily_signals/` 目录）以 JSON 工件形式给出。每个扫描日先建立 `snapshots/<日期>/` 冻结目录；同日重跑必须通过 manifest 哈希边车、全部 CSV 内容哈希和精确文件集合校验，任何改写或额外证据文件都失败关闭。核心字段如下：

- `as_of`：信号生成日期（收盘后）。
- `route`：外层路由结果——`trend`（趋势引擎）、`weak`（弱市龙头）或 `cash`（持有现金）。
- `regime_state`：内部状态机状态——`TREND`、`TRANSITION`、`CHOPPY`。
- `candidates`：候选股票列表，每只含 `symbol`、`score`、`target_weight`、`direction`、`reasons`（触发原因列表）。
- `unavailable_symbols`：因数据缺失或陈旧而无法评估的股票（**不**表示"未入选"）。
- `warmup_health`：预热健康契约（P0-1）——`READY` / `DEGRADED` / `NOT_READY` 三级状态、指标与参考篮就绪比例、新上市与陈旧股票清单及原因代码。`NOT_READY` 时输出不可作为正式交易信号，全部新增买入已失败关闭（`buys_suppressed=true`）；`DEGRADED` 时风险判断保留，仅显著提示。
- `risk_opinion`：独立风险意见（P0-3）——风险等级与置信度、市场状态、健康牛市沉默标记、禁新开仓/冻结加仓语义、建议总敞口上限、最弱子行业簇、袖套共识与连续退化计数及原因代码。该意见与交易动作分离，只描述环境。

当传入 `--account account.json` 时，额外输出：

- `actions`：按标的分组的动作建议——`buy`（建议买入股数与金额）、`sell`（建议卖出股数与原因）、`hold`（继续持有）。
- 账户持仓动作保留 `shares` 作为总持股数，并用 `sellable_shares`、`recommended_shares`、`blocked_shares` 和 `execution_status` 显式表达 T+1 可执行性；可卖数量未知时不得把总持股当作建议卖出量。
- `estimated_equity` / `estimated_market_value`：按最新收盘价估算的账户净权益与持仓市值（仅当所有持仓都有数据时给出）。
- `risk_flags`：组合级风险信号——回撤预警、周期确认、紧急回撤、集中度警告等。

> 所有建议仅供人工决策参考，不构成投资建议。实际下单需考虑盘中流动性、涨跌停板、交易规则和个人风险承受能力。

## 风险分层说明

系统在多个层面叠加风险控制，每一层都有独立触发条件：

| 层面 | 机制 | 默认阈值 | 作用 |
|------|------|---------:|------|
| 单股止损 | 硬灾难止损 | 28%（自峰值） | 单票最深底线 |
| 单股保护 | 成本止损 | 18%（低于成本） | 风险预警 + 账户偏离峰值时待命 |
| 单股保护 | ATR 吊灯止损 | 6 ATR | 风险预警 + 账户偏离峰值时待命 |
| 单股保护 | 盈利分层保护 | 18%/22%/26%/28% | 行业冲击确认（level ≥ 2）时分档待命 |
| 组合回撤 | 回撤预警 | 15% | 弱市路径进入观察 |
| 组合回撤 | 周期确认 | 20% | 连续 N 日确认后进入风控 |
| 组合回撤 | 紧急回撤 | 23% | 立即减仓至安全仓位 |
| 组合回撤 | 终身峰值线 | 26%（弱市）/ 28%（趋势） | 清仓锁定 |
| 单日损失 | 单日损失保护 | 12% | 单日极端波动触发 |
| 集中度 | 净敞口集中度 | 按行业簇 | 过集中时削减簇内最弱标的 |
| 冷却期 | 灾变后冷却 | 10 个交易日 | 阻断所有买入路径 |

风险订单在下一可交易开盘执行，跳空或连续跌停可能使实际损失超过阈值，因此以上数值均非收益保证。

## 已验证基线

初始资金 200 万元、2025-04-01 至 2026-07-20、前复权冻结数据、预热模式：

| 股票数量 | 总收益 | 最大回撤 | 实际成交记录 | 日期/股票/方向桶 |
|---:|---:|---:|---:|---:|
| 1 | 530.8950% | -18.3414% | 24 | 5 |
| 3 | 1059.3890% | -16.8153% | 183 | 77 |
| 5 | 1226.3702% | -15.6349% | 207 | 94 |
| 13 | 1039.0505% | -17.0463% | 204 | 105 |
| 22 | 1052.7670% | -15.3842% | 228 | 104 |

上表为全部自动功能默认开启后的精确趋势基线。实际成交记录按每条袖套 `TradeRecord` 计数；去重桶不能证明券商端合单或净额。弱市生产回放、精确序列指纹与验证限制见 `docs/VALIDATION.md`。

## 仓库结构

新代码可直接使用稳定的规范包出口：

```python
from quantfusion.config import PortfolioPolicy, default_engine_config
from quantfusion.data import DataFetcher
from quantfusion.domain import Position, Signal, TradeRecord
from quantfusion.engine import BacktestEngine, SleeveBacktestEngine
```

### 核心模块

| 文件 | 职责 |
|------|------|
| `quantfusion/domain`、`config`、`data` | 稳定领域模型、公共配置事实源、行情契约与显式缓存上下文 |
| `quantfusion/strategy`、`execution`、`portfolio` | 趋势与弱市信号、执行优先级、组合政策与状态边界 |
| `quantfusion/risk`、`regime` | 组合风控、不可变风险动作、治理证据、指数证据与纯路由状态机 |
| `quantfusion/engine` | 单袖套、三袖套 ensemble、持续账户与唯一 `ProductionReplayEngine` 实现 |
| `quantfusion/account`、`application`、`io` | 账户建议、日扫与研究流程、冻结快照、严格工件和原子状态发布 |
| `quantfusion/research` | 候选参数、走步评价、Pareto 晋级门与可续跑研究证据 |
| 根目录兼容文件 | 保持旧导入和命令行不变，全部委托给 `quantfusion/` 规范实现 |

### 工具脚本

实现统一放在 `scripts/`，推荐使用 `python -m scripts.<模块名>`；原根目录命令和导入名由薄兼容层继续支持。

| 文件 | 用途 |
|------|------|
| `scripts/benchmark_validation.py` | 基准验证工具，对比不同配置下的收益与回撤 |
| `scripts/run_regime_validation.py` | 弱市全量验证，生成多股票池、多随机种子的统计分布 |
| `scripts/validate_basket.py` | 篮子有效性验证，检查数据完整性与映射一致性 |
| `scripts/download_eastmoney_qfq.py` | 东方财富前复权数据下载器 |
| `scripts/backtest_universes.py` | 多股票池批量回测，生成验证工件 |
| `scripts/backtest_cambricon_universe.py` | 寒武纪九股池专项回测，生成验证工件 |
| `stress_test_prefixes.py` | 可续跑的 983 场景生产回放压力测试，记录成交记录、袖套成交和原因归因 |

### 测试文件

| 文件 | 覆盖范围 |
|------|---------|
| `tests/regression/test_quant_fusion.py` | 核心引擎与五股票池精确经济回归 |
| `tests/regression/test_cross_market_overlay.py` | 穿越牛熊叠加层机制回归 |
| `tests/unit/test_risk_governance.py` | 风险治理观测层单元测试 |
| `tests/regression/test_regime_adaptive.py` | 外层路由与弱市策略回归 |
| `tests/contract/test_regime_safety_contracts.py` | 弱市失败关闭与股票池完整性契约 |
| `tests/integration/test_daily_*.py` | 按快照、状态、信号、结构、事务和命令行职责拆分的日扫集成契约 |
| `tests/contract/test_architecture.py` | 单向依赖、无环导入、禁止旧模块反向依赖和兼容层规模守卫 |
| `tests/unit/` | 领域叶子、缓存上下文、引擎、状态机、风险动作、账户与研究模块测试 |
| `tests/contract/test_fail_closed_boundaries.py` | 不可解析数据、缺失指数与格式异常契约 |
| `tests/regression/test_quant_fusion_optimizer.py` | 优化器走步、候选淘汰和留出集回归 |
| `tests/contract/test_repository_hygiene.py` | 仓库布局、文档一致性与生成工件隔离契约 |
| `tests/regression/test_system_boundaries.py` | 账户、行情、基准与叠加层公共边界回归 |
| `tests/unit/test_stress_scenarios.py` | 压力场景生成、确定性和尾部统计单元测试 |

### 数据目录

| 目录 | 内容 |
|------|------|
| `data/market/` | 趋势回测用前复权日线数据、清单与字节哈希 |
| `data/regime/` | 外层路由与弱市验证用历史数据、清单与字节哈希 |
| `tests/fixtures/` | 持续集成读取的精确黄金基线 |
| `artifacts/validation/` | 已审查的批量回测与压力验证工件 |
| `examples/` | 不含真实账户信息的输入样例 |

旧参数值 `--data-dir market_data` 和 `--regime-data-dir historical_data` 在调用方没有同名真实目录时会自动解析到新位置，现有自动化无需立即迁移。

### 配置与文档

| 文件 | 说明 |
|------|------|
| `README.md` | 本文件，项目总览与使用指南 |
| `docs/VALIDATION.md` | 当前有效回测、序列指纹与压力验证结论 |
| `docs/ARCHITECTURE.md` | 模块边界、依赖规则、状态所有权、事务顺序和扩展指南 |
| `data/README.md` | 两组冻结数据的来源、用途与完整性校验 |
| `LICENSE` | MIT 开源协议 |
| `requirements.txt` | 运行时依赖 |
| `requirements-dev.txt` | 开发与测试依赖 |
| `requirements-lock.txt` | 锁定版本依赖（CI 使用，含哈希） |
| `.gitignore` | 忽略规则 |
| `examples/account.json` | 账户快照示例，复制为 `account.json` 后填入真实持仓使用 |
| `.github/workflows/ci.yml` | 持续集成配置（测试、类型检查、安全审计、精确回归） |

### 生成工件（默认不持久化）

以下工件由工具脚本生成，按需重新运行即可，不常驻仓库：

- `artifacts/validation/regime_validation_results.json`：弱市全量验证结果
- `optimizer_output/`：`quant_fusion_optimizer.py` 的走步优化报告与推荐配置
- `daily_signals/`：每日扫描输出
- `data_cache/`：行情数据缓存
- `benchmark_validation.json`：基准验证输出

仓库持久化的 `artifacts/validation/prefix_stress.json` 与 `artifacts/validation/universe_stress.json` 是可复现压力工件；后者记录三个固定种子、每种规模每种子 50 次随机抽样、50 次顺序置换和全部硬门禁结果。

如需持久化某次验证结果，将对应文件放入 `artifacts/validation/` 并更新 `docs/VALIDATION.md`。

## 与其他文档的关系

- `docs/VALIDATION.md` 是当前有效基线、序列指纹和压力结果的权威记录。
- `docs/ARCHITECTURE.md` 是规范模块边界和状态所有权的维护契约。
- `data/README.md` 说明冻结行情的来源、用途和校验方式。
- 三者分工：README 负责使用，架构文档负责边界，验证文档负责证明结果未漂移。

## 故障排查

| 现象 | 可能原因 | 处理方式 |
|------|---------|---------|
| `strict_unmapped` 报错 | 新股票未在行业映射中注册 | 在 `quantfusion.config.universe` 和规范引擎配置中添加映射，或临时使用 `strict_unmapped=False`（仅研究用） |
| 路由结果为 `cash` | 指数数据缺失 / 陈旧 / 不可解析 | 检查 `data/regime/` 中的 `000300.csv` 与 `000682.csv` 是否最新 |
| 候选列表为空 | 所有股票动量为负 / 数据不足 | 正常现象，弱市中持有现金是预期行为 |
| 回测收益与基线不符 | 数据版本 / 参数配置不一致 | 使用 `data/market/` 冻结数据、`--indicator-state warm`、默认参数，核对 `SHA256SUMS` |
| 账户建议为零买入 | 现金不足 / 超仓位上限 / 集中度限制 | 检查 `risk_flags` 和 `actions[].reasons`，确认账户快照是否完整 |
| 日扫提示 `NOT_READY` 并抑制买入 | 预热健康契约失败关闭（指标历史不足或 regime 证据缺失） | 检查 `warmup_health.reasons`（如 `indicator_warmup_incomplete`、`regime_index_missing_or_stale`、`new_symbols_without_full_history`），补齐预热数据或延长回看窗口后重跑 |
| 日扫提示 `DEGRADED` | 数据陈旧 / 新上市股票 / 参考篮不完整 | 风险判断保留，按 `warmup_health.reasons` 人工确认新增风险动作；刷新数据可消除 `regime_index_stale` 与 `stale_symbols` |
| 测试失败 | 基线变更未同步 | 检查 `tests/fixtures/backtest_golden_metrics.json` 是否与代码一致；任何策略变更都必须更新基线并说明原因 |

## 优势

因果执行边界明确，A 股手数、费用、滑点、涨跌停和成交量容量较完整；数据与风险状态失败关闭；交易、拒单、融合、风险事件和有效配置可审计；趋势基线有精确回归保护。

## 缺点和已知限制

持续账户、路由和日扫事务仍具有较高编排复杂度；参数画像包含行业先验；股票池组成会显著影响结果；日线模型不能重现盘中路径和排队成交；弱市样本存在幸存者偏差和相关性；前复权数据可能被数据源重述；高历史收益不能外推为长期稳定收益。

## 适用行情

适合 AI 硬件和半导体产业链的中长期趋势、高波动趋势延续和龙头强弱分化行情，以及收盘后人工复核、次日执行的低频决策场景。

## 不适用行情

不适合窄幅反复震荡、连续一字涨跌停、长期停牌、分钟级止损、未重新建立参数画像的非科技股票池，以及需要自动托管账户或保证最大亏损的场景。

## 健壮性与灵活性

健壮性来自输入校验、行情哈希、成交量单位契约、严格映射、因果测试、精确回归和失败关闭。灵活性来自逐股票配置、行业画像、单袖套或三袖套、冷启动或预热、外层部署模式、参数优化和本地或在线数据路径。任何参数、参考篮子、费用、映射或冻结数据变化都必须重新通过完整回归。

## 还能继续提升的方向

1. 在保持公共接口和精确回归不变的前提下逐步增强规范模块类型精度与复杂度预算。
2. 建立时间点可得的行业、上市、退市、停牌和交易规则数据库。
3. 增加滚动样本外、区块自助法、多成本情景和真实人工执行对账。
4. 扩大弱市验证年份和低相关股票池，并提高核心模块静态类型覆盖率。

## 开发与质量门禁

```bash
# 安装开发依赖
python -m pip install -r requirements-dev.txt

# 编译检查
python -m compileall -q .

# 代码风格
ruff check --select=E,F,W --ignore=E501,E402,E731,E741 .

# 完整测试
python -m pytest -q

# 类型检查（整个规范包）
pyright quantfusion

# 安全审计
bandit -r quantfusion scripts *.py -ll

# 依赖漏洞检查
pip-audit --strict -r requirements-lock.txt
```

CI 在每次推送时自动执行上述全部检查，并额外运行五组趋势基线回归（1/3/5/13/22 只股票池）：成交记录、卖出记录和日期/股票/方向桶等整数指标要求精确一致，总收益与最大回撤等浮点指标仅容忍跨平台 1-ulp 级别的浮点求和顺序差异（`rel_tol=1e-9`，远小于任何真实行为漂移的量级）。任何策略、费用、数据或映射变更都必须更新基线并在 `docs/VALIDATION.md` 中说明变化原因。
