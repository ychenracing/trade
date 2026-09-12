# Quant Fusion A股科技趋势决策系统

## 项目定位与使用边界

Quant Fusion 面向 A 股 AI 硬件、光通信和半导体产业链，提供日线研究、组合回放、收盘后扫描和真实账户人工决策支持。不连接券商、不自动下单、不托管账户，也不保证未来收益。全部 Python 实现位于 `quantfusion/`，工具以 `python -m scripts.<模块名>` 运行。

信号只使用收盘及以前可见的数据，模拟订单最早在后续可交易日开盘执行。跳空、连续跌停、流动性和人工执行偏差都可能使实际损失超过触发线。模拟持仓不是真实持仓；真实账户快照不注入历史回放。

已有 AB5 正式基线是在明确例外下接受的历史结果：最差回撤 **21.106217%**，随机日期/股票/方向桶 P90 为 **185**，部分股票池的财富保留和相邻扩展明显退化；不是原 18% 等全部标准通过。工程回归、有限例外下的正式接受和历史 rejected 结果不可混用，完整来源集中在[验证结果与证据](docs/VALIDATION.md#formal-stress-evidence)。

## 快速开始

先在仓库根目录、独立 Python 环境中安装运行依赖。工程测试环境及 Python 版本覆盖见验证说明；不要把本地安装运行依赖等同于锁定环境 CI。

```bash
python -m pip install -r requirements.txt
python -m quantfusion.application.backtest_cli --help
python -m quantfusion.application.daily_scan --help
```

以下多行命令使用 shell 的反斜杠续行；在不支持该续行形式的终端中合并成一行即可。所有 `YYYY-MM-DD` 都须先替换为已完成收盘、且输入确实覆盖的目标交易日，不要原样执行占位符。

### 冻结历史复现

以下是**历史示例**：五股、初始资金 200 万元、2025-04-01 至 2026-07-20、前复权冻结输入和预热指标。使用现行默认配置回放这段历史，不等于复现旧 pre-C6 配置或重新认证 AB5 正式验收。

```bash
python -m quantfusion.application.backtest_cli \
  --symbol 300308,300502,300394,688256,603986 \
  --start 2025-04-01 --end 2026-07-20 --capital 2000000 \
  --data-dir data/market --indicator-state warm \
  --save-dir ../trade-runtime/history --no-plot
```

输入由仓库冻结数据与配置提供，先按[数据说明](data/README.md)核对清单和哈希。该入口是 `BacktestEngine` 趋势回测，打印终端报告，并由现有报告保存函数写入 `--save-dir`；它没有 `--regime-data-dir` 参数，不生成当天的账户建议或模拟日扫 `signals` 工件。`--no-plot` 关闭绘图，不关闭回测或结果保存。

旧源码的历史结果必须同时匹配其源码、数据、配置、窗口和场景身份；仅使用同一日期并不能恢复旧结果。不要为追上旧表格改默认参数或覆盖黄金预期。

### 日常人工决策支持

运行数据放在仓库外的独立目录，例如 `../trade-runtime/`。股票缓存由现有行情提供方获取并维护；指数使用 `../trade-runtime/regime/000300.csv` 与 `000682.csv`，格式、单位、预热和刷新条件见数据说明。近期扫描会尝试自动刷新指数；较早历史目标不会联网补指数，需要使用者准备真实合规文件。没有数据服务、网络或合格文件时，不存在“强制继续即正常”的操作。

**不要省略 `--regime-data-dir`。** 当前默认值仍指向冻结 `data/regime`，其中两个指数的冻结清单截至 2026-07-28；近期刷新可能写入传入目录。当前日决策消费既有 `detect_regime` 校验：固定指数证据为 `unknown`（不完整、无效或超过既有新鲜度容忍）时返回现金防御，不沿用非现金旧路由。该校验仍使用 `MAX_EVIDENCE_STALENESS_DAYS=10` 的自然日容忍，不是目标交易日完整性证明。使用前仍须核对两个文件真实截止日、共同交易日覆盖和来源；防御结果不能当作当前正常买入依据。

根据目标选择下面**一种**模式。模拟模式从起点重建虚拟账本；账户模式分析真实同日快照，两者不是可互换的输出。

```bash
# 模拟日扫：固定研究起点，截止日替换为本次目标交易日
python -m quantfusion.application.daily_scan \
  --start-date 2026-07-01 --end-date YYYY-MM-DD \
  --cache-dir ../trade-runtime/cache \
  --regime-data-dir ../trade-runtime/regime \
  --output-dir ../trade-runtime/simulation

# 真实账户建议：先准备合格的同日账户快照
python -m quantfusion.application.daily_scan \
  --account ../trade-runtime/account.json --account-id main \
  --end-date YYYY-MM-DD --cache-dir ../trade-runtime/cache \
  --regime-data-dir ../trade-runtime/regime \
  --output-dir ../trade-runtime/account
```

模拟日扫默认起点为 `2026-07-01`、初始资金为 200 万元，来源为 `quantfusion/config/daily.py`；示例显式写出起点，不是每天自动滚动起点的建议。已有运行保持股票池、起点、资金与部署身份一致，不靠更换输出目录或重置状态解除原身份的限制。账户模式使用快照的现金与持仓，不使用模拟 `--start-date`、`--capital` 来补造历史。

先读取本次退出状态和终端提示，再核对本次机器 JSON 的日期、模式、账户或运行身份，以及限制字段，最后阅读中文报告。退出 0 只表示相应发布路径返回成功，**不表示可以买入、证据覆盖请求日、风险状态已获准重置或下一日必能成交**。已捕获的数据、结构和状态写入错误通常返回 1；未捕获异常不保证采用同一退出方式，错误 JSON 也不是所有失败都会生成。

## 日扫信号与账户建议

### 先阅读中文决策报告

现有命令无需增加报告参数。原有机器 JSON／状态发布后，终端显示中文阅读版，并在同一输出目录保存 `signals_<日期>.<来源哈希前12位>.md` 或 `account_signals_<日期>.<来源哈希前12位>.md`。先看市场与数据限制和逐股速览，再看每只股票的买卖依据、阻止原因、数量和最终处理。

三种策略用“突破前期高点”“短期与长期均价趋势”“价格波动通道”等描述解释，不把多个策略记录当作投票或合并订单。卖出建议和可卖数量分开，零数量候选不代表可买入，历史最大回撤也不是今天的仓位建议。

阅读版只解释生产者记录，不重新计算信号、风险或仓位。账户未输出某只股票建议时明确说明无法判断原因；未知原因保留原始依据，不编造“没有风险”。统一买入抑制与独立风险意见分别说明。补充的当日执行／近期风险事件来自本次回放内存并标明观察日期，不把旧事件说成今天仍生效；来源 JSON 哈希不认证这些补充事件。

报告记录来源 JSON 全字节 SHA-256，同日重跑用新来源哈希区分旧报告。它不是订单、新的授权或成功指针。原流程失败不会生成本次正常阅读版；派生报告保存失败不回滚已经发布的机器结果。核对本次 JSON 的完整哈希、日期和身份，不只看目录中是否有 Markdown 或同名旧文件。

### 模拟日扫：不传入账户

读取 `signals_<日期>.json`，`mode="simulation"`。回放前建立 `snapshots/<日期>/`；同日复用须通过 manifest 哈希边车、CSV 内容哈希和精确文件集合校验，不能覆盖或额外塞入证据文件。

| 字段或文件 | 如何读取 |
|---|---|
| `scan_date`、`status`、`run_id` | 核对本次请求日期与运行身份；正常机器结果为 `status="ok"`。结构校验或序列化失败可能另写 `.error.json`，但失败回执可能写不成。 |
| `deployment.current_decision` | 当前时点部署的 `name`、`boundary`、`reason`；`deployment.decision` 为回放返回的判断。不一致时看 `current_route_buy_suppression`，不存在顶层 `route` 字段。 |
| `signals` | 逐股 `code`、`name`、`signal`、`held_shares`、`strategies`、`industry`、`profile`；持股数来自模拟账本，不是带建议权重的真实账户候选列表。 |
| `pending_signals`、`blocked_signals` | 未被应用层抑制的模拟挂单与被抑制买入分别保存；核对 `direction`、`strategy_name`、`target_shares`、`reason`、`blocked`、`executable`。可执行标记不保证成交。 |
| `summary.buys_suppressed` | 结合 `risk_state_identity_mismatch`、`current_route_mismatch`、`warmup_not_ready` 判断本次为什么不允许新增买入。 |
| `warmup_health.warmup_status` | `NOT_READY` 抑制全部新增买入；`DEGRADED` 本身仅提示，不覆盖其他限制，也不证明数据完全适用。 |
| `risk_opinion`、`portfolio` | 前者是独立环境意见，后者记录模拟绩效和风险状态；两者都不能单独替代最终信号限制。 |
| `account_risk_budget` | 必须有实际评估的 AB5 回执，不能只凭启用开关为真判断成功。 |
| `deployment.requested_symbols`、`selected_symbols`、`unavailable_symbols` | 区分请求、入选与数据不可用集合；未入选不是缺失数据的同义词。 |
| `risk_state_saved`、`latest_success.json` | 前者披露状态保存，后者是模拟结果索引，不是账户建议索引或买入授权。索引中的文件、日期、`run_id` 必须与实际 JSON 对得上。 |

机器信号先写，随后保存连续性状态；状态保存失败时 JSON 可能已经存在，命令仍返回失败。状态标记回写和成功索引更新是尽力操作，不是多文件整体事务。身份不匹配时可以保留旧状态、发布禁止买入但仍保留卖出能力的结果；这与磁盘写入失败不是同一情况。详细顺序见[架构说明](docs/ARCHITECTURE.md)。

### 真实账户建议：传入账户

读取 `account_signals_<日期>.json`，`mode="account_decision_support"`，`as_of` 为请求日期。当前部署在 `deployment_decision` 的 `name`、`boundary`、`reason`。账户模式不写模拟 `snapshots/`、`risk_state.json`、`latest_success.json`，不输出模拟 `signals`、`pending_signals`、`warmup_health` 或 `risk_opinion`；这不表示其账户风险检查被关闭。

`actions` 包括 `HOLD`、`SELL`、`REDUCE_REVIEW`、`DATA_ERROR`、`BLOCKED`、`BUY_CANDIDATE`。持仓动作的 `shares` 是总股数，`sellable_shares`、`recommended_shares`、`blocked_shares` 与 `execution_status` 分别披露可卖、建议、受阻数量和执行限制。`BUY_CANDIDATE` 是时点筛选，不是生产袖套净订单；其 `shares=0`，收盘估算整手数量写在 `indicative_target_shares`，并标记 `INDICATIVE_REVIEW_ONLY`。趋势候选的 `strategies` 只列实际触发项，不编造未触发策略。

`estimated_equity` 和 `estimated_market_value` 只在持仓估值完整时给出。检查 `evidence_date`、`data_complete`、`valuation_complete`、`unavailable_symbols`、`buys_suppressed`、`buy_suppression_reasons` 及预算回执；防御结果成功保存也可能禁止全部新增买入。行情实际日期一致不必然等于请求日期，仍须核对最新交易日。

`account_snapshot_sha256` 是输入账户文件的精确字节哈希；工件不记录其文件内容或绝对路径。下一可交易日必须根据真实开盘价、现金、可卖数量、涨跌停及人工状态重新复核，报告不是可直接发给券商的订单。

## 账户输入与连续性

首次可将 [examples/account.json](examples/account.json) 另存为仓库外的账户文件，再填写真实事实；不要用样例覆盖已有快照。输入由用户维护，JSON 根字段只接受 `schema_version`、`account_id`、`snapshot_date`、`cash`、`peak_equity`、`positions`，未知字段和旧 schema 均拒绝。

| 输入 | 当前要求与责任 |
|---|---|
| `schema_version`、账户及日期 | 版本为整数 `3`；非空 `account_id` 必须匹配 `--account-id`，`snapshot_date` 必须与请求日期一致，均在行情请求前检查。日期用 `YYYY-MM-DD`。 |
| `cash`、`peak_equity` | 现金为非负有限数；峰值为正的有限数，不接受字符串、布尔值或非有限数。峰值指该真实账户一致估值口径的历史总权益高水位，不是初始本金、单股价格峰值或最近一段窗口最高值。用户依据真实账户历史维护。 |
| `positions` | 六位股票代码映射；每项必填正整数 `shares`、介于零与总股数之间的整数 `sellable_shares`、正有限 `avg_cost` 和不晚于快照日的 `entry_date`。可卖数量由真实账户事实提供，不能把总持仓自动视为全可卖。 |
| `highest_close` | 可选的正有限数，代表建仓以来同一价格口径的最高收盘价。程序结合建仓日之后可见行情、该值与当前收盘价取峰值，不使用建仓以前的高点。 |

账户预算使用现金与实际持仓估值，有效峰值取输入峰值与本次权益的较大值；新高只能提高预算计算峰值，不能降低旧峰值。输入校验不能认证用户提供的历史峰值是否真实，程序也不会把本次新高自动写回账户输入供次日使用。

建仓早于加载窗口且未提供 `highest_close` 时，输出 `PEAK_EVIDENCE_INCOMPLETE`，停用依赖完整持仓峰值的保护；成本止损、可用趋势退出等仍按其条件评价。这不是“没有风险”，也不能通过伪造建仓日消除提示。

账户快照是单次真实事实输入；模拟 `risk_state.json` 只用于跨日显示和身份连续性检查，不注入重新计算的历史回放；历史回放中的峰值、锁与挂单由该次输入重建；虚拟袖套是模拟内部账本。账户模式不恢复历史挂单、冷却、路由计数或券商账本，不能通过拷贝模拟状态得到真实账户连续性。

当前没有在这条账户输入链上核验充值、提现、复权重述或账户迁移的自动连续性调整流程。出现这些情况，应先核正现金流和一致估值依据，保全原快照与来源；不擅自降低 `peak_equity`、删除状态、改身份字段或编造日期来解除限制。

## 风险适用范围

下面列职责而不是建立第二份风险合同。具体阈值、公式和条件以当前配置、实现及适用冻结合同为准；不同层可以同时约束同一笔建议。

| 规则与入口 | 参考口径与触发来源 | 动作、恢复与执行限制 |
|---|---|---|
| 趋势单股策略保护：模拟策略子持仓 | 各策略成本、持仓峰值、均线与波动指标；来源为 `strategy/`、`config/engine.py` 和实际参数画像，不是统一账户 18% 线。 | 形成退出或保护信号；再次进入仍须策略条件、现金及全部风险门允许。不能套用弱市或叠加层的另一套止损数值。 |
| 弱市龙头：专用弱市账本 | 建仓成本与持仓价格峰值；22% 灾难止损、5 ATR 初始保护、80 个交易日时间条件和盈利后 3 ATR 吊灯来自 `config/regime.py`、`config/weak.py` 及 `strategy/weak.py`。 | 按真实触发原因退出；恢复受原因对应冷却、探仓与连续确认条件约束，不是统一等十天即可买回。 |
| 跨市场叠加保护：其拥有执行权的模拟账本 | 独立风险篮证据、单股成本／峰值和账户回撤联合判断；成本、吊灯、分层回吐与灾变保护适用条件不同，见 `config/overlay.py` 与 `risk/overlay/`。 | 风险升级限制加仓／新开仓并产生减仓动作；风险回落、趋势健康和冷却共同决定恢复。专用弱市／现金路径持有执行权时不重复执行风险动作。 |
| 袖套及合并账户回撤控制：模拟回放 | 周期风险峰值与终身峰值分开；趋势 `PortfolioPolicy`、弱市配置及合并账户按规模／参考篮完整度收紧的政策不同。 | 预警、确认、减仓或终态锁依各自政策；普通再武装与集中账户的较长再武装不是同一计数。路由切换不重置峰值、锁、挂单或冷却。 |
| AB5 账户风险预算：合并回放、单账户回放、真实账户建议 | 同一账户连续高水位，预算基准为 `0.82 × peak`，另计两日规划压力与退出成本；公式唯一来源为 `risk/account_budget.py`。这不是保证实盘最大亏损 18%。 | 计算允许总敞口、裁剪买入并计划减仓；每次使用当时真实权益重新评价余额，未成交卖出不能增加买入额度，其他风险锁仍有效。真实账户使用可卖数量，回放使用既有成交队列。 |
| 真实账户单股建议：真实持仓 | 快照成本、建仓后行情及可证实的持仓峰值；来源为 `application/account_scan.py`。 | 生成 `SELL`、`REDUCE_REVIEW` 等人工建议；峰值证据不足时披露并停用依赖该证据的保护，不伪造袖套或完整历史状态。 |
| 正式验收指标：固定历史场景，不是交易触发器 | 全场景最大回撤、财富、成交桶、排列和来源检查以适用合同为准；原 18% 目标与 AB5 有限历史例外分别保存。 | 决定证据能否按合同发布，不直接下单；通过例外接受不代表原阈值全满足，也不自动豁免未来候选。 |

ATR 是价格波动尺度，不是百分比。所有计划只使用当时已知信息；收盘触发与下一可交易日成交之间存在风险，实际亏损不是触发阈值的绝对上限。

## 当前策略结构

趋势引擎由唐奇安突破、双均线趋势和 ATR 通道组成，三类策略保留独立子持仓与审计轨迹。默认 `allocation_mode=ensemble`，资金在 fast、base、slow 三个独立袖套分配；`dynamic_sleeve_weights=True` 只在确认的内部状态变化后迁移未使用现金，不合并持仓、策略、挂单、峰值或冷却。卖出优先于买入，袖套内使用 `symbol_level_sell_veto`，跨袖套不作券商级净额或相反成交抵消，双边成交分别承担费用。

基础配置最大单股权重 60%、总仓位 100%、最多六只；实际额度还受行业画像、参考篮、市场状态、成交容量和账户预算的更严格限制。股票池扩展的固定参考、连续可执行意图确认、粘性候选与外围新标的准入由规范组合实现负责，不将简单的股票数量或当日排名当作充分买入依据。

内部状态机使用等权指数斜率、均线广度、ADX、Hurst 和波动率，标记 `TREND`、`TRANSITION`、`CHOPPY`。外层 `ProductionReplayEngine` 使用两个固定指数的 MA60/MA120 中期证据和连续确认，在趋势、转弱、弱市、恢复与现金路径间迁移，沿用同一持续模拟账户。已有趋势账本转弱时由叠加层承担风险执行；空仓进入弱市才启用正动量龙头账本。弱市成熟／新兴双通道及周期内名单冻结避免每日重排变成隐性轮动。

指数缺失、不可解析或某评价日证据不足时存在现金防御路径；当前日决策统一拒绝 `unknown` 指数证据，但两个指数同时陈旧且仍在既有容忍内时，仍须核对请求日覆盖，不能用“任何陈旧都自动现金”概括实际实现。股票缺失处理也按入口区分，见数据说明。

子行业参数收缩 `subindustry_shrinkage=0.5` 将允许细分的最大单股权重、ATR 倍数和风险预算向粗粒度父画像收缩；入场／出场周期、盈利保护、加仓与路由参数沿层级共享，不因为本次文档说明而改变。

## 风险治理观测层

`quantfusion.risk.governance` 从已有状态生成预热健康、独立风险意见、袖套共识、风险篮覆盖和风险事件校准等证据，计算本身不直接改写交易账本，`risk_opinion` 也不直接生成订单。但模拟日扫**另外消费** `warmup_health`：`NOT_READY` 抑制全部新增买入，`DEGRADED` 本身只提示。因此不能笼统称治理输出“完全不进入决策路径”。

预热统计逐股历史、风险篮实际可观察范围和指数状态；不能把新上市股票与成熟股票视为同等证据。独立意见描述等级、置信度、市场环境、建议敞口和原因，最终仍须看应用层限制与实际信号。共识和覆盖不足是观察证据，不自动等于买入或卖出。

`risk_event_calibration` 的 1/3/5/10/20 日结果与 L1 冻结机会成本是事后分析，不可回填为当时决策输入。随回放输出的字段包括 `warmup_health`、`risk_opinion`、`sleeve_agreement`、`risk_governance_series`、`risk_event_calibration`；历史零漂移对照只能证明其当时覆盖的输入与身份，不代表任何未来修改自动无行为影响。

## 默认策略参数

以下清单覆盖 `quantfusion.config.engine.default_engine_config()` 的全部顶层合法字段；具体默认值、类型与范围以该函数和 `validate_engine_config()` 为唯一事实源，不把参数在正文其他位置出现当作清单已完整。

策略参数：`entry_period`、`exit_period`、`adx_threshold`、`adx_period`、`atr_period`、`rsi_period`、`ma_short`、`ma_long`、`atr_multiplier`、`trail_atr_mult`、`channel_mult`、`channel_lower_mult`、`risk_pct`、`hard_stop`、`strategy_weight`、`max_symbol_weight`、`max_total_weight`、`max_units`、`max_drawdown`、`daily_loss_limit`、`account_risk_budget_enabled`、`sector_guard_enabled`、`sector_guard_min_symbols`、`sector_shock_return`、`sector_shock_breadth`、`sector_shock_ma`、`sector_shock_window`、`sector_shock_confirmations`、`sector_recovery_ma`、`sector_recovery_breadth`、`sector_recovery_confirmations`、`symbol_level_sell_veto`、`momentum_lookback`、`max_positions`、`group_min_slots`、`fusion_single_scale`、`fusion_double_scale`、`fusion_triple_scale`、`profit_lock_activation`、`profit_lock_giveback`、`reversal_break_giveback`、`reversal_exit_period`、`reversal_loss_cut`、`reversal_turtle_enabled`、`reversal_dual_ma_enabled`、`reversal_atr_channel_enabled`、`combined_group_weight_limits`、`liquidate_on_circuit_breaker`、`strict_unmapped`、`commission_rate`、`stamp_duty`、`slippage`、`min_commission`、`max_pending_buy_days`、`pyramid_add_atr`、`pyramid_risk_decay`、`atr_method`、`limit_price_epsilon`、`per_symbol_limit_pct`、`st_symbols`、`risk_free_rate`、`market_regime_enabled`、`regime_ewi_lookback`、`regime_breadth_ma_long`、`regime_adx_trend`、`regime_adx_choppy`、`regime_hurst_window`、`regime_hurst_trend`、`regime_hurst_choppy`、`regime_vol_lookback`、`regime_vol_extreme_pct`、`regime_ewi_slope_trend`、`regime_ewi_slope_choppy`、`regime_score_trend`、`regime_score_choppy`、`regime_choppy_confirmations`、`regime_trend_confirmations`、`regime_recovery_confirmations`、`regime_min_state_hold`、`regime_transition_scale`、`regime_transition_pyramid_scale`、`regime_transition_trim_confirmations`、`regime_trend_to_transition_confirmations`、`regime_choppy_exit_ratio`、`regime_transition_exit_ratio`、`enable_cm_overlay`、`cm_overlay_shock_trim`、`cm_independent_risk_basket`、`cm_trend_health_protection`、`cm_risk_continuous_confirm_days`、`cm_risk_level2_drawdown`、`cm_risk_level3_drawdown`、`cm_risk_severe_direct_return`、`dynamic_sleeve_weights`、`transition_fast_weight`、`transition_base_weight`、`transition_slow_weight`、`choppy_fast_weight`、`choppy_base_weight`、`choppy_slow_weight`、`adaptive_max_positions`、`transition_max_positions`、`choppy_max_positions`、`sticky_candidates`、`adaptive_sticky_candidates`、`sticky_min_score_gap`、`sticky_confirm_days`、`sticky_cycle_days`、`sticky_rotated_cooldown_days`、`concentrated_account_rearm_days`、`incomplete_reference_max_total_weight`、`established_expansion_min_score`、`subindustry_shrinkage`。

`account_risk_budget_enabled=True` 为当前默认；实际预算以回执为准，历史对照显式关闭时必须保留自己的证据身份。`strict_unmapped=True` 拒绝未映射标的。周期和确认数通常以交易观测或计数为单位，ATR 倍数是无量纲倍数，收益／回撤／权重为比例，金额按资金口径、数量按股；每一项仍须核对其校验与消费路径，不把名称带 `days` 的所有字段都推定为自然日。

研究代码通过现有引擎的 `cfg` 和合法的 `per_symbol_config` 覆盖；逐股允许字段以 `PER_SYMBOL_OVERRIDE_KEYS` 为准，未知字段拒绝。普通日扫没有一个任意注入全套策略 JSON 的参数，也不能把这些 Python 配置键直接当成 CLI 选项。日扫路径与起点来自 `config/daily.py`，公开 CLI 覆盖范围以 `--help` 和解析器为准。

## 组合策略参数

以下清单覆盖 `quantfusion.config.portfolio.PortfolioPolicy` 数据类的全部字段，继承字段也包含在内；默认值与校验以数据类为准。

组合参数：`allocation_mode`、`single_lookbacks`、`allocation_horizons`、`drawdown_alert`、`confirmed_drawdown`、`drawdown_confirmations`、`emergency_drawdown`、`adv_lookback`、`max_order_adv_ratio`、`candidate_lookbacks`、`candidate_horizons`、`rearm_trading_days`、`terminal_drawdown`、`concentration_drawdown_adjustment`、`candidate_reference_percentile`、`regime_symbols`、`market_regime_enabled`、`regime_ewi_lookback`、`regime_breadth_ma_long`、`regime_adx_trend`、`regime_adx_choppy`、`regime_hurst_window`、`regime_hurst_trend`、`regime_hurst_choppy`、`regime_vol_lookback`、`regime_vol_extreme_pct`、`regime_ewi_slope_trend`、`regime_ewi_slope_choppy`、`regime_score_trend`、`regime_score_choppy`、`regime_choppy_confirmations`、`regime_trend_confirmations`、`regime_recovery_confirmations`、`regime_min_state_hold`、`regime_transition_scale`、`regime_trend_to_transition_confirmations`、`regime_choppy_exit_ratio`、`regime_transition_exit_ratio`。

策略基础参数与组合政策不是同一层。趋势 `PortfolioPolicy` 的周期确认基准 23%、紧急基准 27%、终身峰值线 28%，还会按集中度收紧；弱市使用独立梯度，合并账户也有自己的政策。研究调用通过现有引擎 `policy` 参数传入已校验的 `PortfolioPolicy`，不是修改一处数值就重定义所有风险线。

## 交易与数据契约

模拟买入按 100 股手数处理，佣金率 0.025%、最低 5 元、卖出印花税 0.05%、单边滑点 0.1%；这些是模型配置，不承诺等同于用户券商实际收费。买入还受现金、单股与总权重、行业、持仓数和成交量容量约束；组合容量参考前 20 日平均成交量，默认参与比率 0.5%。

`total_trades`、`sell_trades` 统计实际 `TradeRecord`；`date_symbol_side_count`、`date_symbol_sell_side_count` 统计日期／股票／方向桶。后者不是券商订单数，不证明合单或净额。输入字段、单位、日期与缺失处理的准确适用范围见数据说明；不能用“所有数据均校验”代替对实际覆盖和来源的检查。

## 参数研究与股票池扩展

优化器通过现有 `ProductionReplayEngine` 评价，按 `risk`、`turnover`、`return` 隔离参数族；三目标 Pareto 选择和普通／压力 holdout 的门限见验证说明。不同研究身份不能共享一个未标注的结果。策略、费用、数据或映射改动按照 [AGENTS.md](AGENTS.md) 与适用合同决定验证范围，不因阅读本页启动经济任务。

新增股票须有行业映射与合适画像，默认 `strict_unmapped=True`，不以关闭映射校验作为日常修复。正式压力入口支持精确 ID、场景族、ID 文件和 shard 诊断；任何选择都不能成为正式发布结果。以下为研究命令模板，须先具备相应研究授权、核实输入并替换真实的 40 位源码 SHA；不是日常日扫步骤。

```bash
python -m quantfusion.application.optimizer --symbol 300308 --stage risk \
  --data-dir data/market --regime-data-dir data/regime

python -m quantfusion.application.stress --source-revision <verified-40-char-SHA> \
  --scenario-id add-one-05-688072 \
  --diagnostic-output artifacts/diagnostics/add-one-05-688072.json
```

完整正式计划、初始基线／晋级门、历史失败和有限例外的数值与来源只在验证说明及原始证据中维护。诊断写入独立 checkpoint 和显式 diagnostic 输出，不得搬入 canonical 路径冒充正式接受。仅在适用合同要求时运行未筛选的正式矩阵，不能把旧恢复合同当作重启旧任务的授权。

## 仓库结构与文档职责

| 入口 | 用途 |
|---|---|
| `quantfusion/` | 唯一规范实现；`config/` 是配置事实源，`engine/` 是模拟引擎，`application/` 是 CLI 与流程，`account/` 是账户输入与时点建议支持。 |
| `scripts/` | 批量验证、股票下载与研究工具；统一用模块方式启动，先看各自 `--help`。 |
| `tests/unit/`、`contract/`、`integration/`、`regression/` | 分别覆盖单元、契约、集成和经济回归；具体文件见架构与验证入口。 |
| `data/market/`、`data/regime/` | 只读冻结输入，不是日常更新目录。 |
| `tests/fixtures/`、`artifacts/validation/` | 黄金预期与已审查证据；保留来源，不因文档整理而重新封存。 |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | 模块、状态、因果及发布边界，不重复历史成绩表。 |
| [VALIDATION.md](docs/VALIDATION.md) | 证据分类、适用范围、完整结果及来源索引，不代替实时 PR/checks。 |
| [data/README.md](data/README.md) | 文件格式、单位、冻结完整性与运行数据准备。 |
| [AGENTS.md](AGENTS.md)、[项目 brief](.github/CHATGPT_PROJECT_BRIEF.md) | 工程流程与恢复导航；动态状态在匹配 PR 正文。 |

运行输出、缓存、优化结果和压力检查点不是正式证据的同义词。`daily_signals/`、`data_cache/`、`optimizer_output/` 等默认生成物不提交；真实账户文件也不入库。已审查工件的发布走既有校验与来源绑定，不能靠手工复制到 `artifacts/validation/` 获得接受身份。

## 故障排查

| 现象 | 实际含义与可查证据 | 安全处理 |
|---|---|---|
| 指数陈旧、缺失或路由为现金 | 核对独立指数 CSV 的真实截止日、`live_refresh_manifest.json`、路由边界与健康原因；旧路由名称不是当前日期证明。 | 补齐合法运行输入，保持冻结目录不变；输入不充分时停止使用当前买入判断，不伪造日期或放宽容忍。 |
| 股票获取失败／截止日不一致 | 模拟正常路径可中止；账户可能保存 `BLOCKED`／`DATA_ERROR` 防御结果。 | 检查提供方错误、实际日期、全部必要标的及缓存来源；不静默删掉缺失标的改变股票池。 |
| 预热未完成 | 模拟 `warmup_health=NOT_READY` 会抑制买入；`DEGRADED` 是降级提示。 | 准备真实预热数据，核对新上市边界；不把降级输出写成完整正常信号。 |
| 账户文件被拒绝／峰值证据不足 | 看输入错误或 `PEAK_EVIDENCE_INCOMPLETE`；快照版本、账户、日期、金额、可卖数量都需真实合格。 | 核正快照与建仓后峰值依据；不删除字段、降低峰值或伪造建仓日期解除限制。 |
| 风险状态身份不匹配 | 可禁止买入并保留旧状态，检查 `run_id`、配置／股票池身份和 `summary`。 | 查明是否误用了别的输出目录或配置；保全原状态，不能靠 `--reset-risk-state` 恢复原账户的买入权限。 |
| 状态或机器结果保存失败 | JSON 可能部分已发布；索引、状态标记和退出状态需联合核对，错误回执不保证存在。 | 检查磁盘与权限，保全已有文件；不把旧索引或仅有 JSON 视为本次完整成功。 |
| 目录内有旧报告或同日多份报告 | 日期相同也可能不同 `run_id`、账户输入或 JSON 哈希。 | 以本次机器来源和完整哈希核对，不自动取任意一份旧 Markdown。 |
| 中文报告保存失败 | 机器结果可能已经成功发布，派生报告失败不会回滚它。 | 先核对机器结果，处理输出目录问题；不因缺少中文副本就重复计算经济结果或改风险状态。 |
| 未映射股票／没有候选 | 映射错误与合格股票未触发信号不是同一情况，数据不充分也可能导致空结果。 | 先核对映射和证据，再区分正常等待与防御关闭；不默认把所有空列表都叫正常。 |

## 优势

规范引擎统一撮合与核算，模拟、研究和账户建议有明确边界；冻结输入、严格工件、风险回执与来源绑定使结果可追溯。现有中文报告解释已记录的支持与阻止原因，不另建一套决策。

## 缺点和已知限制

科技历史样本有幸存者偏差与事后关注偏差，前复权数据可能重述，日线模型无法证明盘中成交路径。AB5 历史接受包含明确的回撤、成交桶及财富保护例外；默认预算在历史弱市对照中有机会成本，不能宣称全部窗口收益改善。日常指数默认目录仍指向冻结输入；当前日决策拒绝 `unknown` 证据，但既有新鲜度容忍不是请求日完整性保证，仍需核对实际覆盖。

## 适用行情

用于研究有持续趋势、可交易性和充分历史证据的科技股票池，也可借现有弱市与风险机制分析防御场景。适用是研究范围，不是保证获利的市场预测。

## 不适用行情

不能作为分钟级或盘中套利系统、自动券商订单系统，也不应在证据不全、严重流动性不足或实际执行条件无法满足时直接照搬回测建议。

## 健壮性与灵活性

保留映射、输入、风险状态和正式发布校验；显式研究参数可调整，但普通日扫不提供任意旧配置兼容。完整参数可查不意味着任何组合都合理，配置与经济变化仍须按有效合同验证。

## 还能继续提升的方向

优先解决已证实影响日常安全使用的独立实现缺口，并用受影响范围的测试验证；后续策略研究需同时评价收益、回撤、稳定性、换手与成本，不围绕一个历史失败反复调参。本页不授权额外策略、配置、报告或 CI 改造。

## 开发验证

依赖与检查命令见 `requirements-dev.txt`、`.github/workflows/ci.yml` 和 AGENTS。验证范围由变更风险与有效合同决定，先验证相关节点；纯文档不主动运行五池或正式矩阵，正常产生的适用 required checks 和仓库保护仍须满足。Python 3.11 与 3.12 的覆盖并非完全相同，详情见验证说明。
