# ChatGPT Project Brief

> 本文件只保存长期稳定、仓库级的信息。当前任务、临时分支、SHA、测试状态和执行进度应保存在当前 Pull Request 正文中。

## 1. Project

- 项目名称：Quant Fusion A股科技趋势决策系统
- GitHub 仓库：`ychenracing/trade`
- 默认分支：`main`
- 技术栈：Python、pandas、pytest、Ruff、Pyright
- 系统定位：面向 A 股 AI 硬件、光通信和半导体产业链的日线量化研究与人工决策支持项目。
- 执行边界：用于收盘后回测、组合验证、信号扫描和账户持仓审视；不连接券商、不自动下单，也不承诺未来收益。

## 2. Purpose and Non-Goals

Quant Fusion 提供冻结行情、趋势与弱市策略、组合回放、风险治理、参数研究、账户建议、严格 JSON 工件和持续集成门禁。信号在收盘后形成，最早在后续可交易日开盘执行。

长期非目标：

- 自动托管账户或自动下单；
- 将回测结果解释为未来收益保证；
- 使用分钟级模型重现盘中路径；
- 在行情、指数、映射或认证证据不可信时继续交易决策；
- 让研究层复制规范引擎的撮合、费用、交易规则或资金核算。

## 3. Architecture and Module Boundaries

仓库采用单进程模块化单体，规范实现位于 `quantfusion/`：

- `domain/`、`config/`、`data/`、`indicators/`：领域模型、配置事实源与画像构造、行情契约和无副作用指标。
- `strategy/`、`execution/`、`portfolio/`：信号、稳定撮合规则和组合状态边界。
- `risk/`、`regime/`：组合风控、治理证据、跨市场叠加和纯路由状态转换。
- `engine/`：单袖套、组合引擎和持续账户生产回放。
- `account/`、`application/`、`io/`：账户建议、流程编排、严格工件和原子状态发布。
- `research/`：候选配置、走步评价、晋级门和研究证据。
- 根目录：只放项目文档与配置，不提供 Python import 或 CLI surface。
- `scripts/`：只通过 `python -m scripts.<模块名>` 启动的可复现研究与验证命令。
- `tests/`：单元、契约、集成和经济回归测试。

依赖方向、无环导入、私有名称边界，以及根目录不得包含 Python 实现或入口，均由架构契约测试守卫。

## 4. Non-Negotiable Constraints

- 只处理日线数据，保持收盘后决策与后续可交易日开盘执行的因果边界。
- 默认对未映射股票、缺失或陈旧指数、不可解析数据和证据不足情况 fail-closed。
- 回测、日扫、优化和压力验证必须复用规范引擎，不得复制交易语义。
- 路由变化不得重建账户、清空挂单、重置峰值或丢失风险锁与冷却状态。
- 风险政策先产生不可变动作，再经执行适配器进入既有挂单队列。
- 压力诊断的 ID/family/shard 选择与正式计划严格隔离；只有精确 canonical 正式计划可发布，且计数语义只使用 `trade_records`。
- 日扫必须按验证输入、冻结证据、生产回放、验证结果、发布工件、发布状态、更新成功指针的顺序执行；前一步失败不得发布后续状态。
- 行情缓存目录必须通过显式上下文传递，避免并行或连续运行互相污染。
- 冻结行情、黄金指标和已审查验证工件不得与临时缓存、日扫输出或研究检查点混用。
- 参数、费用、映射、参考篮子或冻结数据变化必须重新通过适用的完整回归。
- 账户建议仅供人工决策，实际执行必须考虑流动性、涨跌停、交易规则和个人风险承受能力。

## 5. Authoritative Sources

- 项目定位、使用边界、命令和仓库结构：`README.md`
- 模块边界、依赖方向、状态所有权和扩展规则：`docs/ARCHITECTURE.md`
- 长期验证结论与经济基线：`docs/VALIDATION.md`
- 渐进式验证约定：`AGENTS.md`
- 规范实现：`quantfusion/`
- 配置事实源：`quantfusion/config/`；其中 `engine.py` 拥有默认值与校验，`profiles.py` 拥有行业分类、符号路由与参数画像构造。
- 测试与契约：`tests/`
- 冻结行情与数据说明：`data/`、`data/README.md`
- 持续集成：`.github/workflows/ci.yml`
- 依赖定义：`requirements.txt`、`requirements-dev.txt`、`requirements-lock*.txt`

## 6. Standard Commands

以下命令由 README 和 CI workflow 支持：

```bash
python -m pip install -r requirements-dev.txt
python -m compileall -q .
ruff check --select=E,F,W --ignore=E501,E402,E731,E741 .
python -m pytest -q
pyright quantfusion
bandit -r quantfusion scripts -ll
pip-audit --strict -r requirements-lock.txt
```

CI 使用锁定依赖，具体检查及其执行条件以 `.github/workflows/ci.yml` 为准。昂贵回测、基准和模拟遵循 `AGENTS.md` 的风险驱动渐进验证和适用验收合同，不机械套用固定层级。

## 7. Important Paths

- `quantfusion/`：规范模块化实现。
- `quantfusion/config/`：引擎、组合、风险、路由和股票池的公共配置事实源。
- `scripts/`：以模块方式执行的批量回测、数据下载和篮子验证工具。
- `quantfusion/application/stress.py`、`stress_scenarios.py`、`stress_metrics.py`、`stress_artifacts.py`：压力编排、场景选择、指标门禁和工件发布边界。
- `tests/unit/`：领域、引擎、状态机、风险、账户和研究单元测试。
- `tests/contract/`：架构、失败关闭、数据和仓库契约。
- `tests/integration/`：日扫与发布事务集成契约。
- `tests/regression/`：引擎、路由、叠加层和经济序列回归。
- `tests/fixtures/`：测试读取的黄金基线。
- `data/market/`、`data/regime/`：冻结行情与路由证据。
- `artifacts/validation/`：已审查验证工件。
- `examples/`：不含真实账户信息的输入样例。
- `docs/ARCHITECTURE.md`、`docs/VALIDATION.md`：架构与验证权威文档。

## 8. CI and Acceptance Entry Points

`.github/workflows/ci.yml` 在 Pull Request 上运行：

- Python 测试矩阵：安装对应锁文件、编译、Ruff 和 pytest。
- 规范包类型检查：`pyright quantfusion`。
- 安全与依赖检查：Bandit 和 pip-audit。
- 精确回测回归：在前置门通过后核验冻结股票池的整数指标、浮点指标和经济序列指纹。

验证范围、证据复用和完成标准遵循 `AGENTS.md` 与本次任务的适用验收合同；不因里程碑、换对话或交接机械扩测或重跑。工程检查、经济验收和合并条件分别报告，未运行或未核验项明确标记，不得当作通过。

## 9. Prohibited Actions

- 不得连接券商、自动下单或把建议表述为收益保证。
- 不得绕过失败关闭、严格映射、行情新鲜度、冻结数据哈希或因果执行边界。
- 不得让研究层复制信号、费用、涨跌停、成交量容量、T+1 或资金核算。
- 不得在路由切换时重置账户、持仓、挂单、峰值、风险锁或冷却状态。
- 不得绕过日扫的不可交换事务发布顺序。
- 不得把真实账户信息、凭据、密钥、缓存或临时运行工件提交到仓库。
- 不得在根目录重新增加 Python API/CLI，或让规范包依赖已删除的根模块名。
- 不得无依据修改黄金基线、冻结行情、验证工件或配置事实源。
- 不得擅自改写 Git 历史、force push、丢弃未知工作或覆盖无关改动。
- 不得根据旧聊天猜测当前分支、SHA、PR 或 CI 状态。

## 10. Context Loading Protocol

1. 新开发任务可以直接使用自然语言提出，不要求预先填写固定 Prompt。
2. 仓库任务先读取适用的 `AGENTS.md`，再按任务需要读取本文件和相关权威文档。
3. 实施前搜索匹配的开放 PR 和远端分支；仅在相关时读取 Issue。
4. 如果存在匹配工作，从现有现场原地继续。
5. 当前动态任务状态默认维护在 Pull Request 正文。
6. 不强制普通单 PR 任务创建 Issue。
7. 优先读取目标代码、直接调用者、相关测试和直接相关配置。
8. 只有证据不足、状态冲突或影响范围扩大时才扩大读取。
9. 不默认加载完整仓库、完整聊天、完整日志或全部 GitHub Actions 历史。
10. 长对话交接在可用时按需使用 `conversation-continuity-guard`；技能缺失不阻断任务，仍须保存可恢复状态并核验 GitHub 当前现场。

## 11. References

- `README.md`
- `AGENTS.md`
- `docs/ARCHITECTURE.md`
- `docs/VALIDATION.md`
- `quantfusion/`
- `scripts/`
- `tests/`
- `data/README.md`
- `.github/workflows/ci.yml`
- `requirements-dev.txt`
- `requirements-lock.txt`
