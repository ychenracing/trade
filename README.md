# Quant Fusion A股科技趋势决策系统

## 项目定位

Quant Fusion 是一个面向 A 股 AI 硬件、光通信和半导体产业链的日线量化研究与人工决策支持项目。它用于收盘后回测、组合验证、日常信号扫描和真实账户持仓审视，不连接券商，不自动下单，也不承诺未来收益。

生产回测核心集中在 `quant_fusion.py`。系统同时提供：

- 趋势行情下的三策略、三袖套组合引擎；
- 弱市或趋势证据不足时的因果路由；
- 真实账户时点建议；
- 参数搜索、跨股票池回归、压力测试和简单基准归因；
- 冻结行情、哈希校验、严格 JSON 工件和持续集成门禁。

## 最重要的使用边界

1. 本项目只处理日线数据，不适用于高频、分时或盘中自动交易。
2. 所有信号在收盘后形成，最早在后续可交易日开盘执行。
3. 回测结果来自固定历史样本，不能视为未来收益保证。
4. 实际成交会受到停牌、涨跌停、冲击成本、成交量和人工执行偏差影响。
5. `--account` 只生成真实持仓的时点建议，不把持仓注入历史回测，也不计算伪造的实盘历史收益。
6. 新股票必须先完成行业、参数和风险分组映射；默认 `strict_unmapped=True`，未映射股票会失败关闭。

## 当前策略结构

### 1. 趋势生产引擎

趋势引擎同时运行三类策略：

- **唐奇安突破**：在滞后一日的通道上轨突破后入场，在下轨、硬止损、盈利保护或快速反转条件下退出。
- **双均线趋势**：使用短长期均线、趋势强度和风险约束识别持续趋势。
- **ATR 通道**：使用波动率通道确认价格扩张，并以 ATR 跟踪止损保护趋势利润。

三类策略不是简单投票后共用一个虚拟仓位，而是保留独立子持仓和审计轨迹。同一股票同一交易日的买入会按确认数量缩放；卖出优先于买入，默认启用股票级卖出否决。

### 2. 三袖套组合

默认 `allocation_mode=ensemble`，初始资金被分成三个固定虚拟袖套：

- 短袖套：分配窗口 `(3, 5, 10)`；
- 中袖套：分配窗口 `(5, 10, 20)`；
- 长袖套：分配窗口 `(5, 20, 60)`。

三个袖套共享交易日历、股票数量上限、成交量容量和同步执行顺序，但不互相借用闲置现金。所有卖出先执行，再按全局排序接纳新股票。

### 3. 候选与集中度控制

- 股票池不超过 6 只时，所有股票保留候选资格，由各自信号决定是否入场。
- 股票池超过 6 只时，候选需要达到固定参考篮子的第 50 分位，并受最多 6 只持仓限制。
- 单只股票最大权重为 60%，总仓位最大 100%。
- 一只或两只股票缺少有效横截面信息时，系统使用更慢的时间序列趋势契约。
- 默认固定参考篮子为中际旭创、新易盛、天孚通信、澜起科技和兆易创新。改变投资领域时，应提供新的稳定参考篮子，而不是直接复用本项目的科技硬件篮子。

### 4. 内部市场状态机

趋势引擎内部使用固定篮子的等权指数斜率、均线广度、ADX 中位数、Hurst 指数和波动率分位，对市场标记为 `TREND`、`TRANSITION` 或 `CHOPPY`。状态变化必须经过多日确认和最短保持期，避免单日噪声频繁改变仓位。

内部状态机属于趋势引擎的一部分；它与外层 `regime_adaptive.py` 的部署路由不是同一个概念。

### 5. 外层因果部署路由

`regime_adaptive.py` 在部署边界只使用当时可见的数据：

- 沪深 300 `000300` 与科技风险指数 `000682` 都满足 MA20 大于 MA60：使用原趋势生产引擎；
- 否则：在请求股票池中选择 240 个交易日动量为正的前三名，使用低换手持有策略；
- 没有完整、新鲜的指数证据或没有正动量股票：持有现金。

历史回测的路由边界固定在开始日前，不会用后来的行情改写过去。日常扫描会另外计算“当前路由”。若当前路由与历史回放路由不同，新增买入失败关闭，卖出仍保留。

### 6. 弱市策略

弱市策略最多持有三只正 240 日动量龙头，每只不超过 59%。主要退出规则：

- 入场时使用 22% 灾难止损和 5 ATR 止损，取更严格者；
- 持仓至少 80 个交易日且收益不高于 -10% 时触发时间止损；
- 峰值收益达到 30% 后启用 3 ATR 吊灯止损；
- 一次退出后，在该次选择周期内不重新入场；
- 行情证据超过 10 个自然日视为陈旧。

弱市策略故意把底层组合回撤阈值放宽到接近 100%，避免趋势引擎的组合熔断干扰该独立策略。因此弱市风险主要由逐股票止损承担，不能把它理解为最大回撤有硬性 20% 保证。

## 交易、费用和因果性

- 信号使用当日收盘及之前的数据生成。
- 唐奇安通道和反转低点均向后移动一日，避免当日高低价进入当日决策。
- 订单最早在后续可交易日开盘执行。
- A 股最小交易单位为 100 股。
- 模拟佣金率 0.025%，最低佣金 5 元，卖出印花税 0.05%，单边滑点 0.1%。
- 按股票代码估计主板、创业板、科创板、北交所及 ST 涨跌停限制。
- 买入受现金、单股权重、总仓位、行业权重、持仓数量和成交量容量共同约束。
- 组合每日最多参与前 20 日平均成交量的 0.5%，同日同股票同方向订单共享容量。
- 开放持仓在结束日按收盘价计价，不做虚构的期末强制平仓。

## 风险控制

### 周期回撤

有效确认阈值随股票池数量变化：

```text
确认阈值 = 23% - 2% / 股票数量
紧急阈值 = 27% - 2% / 股票数量
```

触发周期锁后，系统清仓并等待 10 个交易日，再重置周期高水位。独立的终身峰值回撤线为 28%，一旦触发不自动恢复。18% 仅为审计预警，开盘跳空可能使实际回撤超过决策阈值。

### 板块广度保护

固定五股篮子用于识别同步冲击。至少需要四只有效观测；缺失数据会暂停恢复确认，而不会清除已确认的风险状态。板块冲击触发后，新增买入受限，并按配置生成清仓信号。

### 风险状态连续性

日扫每次都从 `--start-date` 完整回放历史，不把上次结束时的风险状态注入过去。保存的 `risk_state.json` 只用于身份校验、连续性展示和终态审计。

身份包含股票池、开始日期、指标状态、资金和预热天数。身份不一致时：

- 买入信号进入 `blocked_signals`；
- 卖出和持有信号继续显示；
- 旧风险状态不被覆盖；
- 只有显式使用 `--reset-risk-state` 才建立新身份。

工件先写入，风险状态后提交；两者使用临时文件、`fsync` 和原子替换，并共享 `run_id`。

## 真实账户模式

`daily_signal_scan.py --account 账户文件.json` 调用独立的 `account_signal_engine.py`。它读取真实现金、成本、建仓日期和历史最高收盘价，生成：

- `HOLD`：没有触发账户级退出条件；
- `SELL`：触发保护止损或均线趋势破坏；
- `REDUCE_REVIEW`：当前路由偏防守，或持仓不在弱市龙头内；
- `BUY_CANDIDATE`：现金大于零且存在新的弱市龙头候选；
- `DATA_ERROR`：持仓行情无法可靠获取。

只要任一持仓无法定价，`valuation_complete` 为 `false`，`estimated_market_value` 和 `estimated_equity` 为 `null`，避免把部分持仓估值伪装成完整总资产。`priced_market_value` 仅表示已成功定价部分。

账户文件示例：

```json
{
  "cash": 370000,
  "peak_equity": 3000000,
  "positions": {
    "300308": {
      "shares": 900,
      "avg_cost": 100.0,
      "entry_date": "2026-01-02",
      "highest_close": 160.0
    }
  }
}
```

股票代码必须为六位字符串，股数必须为正整数，价格必须为正有限数。

## 默认策略参数

以下表格直接对应 `_CoreBacktestEngine._default_config()`。行业配置和逐股票覆盖只修改表中允许覆盖的字段，最终生效值会写入回测结果的 `resolved_symbol_configs`。

| 参数 | 默认值 | 含义 |
|---|---:|---|
| `entry_period` | `8` | 唐奇安入场通道窗口 |
| `exit_period` | `3` | 唐奇安退出通道窗口 |
| `adx_threshold` | `12` | 趋势强度最低阈值 |
| `adx_period` | `10` | ADX 计算窗口 |
| `atr_period` | `10` | ATR 计算窗口 |
| `rsi_period` | `20` | RSI 计算窗口 |
| `ma_short` | `15` | 短期均线窗口 |
| `ma_long` | `60` | 长期均线窗口 |
| `atr_multiplier` | `1.0` | 风险定仓使用的 ATR 倍数 |
| `trail_atr_mult` | `4.0` | 盈利后跟踪止损的 ATR 倍数 |
| `channel_mult` | `2.0` | ATR 通道上轨倍数 |
| `channel_lower_mult` | `3.0` | ATR 通道下轨倍数 |
| `risk_pct` | `0.03` | 单次理论风险预算比例 |
| `hard_stop` | `0.15` | 相对成本价的硬止损比例 |
| `strategy_weight` | `0.98` | 单策略可使用的资金比例 |
| `max_symbol_weight` | `0.60` | 单只股票最大权重 |
| `max_total_weight` | `1.00` | 组合最大总仓位 |
| `max_units` | `20` | 单策略最多加仓单元 |
| `max_drawdown` | `0.165` | 底层兼容配置中的最大回撤阈值 |
| `daily_loss_limit` | `0.06` | 单日损失保护阈值 |
| `sector_guard_enabled` | `True` | 是否启用板块广度保护 |
| `sector_guard_min_symbols` | `5` | 启用板块保护所需最少观测数 |
| `sector_shock_return` | `-0.05` | 板块冲击收益阈值 |
| `sector_shock_breadth` | `0.20` | 板块冲击广度阈值 |
| `sector_shock_ma` | `5` | 板块冲击均线窗口 |
| `sector_shock_window` | `4` | 板块冲击确认窗口 |
| `sector_shock_confirmations` | `2` | 板块冲击确认次数 |
| `sector_recovery_ma` | `5` | 板块恢复均线窗口 |
| `sector_recovery_breadth` | `0.80` | 板块恢复广度阈值 |
| `sector_recovery_confirmations` | `2` | 板块恢复确认次数 |
| `symbol_level_sell_veto` | `True` | 同一股票卖出信号是否否决全部买入信号 |
| `momentum_lookback` | `5` | 兼容候选排序动量窗口 |
| `max_positions` | `6` | 最多同时持有股票数 |
| `group_min_slots` | `2` | 兼容行业保底槽位；当前协调器可忽略弱候选 |
| `fusion_single_scale` | `0.9` | 单策略确认时的目标仓位缩放 |
| `fusion_double_scale` | `1.0` | 双策略确认时的目标仓位缩放 |
| `fusion_triple_scale` | `1.1` | 三策略确认时的目标仓位缩放 |
| `profit_lock_activation` | `0.20` | 启动盈利保护所需峰值收益 |
| `profit_lock_giveback` | `0.22` | 盈利保护允许的回撤比例 |
| `reversal_break_giveback` | `0.22` | 趋势破坏退出允许的峰值回撤 |
| `reversal_exit_period` | `6` | 快速反转低点窗口 |
| `reversal_loss_cut` | `0.10` | 反转亏损退出比例 |
| `reversal_turtle_enabled` | `True` | 唐奇安策略是否启用快速反转退出 |
| `reversal_dual_ma_enabled` | `True` | 双均线策略是否启用快速反转退出 |
| `reversal_atr_channel_enabled` | `True` | ATR 通道策略是否启用快速反转退出 |
| `combined_group_weight_limits` | `海外算力 1.0；国产半导体 0.8` | 行业组合权重上限 |
| `liquidate_on_circuit_breaker` | `True` | 触发组合熔断后是否生成清仓信号 |
| `strict_unmapped` | `True` | 未映射股票是否失败关闭 |
| `commission_rate` | `0.00025` | 佣金率 |
| `stamp_duty` | `0.0005` | 卖出印花税率 |
| `slippage` | `0.001` | 单边滑点比例 |
| `min_commission` | `5.0` | 单笔最低佣金 |
| `max_pending_buy_days` | `5` | 未成交买单最多保留交易日 |
| `pyramid_add_atr` | `1.0` | 加仓所需价格推进 ATR 倍数 |
| `pyramid_risk_decay` | `1.0` | 后续加仓风险预算衰减 |
| `atr_method` | `wilder` | ATR 平滑方式 |
| `limit_price_epsilon` | `0.001` | 涨跌停判定容差 |
| `per_symbol_limit_pct` | `空映射` | 逐股票涨跌停比例覆盖 |
| `st_symbols` | `空集合` | 按 5% 涨跌停处理的股票集合 |
| `risk_free_rate` | `0.0` | 夏普比率使用的年化无风险利率 |
| `market_regime_enabled` | `True` | 是否启用内部市场状态机 |
| `regime_ewi_lookback` | `20` | 等权指数斜率窗口 |
| `regime_breadth_ma_long` | `20` | 广度长期均线窗口 |
| `regime_adx_trend` | `25` | 趋势状态 ADX 阈值 |
| `regime_adx_choppy` | `20` | 震荡状态 ADX 阈值 |
| `regime_hurst_window` | `100` | Hurst 指数窗口 |
| `regime_hurst_trend` | `0.55` | 趋势状态 Hurst 阈值 |
| `regime_hurst_choppy` | `0.45` | 震荡状态 Hurst 阈值 |
| `regime_vol_lookback` | `60` | 波动率分位窗口 |
| `regime_vol_extreme_pct` | `0.90` | 极端波动分位阈值 |
| `regime_ewi_slope_trend` | `0.02` | 趋势状态等权指数斜率阈值 |
| `regime_ewi_slope_choppy` | `-0.02` | 震荡状态等权指数斜率阈值 |
| `regime_score_trend` | `2` | 趋势候选总分阈值 |
| `regime_score_choppy` | `-3` | 震荡候选总分阈值 |
| `regime_choppy_confirmations` | `2` | 进入震荡状态确认次数 |
| `regime_trend_confirmations` | `3` | 进入趋势状态确认次数 |
| `regime_recovery_confirmations` | `3` | 从震荡状态恢复确认次数 |
| `regime_min_state_hold` | `3` | 状态最少保持交易日 |
| `regime_transition_scale` | `1.0` | 过渡状态仓位缩放 |
| `regime_trend_to_transition_confirmations` | `3` | 趋势转过渡确认次数 |
| `regime_choppy_exit_ratio` | `0.30` | 震荡状态退出比例 |
| `regime_transition_exit_ratio` | `0.0` | 过渡状态退出比例 |

## 组合策略参数

以下字段来自 `PortfolioPolicy`，控制袖套、候选、成交量和回撤状态机。

| 参数 | 默认值 | 含义 |
|---|---:|---|
| `allocation_mode` | `ensemble` | 默认使用三袖套组合 |
| `single_lookbacks` | `(5, 10, 20)` | 单袖套分配动量窗口 |
| `allocation_horizons` | `(3,5,10)/(5,10,20)/(5,20,60)` | 三袖套分配窗口 |
| `drawdown_alert` | `0.18` | 审计型回撤预警 |
| `confirmed_drawdown` | `0.23` | 周期回撤确认基准 |
| `drawdown_confirmations` | `2` | 周期回撤确认次数 |
| `emergency_drawdown` | `0.27` | 周期紧急回撤基准 |
| `adv_lookback` | `20` | 成交量容量回看窗口 |
| `max_order_adv_ratio` | `0.005` | 组合单日参与前 20 日均量的最大比例 |
| `candidate_lookbacks` | `(10, 20, 40)` | 候选动量参考窗口 |
| `candidate_horizons` | `(10,20,40)/(10,20,40)/(10,40,80)` | 各袖套候选窗口 |
| `rearm_trading_days` | `10` | 周期锁定后的现金冷却交易日 |
| `terminal_drawdown` | `0.28` | 终身峰值回撤终止线 |
| `concentration_drawdown_adjustment` | `0.02` | 集中度回撤收紧系数 |
| `candidate_reference_percentile` | `0.50` | 大股票池候选最低参考分位 |
| `regime_symbols` | `300308/300502/300394/688008/603986` | 固定非交易状态篮子 |
| `market_regime_enabled` | `True` | 是否启用内部市场状态机 |
| `regime_ewi_lookback` | `20` | 等权指数斜率窗口 |
| `regime_breadth_ma_long` | `20` | 广度长期均线窗口 |
| `regime_adx_trend` | `25` | 趋势状态 ADX 阈值 |
| `regime_adx_choppy` | `20` | 震荡状态 ADX 阈值 |
| `regime_hurst_window` | `100` | Hurst 指数窗口 |
| `regime_hurst_trend` | `0.55` | 趋势状态 Hurst 阈值 |
| `regime_hurst_choppy` | `0.45` | 震荡状态 Hurst 阈值 |
| `regime_vol_lookback` | `60` | 波动率分位窗口 |
| `regime_vol_extreme_pct` | `0.90` | 极端波动分位阈值 |
| `regime_ewi_slope_trend` | `0.02` | 趋势状态等权指数斜率阈值 |
| `regime_ewi_slope_choppy` | `-0.02` | 震荡状态等权指数斜率阈值 |
| `regime_score_trend` | `2` | 趋势候选总分阈值 |
| `regime_score_choppy` | `-3` | 震荡候选总分阈值 |
| `regime_choppy_confirmations` | `2` | 进入震荡状态确认次数 |
| `regime_trend_confirmations` | `3` | 进入趋势状态确认次数 |
| `regime_recovery_confirmations` | `3` | 从震荡状态恢复确认次数 |
| `regime_min_state_hold` | `3` | 状态最少保持交易日 |
| `regime_transition_scale` | `1.0` | 过渡状态仓位缩放 |
| `regime_trend_to_transition_confirmations` | `3` | 趋势转过渡确认次数 |
| `regime_choppy_exit_ratio` | `0.30` | 震荡状态退出比例 |
| `regime_transition_exit_ratio` | `0.0` | 过渡状态退出比例 |

### 集中度后的有效阈值

`confirmed_drawdown` 和 `emergency_drawdown` 会按股票数量减去 `concentration_drawdown_adjustment / N`。`terminal_drawdown` 不随股票数量变化。

## 参数路由与扩展

系统按股票显式映射到光模块、海外存储材料、国产设计、国产材料、晶圆制造、半导体设备等参数画像。`per_symbol_config` 只允许覆盖策略级字段，组合级字段必须在全局配置或 `PortfolioPolicy` 中修改。

新增股票的正确步骤：

1. 补充分类、行业组、参数画像和执行优先级；
2. 使用冻结前复权数据；
3. 运行单股、少量股票和大股票池回归；
4. 检查收益、最大回撤、交易次数、成交量容量和参数邻域；
5. 更新文档与测试后再纳入日扫股票池。

## 快速开始

安装运行依赖：

```bash
python -m pip install -r requirements.txt
```

使用冻结数据运行五股趋势回测：

```bash
python quant_fusion.py \
  --start 2025-04-01 \
  --end 2026-07-20 \
  --capital 2000000 \
  --data-dir market_data \
  --indicator-state warm \
  --no-plot
```

不传 `--data-dir` 时，通过 AKShare 按东方财富、新浪、腾讯顺序容错。东方财富和腾讯的手数会转换为股；新浪已是股。缓存必须带成交量单位侧车文件，旧缓存不会被猜测复用。

运行日常模拟信号：

```bash
python daily_signal_scan.py \
  --end-date 2026-08-04 \
  --cache-dir data_cache \
  --output-dir daily_signals
```

运行真实账户时点建议：

```bash
python daily_signal_scan.py \
  --account account.json \
  --end-date 2026-08-04 \
  --cache-dir data_cache \
  --output-dir daily_signals
```

运行外层路由验证：

```bash
python run_regime_validation.py --workers 4
```

运行简单基准：

```bash
python benchmark_validation.py \
  --symbols 300308,300502,300394,688008,603986 \
  --data-dir market_data \
  --regime-data-dir historical_data \
  --start 2025-04-01 \
  --end 2026-07-20
```

运行参数优化：

```bash
python quant_fusion_optimizer.py \
  --symbol 300308,300502,300394,688008,603986 \
  --data-dir market_data \
  --start 2024-01-02 \
  --test-start 2026-01-05 \
  --end 2026-07-20 \
  --train-months 12 \
  --validation-months 6 \
  --step-months 6 \
  --candidates 40 \
  --seed 17 \
  --output-dir optimizer_output
```

优化器采用扩展训练窗口、非重叠验证窗口、参数邻域支持、收益/回撤帕累托筛选和一次性最终留出集。优化结果仍然是历史研究结果，不代表未来最优。

## 数据目录

- `market_data/`：2025—2026 生产回归使用的前复权冻结快照和 SHA-256 清单。
- `historical_data/`：2022—2024 弱市验证及两只固定指数证据。
- 在线缓存：由 `data_cache/` 保存，并通过成交量单位元数据避免不同提供方口径混用。

刷新冻结数据属于新的研究快照，必须重新生成哈希、回归结果和文档，不能静默覆盖。

## 已验证基线

初始资金 200 万元、2025-04-01 至 2026-07-20、前复权冻结数据、指标预热模式：

| 股票数量 | 总收益 | 最大回撤 | 交易次数 |
|---:|---:|---:|---:|
| 1 | 530.8950% | -18.3414% | 24 |
| 3 | 1083.6973% | -17.9190% | 194 |
| 5 | 1115.9924% | -15.8573% | 222 |
| 13 | 1038.7405% | -18.4072% | 324 |
| 22 | 983.5716% | -16.2177% | 244 |

精确数值由 `backtest_golden_metrics.json` 和 CI 的 `Exact backtest regression` 门禁保护。更详细的验证和限制见 `BACKTEST_RESULTS.md`。

## 优势

- **因果性明确**：信号与执行分离，通道滞后，历史路由不使用未来数据。
- **A 股执行约束较完整**：T+1、手数、费用、滑点、涨跌停、成交量容量均纳入。
- **多层风险控制**：逐持仓止损、板块广度、周期回撤和终身回撤互相独立。
- **股票池扩展有明确边界**：显式映射、固定参考篮子和严格失败关闭。
- **结果可审计**：成交、拒单、融合、风险事件、有效配置和工件身份可追踪。
- **测试强**：包含未来数据变动、陈旧证据、晚上市股票、严格 JSON、原子写入、CLI 和精确收益回归。
- **实盘与回测隔离**：真实账户只做时点建议，不污染历史状态机。

## 缺点和已知限制

- 核心文件较大，状态机和多袖套协调复杂，修改成本高。
- 参数画像含有较强的行业与股票先验，不适合直接迁移到消费、医药或周期行业。
- 股票池结果依赖资产组成，不能保证增加或删除股票后收益不变。
- 日线开盘成交模型无法模拟盘中路径、排队成交和真实冲击成本。
- 涨跌停判断属于估计模型，不能完全覆盖复牌、除权和交易所特殊规则。
- 外层路由是边界决策，不是每天改写历史持仓的动态切换器。
- 弱市样本存在幸存者偏差、股票池相关性和有效独立样本不足。
- 历史前复权数据可能因后续公司行动被数据源重述。
- 回测收益很高，部分来自科技产业链集中上涨阶段，不应外推为长期稳定收益。
- 当前账户建议不给出自动下单股数，需人工结合可用资金、税费和执行价格确认。

## 适用行情

- AI 硬件和半导体产业链的中长期趋势上涨；
- 高波动但趋势延续、能够让盈利头寸持续扩张的行情；
- 股票池中存在明显强弱分化，龙头动量具有延续性的行情；
- 收盘后人工复核、次日执行的低频决策场景。

## 不适用行情

- 无方向、快速来回反转的窄幅震荡；
- 连续一字涨跌停、长期停牌或流动性极差的股票；
- 需要分钟级止损或盘中择时的场景；
- 非科技产业链且没有重新建立参考篮子和参数画像的股票池；
- 指数和个股数据不完整、陈旧或来源口径不清的日期；
- 需要自动托管真实账户、自动下单或保证最大亏损的场景。

## 健壮性与灵活性

健壮性来自输入校验、行情哈希、提供方单位契约、严格映射、因果测试、精确回归和失败关闭。灵活性来自逐股票配置、行业画像、单袖套/三袖套选择、冷/热指标状态、外层部署模式、参数优化和本地/在线数据双路径。

这种灵活性不是“任意改参数都安全”。任何策略参数、参考篮子、交易费用、股票分类或冻结数据变化，都必须重新通过完整测试和收益/回撤/交易次数回归。

## 还能继续提升的方向

1. 将大核心文件按数据、信号、执行、组合、风险和报告拆分，同时保持公共接口和精确回归不变。
2. 为所有新增股票建立时间点可得的行业和上市状态数据库，减少幸存者偏差。
3. 增加退市、停牌、复牌和不同板块规则的历史时间变化模型。
4. 使用滚动样本外、区块自助法和多成本情景报告置信区间，而不只报告点估计。
5. 增加真实人工执行记录，与模型开盘价、滑点和未成交原因做持续对账。
6. 扩大弱市验证年份和非重叠股票池，提升有效独立样本量。
7. 为账户模式增加人工确认后的目标仓位计算，但仍保持不自动下单。
8. 在不降低精确回归保护的前提下，提高核心模块的静态类型覆盖率。

## 仓库结构

- `quant_fusion.py`：生产回测、执行、组合和风险核心。
- `regime_adaptive.py`：外层因果路由和弱市策略。
- `daily_signal_scan.py`：日常模拟信号和风险状态工件。
- `account_signal_engine.py`：真实账户时点建议。
- `market_data_contracts.py`：指数刷新和行情契约。
- `quant_fusion_optimizer.py`：走步验证和参数搜索。
- `benchmark_validation.py`：简单基准归因。
- `run_regime_validation.py`：弱市、留出集和牛市基线验证。
- `backtest_universes.py`、`stress_test_prefixes.py`、`backtest_cambricon_universe.py`：组合回归脚本。
- `test_*.py`：单元、集成、文档一致性和精确回归测试。
- `BACKTEST_RESULTS.md`：当前有效验证结果。
- `historical_data/README.md`：历史冻结数据说明。

## 开发与质量门禁

安装锁定开发依赖：

```bash
python -m pip install -r requirements-lock.txt
```

本地检查：

```bash
python -m compileall -q .
ruff check --select=E,F,W --ignore=E501,E402,E731,E741 .
python -m pytest -q
pyright quant_fusion_optimizer.py daily_signal_scan.py regime_adaptive.py \
  account_signal_engine.py market_data_contracts.py benchmark_validation.py \
  run_regime_validation.py
bandit -r quant_fusion.py quant_fusion_optimizer.py daily_signal_scan.py \
  regime_adaptive.py account_signal_engine.py market_data_contracts.py \
  benchmark_validation.py run_regime_validation.py validate_basket.py \
  download_eastmoney_qfq.py backtest_universes.py \
  backtest_cambricon_universe.py stress_test_prefixes.py -ll
pip-audit --strict -r requirements-lock.txt
```

CI 同时在 Python 3.11 和 3.12 运行完整测试，并重新计算 1、3、5、13、22 只股票的精确收益、最大回撤和交易次数。
