# trade 工程架构重构实施计划

> **执行要求：** 在当前隔离工作区按任务顺序实施；每个任务使用聚焦验证，最终生产候选树只集中执行一次完整工程与经济验证。

**目标：** 把现有大型脚本式系统重构为边界清晰的 `quantfusion` 模块化单体，同时保持策略、参数、撮合、交易序列、生产回放和工件事务语义不变。

**架构：** 根目录模块保留为兼容门面或 CLI，唯一实现源进入 `quantfusion/`。依赖方向固定为 CLI → application → engine/account/research → strategy/execution/portfolio/risk/regime → domain/config/data；canonical package 禁止反向导入根目录兼容模块。

**技术栈：** Python 3.11/3.12、pandas、numpy、pytest、Ruff、Pyright、Bandit、pip-audit。

## 全局约束

- 本轮经济行为变化为零，不修改策略阈值、默认参数、股票池、排序、浮点聚合顺序或执行优先级。
- `import quant_fusion as qf`、旧根 CLI、JSON schema、冻结数据目录和关键公开 API 必须继续可用。
- 移动而不复制实现；旧模块只转发 canonical implementation，不长期保留两套逻辑。
- 小批次只跑编译、Ruff 与受影响测试；最终候选树再运行完整测试、黄金池和 983 场景压力门。
- 若经济 fingerprint、交易数、袖套成交数或 route/risk action 序列漂移，先恢复语义等价再继续，不以“更优雅”为理由接受漂移。

---

### 任务 1：冻结兼容契约并建立架构红灯

**文件：**

- 新建：`tests/contract/test_architecture.py`
- 修改：`test_fail_closed_boundaries.py`
- 修改：`test_repository_hygiene.py`

**接口：**

- 输入：当前 `9bb136d` 的公开 import、根 CLI 与 Git 索引。
- 输出：canonical package、依赖方向、兼容门面、Markdown 清单和源文件规模的可执行契约。

- [ ] 写入架构测试，要求 `quantfusion` 存在、canonical 模块不导入 legacy root、层间无反向依赖、无跨包私有导入、无循环 import。
- [ ] 运行 `pytest -q tests/contract/test_architecture.py`，确认因 canonical package 尚不存在而按预期失败。
- [ ] 保留 pandas 3.0 坏值夹具兼容修正，并运行 `pytest -q test_fail_closed_boundaries.py::IndexParsingFailClosedTests`。
- [ ] 更新 Markdown 清单，使本计划和最终架构文档成为受控文档而非游离工件。
- [ ] 提交基线与契约，提交说明为 `test: freeze refactor contracts`。

### 任务 2：抽取 domain、config、data 与 indicators 叶子层

**文件：**

- 新建：`quantfusion/domain/models.py`
- 新建：`quantfusion/domain/rules.py`
- 新建：`quantfusion/config/engine.py`
- 新建：`quantfusion/config/portfolio.py`
- 新建：`quantfusion/data/providers.py`
- 新建：`quantfusion/data/contracts.py`
- 新建：`quantfusion/indicators/technical.py`
- 新建：各 package 的 `__init__.py`

**接口：**

- 输入：原 `quant_fusion.py` 的 helpers、dataclass、`DataFetcher`、`Indicators` 和默认配置；原 `market_data_contracts.py`。
- 输出：`floor_to_lot()`、`default_engine_config()`、`PortfolioPolicy`、`DataFetcher`、`Indicators` 及领域模型的 canonical API。

- [ ] 先写 public API 与异常行为测试，确认 import 在实现前失败。
- [ ] 按原源码顺序机械移动数值校验、A 股整手、涨跌停、日期解析和领域模型，不改变函数体。
- [ ] 移动 `DataFetcher` 与行情 contract，保持 provider 顺序、cache path、列顺序、异常类型和成交量单位不变。
- [ ] 移动 `Indicators`，保持 Wilder 重播、NaN 恢复和浮点计算顺序不变。
- [ ] 把默认 engine config 与 `PortfolioPolicy` 设为公共事实源，旧 `_CoreBacktestEngine._default_config()` 只 delegate。
- [ ] 运行 data/indicator/config 聚焦测试与 1/5/22 sentinel。
- [ ] 提交说明为 `refactor: extract domain config data indicators`。

### 任务 3：拆分 strategy、execution、portfolio、risk 与 engine

**文件：**

- 新建：`quantfusion/strategy/base.py`
- 新建：`quantfusion/strategy/trend.py`
- 新建：`quantfusion/execution/orders.py`
- 新建：`quantfusion/execution/matcher.py`
- 新建：`quantfusion/portfolio/state.py`
- 新建：`quantfusion/portfolio/allocation.py`
- 新建：`quantfusion/risk/portfolio.py`
- 新建：`quantfusion/engine/core.py`
- 新建：`quantfusion/engine/causal.py`
- 新建：`quantfusion/engine/ensemble.py`
- 新建：`quantfusion/engine/universe.py`
- 新建：`quantfusion/engine/results.py`
- 修改：`quant_fusion.py`

**接口：**

- 输入：叶子层 API 与原引擎类层次。
- 输出：唯一 `BacktestEngine` / `SleeveBacktestEngine` 实现和薄 `quant_fusion.py` 兼容门面。

- [ ] 写 import compatibility、signal/fill/account-order 指纹与 CLI 契约测试，确认旧单体仍不满足新结构。
- [ ] 移动 Base/Turtle/DualMA/ATR 策略类，使策略只依赖 domain、indicators、config。
- [ ] 把 pending、T+1、lot、涨跌停、ADV、费用与稳定执行顺序归入 execution；策略不直接管理账户总状态。
- [ ] 把现金、持仓、allocation、candidate 与集中度状态归入 portfolio；把 drawdown risk manager 归入 risk。
- [ ] 按依赖顺序拆出 core、causal、sleeve、ensemble、universe 与结果报告，避免循环 import。
- [ ] 将根 `quant_fusion.py` 收缩为 canonical API/CLI 转发，并让旧私有兼容符号在过渡期仍可访问。
- [ ] 运行 engine 聚焦测试、1/5/22 sentinel 和一次五黄金池回归。
- [ ] 提交说明为 `refactor: modularize strategies execution portfolio engine`。

### 任务 4：拆分 regime、overlay 与 ProductionReplay

**文件：**

- 新建：`quantfusion/regime/models.py`
- 新建：`quantfusion/regime/evidence.py`
- 新建：`quantfusion/regime/state_machine.py`
- 新建：`quantfusion/strategy/weak.py`
- 新建：`quantfusion/risk/overlay/models.py`
- 新建：`quantfusion/risk/overlay/evidence.py`
- 新建：`quantfusion/risk/overlay/policy.py`
- 新建：`quantfusion/risk/overlay/adapter.py`
- 新建：`quantfusion/engine/replay.py`
- 修改：`regime_adaptive.py`
- 修改：`cross_market_overlay.py`
- 修改：`risk_governance.py`

**接口：**

- 输入：engine、domain/config/data 与现有 route/overlay 状态。
- 输出：纯状态转换、弱市策略、`RiskAction` → adapter → pending、单一 `ProductionReplayEngine`。

- [ ] 写 route 日序列、状态不 reset、cooldown、risk level、pending queue 与 risk action 顺序 characterization tests。
- [ ] 移动 regime models/evidence/state machine，把文件读取移出状态转换函数。
- [ ] 把 leader scoring、probe/confirm、time/hard/chandelier stop 移到 `strategy/weak.py`。
- [ ] 先机械拆 overlay，再引入不可变 `RiskAction`；adapter 生成与旧逻辑完全相同的 `Signal` 并按原顺序写入 pending。
- [ ] 移动 `risk_governance` 到 canonical risk package，不改变纯函数和 schema。
- [ ] 将 `ProductionReplayEngine` 设为日扫、optimizer、stress 和 backtest 共用入口；根文件改为兼容门面。
- [ ] 运行 route/overlay/replay 高风险集成测试和 1/5/22 sentinel。
- [ ] 提交说明为 `refactor: separate regime overlay and production replay`。

### 任务 5：拆分 account、daily scan、IO 与 optimizer 应用层

**文件：**

- 新建：`quantfusion/account/models.py`
- 新建：`quantfusion/account/snapshot.py`
- 新建：`quantfusion/account/service.py`
- 新建：`quantfusion/data/snapshot.py`
- 新建：`quantfusion/io/state_store.py`
- 新建：`quantfusion/io/artifacts.py`
- 新建：`quantfusion/application/daily_scan.py`
- 新建：`quantfusion/application/optimizer.py`
- 修改：`account_signal_engine.py`
- 修改：`daily_signal_scan.py`
- 修改：`quant_fusion_optimizer.py`

**接口：**

- 输入：canonical replay、data source 与 account snapshot。
- 输出：结构化 `DailyScanService`、account service、artifact-first 事务和薄根 CLI。

- [ ] 写 account schema、目标股数、T+1、snapshot、artifact-first、失败路径和 CLI characterization tests。
- [ ] 移动 account models/snapshot/service，消除对 `quant_fusion._floor_to_lot` 的依赖。
- [ ] 移动 frozen snapshot 与 risk continuity store；保持严格 JSON、原子写、stale/missing fail-closed。
- [ ] 把日扫主流程封装为 service，根 CLI 只解析参数、调用 service、渲染结果并返回退出码。
- [ ] 为 data source 增加显式 `cache_dir` 上下文，日扫和 account 不再保存/恢复进程级 `_cache_dir`。
- [ ] 移动 optimizer，使其只调用 canonical ProductionReplay，不复制撮合或回测逻辑。
- [ ] 运行 account/daily/optimizer 聚焦测试与事务失败路径测试。
- [ ] 提交说明为 `refactor: modularize account daily scan and research apps`。

### 任务 6：收口测试布局、架构守卫、CI 与文档

**文件：**

- 新建：`docs/ARCHITECTURE.md`
- 修改：`.github/workflows/ci.yml`
- 修改：`README.md`
- 修改：`test_repository_hygiene.py`
- 重组：`tests/unit/`、`tests/integration/`、`tests/contract/`、`tests/economic/`、`tests/stress/`

**接口：**

- 输入：最终 canonical package。
- 输出：可执行依赖规则、全 package 类型检查、安全扫描和按行为组织的测试。

- [ ] 按领域行为拆分 100 KB 日扫测试，不按数字后缀机械切割。
- [ ] architecture test 检查 legacy 反向依赖、私有跨包 import、层级反向、循环 import 和唯一 replay/config 事实源。
- [ ] Pyright 改为覆盖 `quantfusion/`；Bandit 覆盖 canonical package 与根 CLI；Ruff/compileall 覆盖全仓库。
- [ ] README 只保留用途、运行、策略、风险、目录和验证入口；架构细节写入 `docs/ARCHITECTURE.md`。
- [ ] 运行完整 pytest 之前先跑 compileall、Ruff、Pyright 与 Bandit 聚焦门。
- [ ] 提交说明为 `docs: enforce modular architecture contracts`。

### 任务 7：最终验证、复审与 main 发布

**文件：**

- 验证：生产候选树全部受控文件。
- 发布：`ychenracing/trade` 的 `main` 分支。

**接口：**

- 输入：完成收口的唯一候选树。
- 输出：工程门、经济门与压力门证据，以及 fast-forward 的 GitHub main commit。

- [ ] 运行 `python -m compileall -q .` 与 `ruff check --select=E,F,W --ignore=E501,E402,E731,E741 .`。
- [ ] 运行 Python 3.12 完整 `pytest -q`，并由 GitHub Actions 的 3.11/3.12 matrix 复核。
- [ ] 运行 `pyright quantfusion/`、Bandit 与 `pip-audit --strict -r requirements-lock.txt`。
- [ ] 运行 1/3/5/13/22 黄金回归，核对 return、drawdown、total_trades、sleeve_fill_count、route 和 risk action。
- [ ] 运行 weak/regime、daily artifact、经济 fingerprint 与最终 983 场景 stress gate，读取工件并核对 scenario count/schema/gates。
- [ ] 对最终 diff 做一次独立代码审查；修复 Critical/Important 后只重跑受影响检查。
- [ ] 创建一个有意图的最终提交，通过 GitHub 连接器把 `main` 从 `9bb136d` fast-forward 到新提交；禁止 force update。
