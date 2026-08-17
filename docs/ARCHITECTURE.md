# Quant Fusion 模块化单体架构

## 架构目标

本仓库采用单进程、单代码库的模块化单体。重构只改变代码归属和依赖方向，不改变策略参数、信号、撮合、费用、组合核算、风险动作或生产逐日回放语义。

规范实现全部位于 `quantfusion/`。根目录的历史模块继续提供兼容导入或命令行入口，但不得成为规范包的依赖。生产回测、每日扫描、优化研究、股票池验证和压力验证都复用同一套引擎或 `ProductionReplayEngine`。

## 依赖方向

依赖只能从外向内流动：

```text
兼容入口与命令行
  -> 应用服务
  -> 引擎、账户、研究
  -> 策略、执行、组合、风险、状态路由
  -> 领域、配置、数据契约
```

`tests/contract/test_architecture.py` 自动执行以下守卫：

- 规范包不得导入根目录兼容模块；
- 各子包只能依赖声明的内层子包；
- 跨子包不得导入私有名称；
- 规范模块导入图不得出现循环；
- 根目录关键兼容文件必须保持为薄门面；
- 历史公共 API 必须继续解析到规范实现。

## 模块归属

| 子包 | 唯一主要职责 |
|---|---|
| `domain` | 稳定领域模型、A 股整手与涨跌停等共享规则 |
| `config` | 引擎、组合、风险叠加、状态路由与股票池配置事实源 |
| `data` | 行情契约、供应方、显式缓存目录与冻结证据快照 |
| `indicators` | 无副作用的技术指标计算 |
| `strategy` | 趋势策略和弱市策略，不读取文件或修改总账户 |
| `execution` | 信号生成、订单优先级、T+1 队列与成交流程 |
| `portfolio` | 跨袖套分配、买入授权、订单净额与组合级约束 |
| `risk` | 组合风控、治理观测和跨市场风险叠加层 |
| `regime` | 指数证据、状态模型和纯状态转换 |
| `engine` | 因果回测、袖套、组合运行和生产逐日回放 |
| `account` | 账户快照模型、校验、候选评分与目标仓位 |
| `io` | 严格 JSON 工件、风险状态与原子发布 |
| `research` | 候选、走步评估、推广门、搜索与研究工件 |
| `application` | 回测、日扫、账户、优化器和压力运行的用例编排 |

## 单一事实源

以下对象只能有一个规范实现：

- 引擎默认值由 `quantfusion.config.engine.default_engine_config()` 返回；
- 组合参数由 `quantfusion.config.portfolio.PortfolioPolicy` 定义；
- 行情读取由 `quantfusion.data.providers.DataFetcher` 执行；
- 生产路由回放由 `quantfusion.engine.replay.ProductionReplayEngine` 执行；
- 风险治理由 `quantfusion.risk.governance` 计算；
- 压力场景由 `quantfusion.application.stress` 生成和运行。

历史 API 只做转发。例如 `quant_fusion.BacktestEngine` 与规范引擎是同一个类对象，`BacktestEngine._default_config()` 委托给公共配置函数。

## 状态所有权

| 状态 | 所有者 | 不变量 |
|---|---|---|
| 现金、持仓、挂单和成交 | 引擎与袖套 | 路由切换时不得重建或重排 |
| 账户峰值、周期峰值和风险锁 | 风险管理器 | 路由切换时不得重置 |
| 粘性候选和轮换冷却 | 股票池选择层 | 证据中断按原规则清零 |
| 弱市名单、探针和再入场冷却 | 弱市策略与生产回放 | 弱市周期内保持名单所有权 |
| 外层路由确认计数 | 状态机与生产回放 | 只使用当日收盘及以前证据 |
| 风险叠加级别和灾变冷却 | 跨市场风险叠加层 | 专用弱市或现金路由拥有执行权时只观测 |
| 日扫连续风险状态 | `io.state_store` | 只有信号工件成功写入后才能推进 |

## 风险动作边界

风险策略不再直接拼装并写入袖套挂单。它先产生不可变的 `RiskAction`，并直接使用动作自带的优先级解冲突；组合引擎再调用执行适配器，转译为既有 `Signal` 并写入挂单队列。

```text
风险证据 -> 风险策略 -> RiskAction -> 执行适配器 -> pending 队列
```

适配前后必须保持动作顺序、股票、方向、股数、原因、优先级、冷却和风险级别完全一致。`tests/unit/test_overlay_modules.py` 固定了该边界。
跨日未成交的风险挂单保留显式 `risk_priority` 执行元数据，必须与当日新动作一起解冲突；不得从面向人的 `reason` 字符串反推优先级。

## 行情与缓存上下文

`DataFetcher` 不再保存进程级 `_cache_dir`。每次读取通过 `cache_dir` 显式传入，回测请求、生产回放、每日扫描、账户扫描和优化器各自传递自己的数据上下文。因此并行或嵌套运行不会互相污染缓存目录。

供应方失败切换顺序、缓存路径、成交量单位、OHLCV 校验、日期范围和异常类型保持原语义。冻结快照继续验证精确文件集合、清单哈希与每个证据文件哈希。

## 每日扫描事务

每日扫描由 `quantfusion.application.daily_scan` 编排，根 `daily_signal_scan.py` 只提供兼容命令行。成功发布顺序固定为：

```text
验证输入与冻结快照
  -> 运行生产回放
  -> 校验结果和严格 JSON
  -> 原子写入信号工件
  -> 写入连续风险状态
  -> 原子更新 latest_success
```

任一步失败都不得让后续状态领先于已发布工件。对应行为按快照、schema、信号服务、命令行和工件事务分布在 `tests/integration/`。

## 研究与压力验证

优化器只生成候选、组织走步窗口、比较 Pareto 指标和执行推广门。它通过 `quantfusion.research.replay_api` 调用规范生产回放，不复制撮合或账户逻辑。

压力运行器位于 `quantfusion.application.stress`。根 `stress_test_prefixes.py` 是兼容命令行。正式方案固定生成 983 个场景，并校验运行签名、检查点、场景数量、确定性、正式基线推广门与最终严格 JSON 工件。

## 兼容策略

下列根文件在当前阶段保留：

- `quant_fusion.py`：公共回测 API 与命令行；
- `regime_adaptive.py`：外层路由与生产回放兼容导入；
- `cross_market_overlay.py`：风险叠加层兼容导入；
- `risk_governance.py`：风险治理兼容导入；
- `account_signal_engine.py`：账户建议兼容导入；
- `market_data_contracts.py`：行情契约兼容导入；
- `daily_signal_scan.py`：每日扫描兼容命令行；
- `quant_fusion_optimizer.py`：优化器兼容命令行；
- `stress_test_prefixes.py`：压力验证兼容命令行。

新代码必须直接导入 `quantfusion`，不得新增对上述门面的内部依赖。

## 扩展指南

### 新增策略

把纯信号逻辑放在 `quantfusion.strategy`，默认参数放在 `quantfusion.config`，由引擎显式组合。新增单元测试和最小经济哨兵，不要修改日扫、研究工件或撮合实现。

### 新增行情供应方

只修改 `quantfusion.data.providers` 及其数据契约测试。必须声明成交量单位，并通过同一标准化、日期、OHLCV、新鲜度与缓存契约。

### 新增风险规则

证据和策略放在 `quantfusion.risk`。执行性规则必须输出 `RiskAction`，由适配器进入挂单；纯观测规则只能写入治理证据，不能修改账户。

### 新增命令行参数

参数解析留在兼容入口或应用 CLI 构建器，转换成显式应用请求。不得把 `argparse` 对象传入领域、策略或引擎内部。

## 规模与类型检查说明

普通业务模块超过约 1,000 行或函数超过约 120 行时必须审查职责。引擎默认值与严格校验已归属 `config.engine`，`engine.configuration` 仅保留行业画像、分类与兼容委托；执行流与跨袖套分配分别归属 `execution.flow` 和 `portfolio.allocation`。`application/daily_scan.py` 保留一条较长的显式事务编排，以保护工件优先顺序，数据快照、状态存储、信号提取和账户服务均已拆出。

规范包通过 `pyright quantfusion` 整体检查。引擎与风险叠加层的协作式 mixin 文件只关闭 `reportAttributeAccessIssue`，因为这些属性由最终组合类提供；参数、返回值、可选值、调用签名和其余规则继续检查。第三方 pandas 当前没有随项目锁定独立 stub，因此配置关闭库源码推断，避免把动态 DataFrame API 错判为具体联合类型。

## 验证门禁

最终候选树必须依次通过：

1. 全仓编译、Ruff、完整 pytest；
2. 规范包 Pyright；
3. Bandit 与锁定依赖审计；
4. 1、3、5、13、22 股冻结黄金经济回归；
5. 弱市路由、日扫工件、风险动作与账户契约；
6. 983 场景正式压力矩阵及推广门。

本轮重构的经济行为变更为：无。
