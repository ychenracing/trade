# Trade C6 续作 Prompt：工程修复可恢复，经济验收不放宽

请接管 GitHub 仓库 `ychenracing/trade`，从 PR #63 及其最新后继现场继续完成 C6。使用本 Prompt 即表示我授权下述修复、必要的重新冻结与重新运行、正常 commit/push、Actions 调度，以及满足验收条件后的合并。不需要再次询问“是否授权进入新周期”。

## 1. 目标、授权与旧合同的明确修订

目标：修复诊断生产者与消费者不一致及已暴露的运行恢复问题，恢复可信的 C6 验证，继续完成符合资格的 Base/S、选择、正式验收和可合并工作。

本 Prompt 是原 `trade_c6_causal_risk_closure_final_work_prompt.md` 的续作与执行条款修订。原 C6 的经济目标、风险限制和既有实现仍然是背景依据；本 Prompt 对以下执行冲突具有优先级。不能只在 PR 正文口头解释：应在下一次执行前，把这些修订写入实际生效的任务合同、绑定与必要校验实现。

| 旧限制 | 本次替代规则 |
| --- | --- |
| 原 §5/§8：运行开始后发生任何源码、schema、runner 或 provenance 修复，都终止整个任务并再次索要新研究授权 | 禁止改写旧冻结对象；允许自主诊断、修复、新建明确的修订版本，重新验证受影响阶段后继续。失效的是不可信结果或具体 execution revision，不是整项授权。 |
| 原 §8：工程问题导致旧周期失效后，必须另获授权才能重建 P/I_B/I_S/R | 本次已授权。经济假设不变的工程修订不算新的策略候选；保留历史、建立新身份与修复证据即可。 |
| 原 §8/§10：任何 full958 invocation 后绝不允许再跑 | 同一个稳定、有效候选完成一次有效全量验收；允许因已证明的工程/运行错误修复受影响工作。有效经济失败不能改名重跑择优；正常断点续跑不是新实验。 |
| 原 §8/§13：任意 main 漂移都让整项任务退出，且必须有 expected-base CAS 或 merge queue | 允许正常 merge 同步 main、按依赖分析重新验证；采用仓库实际保护规则和受保护合并能力，见 §8。不要求平台未提供的额外 CAS。 |
| 原 §10：查看结果后的代码调整一律属于重新研究 | 根据实际依赖区分格式/基础设施修复、既有经济合同的实现错误、策略假设变更，见 §4。源码 SHA 改变不等于经济假设改变，也不等于旧结果自然有效。 |
| 原 §13：超过 10 分钟不再等待后，实际续作依赖用户再次触发 | 单次运行不阻塞等待超过 10 分钟。事前建立并验证 GitHub 端续跑；工具有权限时自行调度和恢复，不反复要求用户点按钮。无可用后台执行能力时如实保存 pending 状态，不声称已自动完成。 |

这些修订扩大的是本任务内的工程恢复权限，并明确采用 GitHub 原生受保护合并保证，取代原 Prompt 自设的更强 expected-base 原子前提。它们不授权关闭保护、绕过 required checks、修改策略阈值、伪造 source SHA 或把失败结果改为 accepted。

必须保留：18% 正式 MDD 门、其现有 tolerance、收益保留与 promotion/initial gates、固定数据与场景的经济含义、Base/S 的既定公式和机械选择规则。保留 trade 的风险识别和 veto/reduce 定位；不新增资本 allocator，不删除或放宽账户锁，不按最高收益选择实现。

## 2. 先恢复现场，已完成工作不重做

先给出粗略时间估算、实际里程碑与最大不确定因素。时间估算区分本地工程工作和远端回测时长；不得承诺正式验收一定通过。

读取最新 main、PR #63、匹配后继 PR/分支、适用 AGENTS.md、相关冻结对象、最近失败与活动运行，再读取直接相关调用链。PR 正文历史段落不能代替当前 run/API 状态。不要扫描所有无关仓库或改动其他 Work 任务。

以下是生成此 Prompt 时核验到的历史定位信息；执行时重新核对，不硬编码为最新：

- PR：<https://github.com/ychenracing/trade/pull/63>，Draft/open，C6 v11 标记 INVALID，属于诊断格式错误，尚非经济拒绝。
- main/workflow：`faf45b9eb0d73fb4456266065d331e67eee15906`。
- P：`d3181f504de319daa9efa366e1f1faf727eab011`。
- I_B/PR head：`c76b09c8636a129150c20a7fddcee23a903445d0`。
- I_S：`a3f8a675082e98546e45882fae81880ddc25e517`。
- R：`81612f8b89c790562f0afc807576bbe691e27ea6`。
- 最后 run：`33961836500`；artifact：`9968597791`。
- 最后封存 wrapper 声称 530/3875 execution items；wrapper SHA-256：`b271c0a52e192c92e9be985d9b3b49a3d64feecff435a9a65af2a9a8eaffe891`。
- PR 记录：500 条曾完成独立前缀验证，最后 530 条仅完成 archive/wrapper/source/R 检查，不能直接称为 530 条有效经济结果。
- 原生 manifest 为 765 个场景：add_one 24、leave_one_out 17、permutation 150、prefix 17、random_subset 557。
- 3831 economic evaluations 与 controls/no-drift 等合计 3875 execution items。765、3831、3875、正式 958 是不同集合/计数，不得混用。

优先继续唯一匹配的开放 PR/分支。旧 P/I_B/I_S/R 和失败 artifact 永久保留；PR head 可以正常追加修复 commit，不需要为了保持旧 head 而阻止进展。若已有更新修复/后继，基于其真实 diff 和验证继续，避免第二套实现。

按仓库要求建立一个可恢复的远端 checkpoint；每个实质里程碑再 coherent commit/push 并回读 SHA。不得 reset/clean/rebase/force push、丢弃未知工作或在运行过程中改写被读取的冻结输入。

## 3. 必须完成的修复与事前联通验证

### 3.1 修复真实 producer，不放宽 consumer

已核验的直接根因：原生 `stress_scenarios._multi_seed_scenarios(...)` 提供 `symbols`，不提供 `symbol_count`；`c6_diagnostics.py` 用 `scenario.get(key)` 投影，导致完整记录的 `scenario_definition.symbol_count=null`，违反 P 的 integer schema。

从实际场景 `symbols` 列表派生计数。仅在既有合同要求的场景定义/输出边界修复，不擅自修改官方原生 manifest 的经济身份或 official L4 输出：

```python
definition["symbol_count"] = len(scenario["symbols"])
```

这只是核心修复示意，落地前须检查 symbols 的合法类型、ID 和唯一性约束。不要用 `len(set(...))`、排序或过滤掩盖重复/顺序问题；不要用 `0`、`int(None)`、nullable schema 或消费者默认值消除错误。若输入显式声明了不一致的计数，应按合同报错，不静默接受。

检查同一 metadata 投影在 metrics、L1、W、S、L2 以及消费者中的所有相关入口，复用一个必要的小 helper；不要建立通用 schema 平台。

### 3.2 让真实生产者输出经过真实消费者

大规模经济运行前，批量完成以下有信息价值的验证：

1. 由真实原生生成器生成全部冻结场景定义，运行实际 metadata 投影和实际 schema validator；无需回测即可覆盖全部 765 条。核对全部 required 字段、nullable 边界、类型及跨字段一致性，不只补 symbol_count。
2. 用原生生成的场景结构和纯合成行情，经过真实诊断 producer、序列化/压缩、checkpoint 封存、恢复读取与真实 full-record consumer。不能用手工预填 symbol_count 的 fixture 代替原生场景。合成输入使用明确的测试身份，不假冒正式 P 的数据 fingerprint。
3. 覆盖所有不同输出形状：Base/相关 ablations、相互依赖的 W 组、S 路径、L2；同形状无需逐经济场景重放。完整 W 组才能做组间公式验证。
4. 对 missing/null/bool/错误整数、symbols 不一致、字段缺失和已存在的关键公式校验做少量负例。测试既能接受真实合法输出，也能拒绝真实坏输出。
5. checkpoint 验证分两层：每条记录在进入“可复用的 completed 前缀”前通过实际逐记录类型/公式校验；跨记录/全组/完整集合约束在相应组或阶段完整后执行。不得要求不完整前缀提前通过全量 aggregate gate。
6. 首个实际生产记录和首个封存 checkpoint 必须经过真实 consumer 后才放行自动后继。失败时保留 forensic evidence，但不给无效前缀续跑。不要等几百条之后才第一次检查消费者。

检查 L4 official runner 的原生输出与现有 validator/外层 provenance 能联通，使用纯合成最小合法包或既有非经济集成测试即可；不为预检启动全量正式回测。保持 official artifact 原生格式，不为追踪信息改写官方经济结果。

### 3.3 修复自动续跑，并先验证真实触发链

PR #63 记录了 `.github/scripts/c6_auto_resume.py` 将 `run["name"]` 固定等于 `C6 Bound Economic Run`，而实际 run name 带有 logical-run/attempt。这是已核验的名称校验错配；它是否完整解释 workflow_run 缺失仍需沿事件调用链确认。

- 用仓库、workflow ID/path、可信 workflow revision、允许 ref、事件类型及已认证绑定识别运行。显示名称可展示，不作为唯一信任身份。保留防止旧实验/外部仓库被误调度的限制。
- 核对 workflow_run 触发条件、默认分支上的 workflow 可见性、权限、dispatch 参数、分支 allowlist 和重复 successor 防护。不要为解除名称限制而放开任意 run。
- 若 trusted orchestration 必须先在 main 修复，自动拆出最小非经济 PR，遵守保护与检查并合并，再冻结后续 workflow revision；不得夹带未验收策略。
- 用同一生产调度路径的非经济短 fixture 验证一次真实云端“封存→事件→认证→唯一后继→终止”，限定测试绑定/输出，避免触发旧 v11。若此 probe 暴露工程错误，修复受影响部分并继续，不重跑策略矩阵。
- 分段恢复保留同一有效 execution revision 的 logical run；同一 checkpoint 只能有一个后继，重复事件幂等，无 checkpoint 不伪造 resume。
- 调度单段时长用于满足平台限制和可靠 checkpoint，不机械地每 10 分钟砍一段；10 分钟限制是交互阻塞等待上限。允许远端单段继续运行，并按测得的恢复/资源成本合理选择分段。

## 4. 后续失败的处理规则：修复后继续，不反复要授权

每次问题先按实际读写依赖和行为影响分类，不按文件名、diff 行数或“只是 bug”标签分类。

| 类别 | 自主执行的动作 | 结果处理 |
| --- | --- | --- |
| A：元数据派生、序列化、类型/公式实现错配、checkpoint、调度、IO、CI、文档/provenance 等工程错误 | 修复根因，补最小回归/集成测试，新建修订身份，继续 | 审查这些字段是否参与场景选择、S 资格、D 选择、gate。若参与，重算全部受影响的下游判断；只有交易路径确实无关时才允许复用相应计算。 |
| B：实现偏离既已冻结的经济合同，如既定风险动作被丢弃 | 有合同、调用链和确定性反例支持时，授权最小 correctness 修复；记录实际经济路径会变化，修复后继续 | 不能称作零经济漂移；失效所有受影响计算与依赖结果，重新绑定并重跑相关阶段，最终完成稳定候选全量验收。 |
| C：实现正确，但真实有效结果未通过 MDD/retention/qualification 等经济条件 | 沿原 C6 已授权机械规则继续，例如 Base residual 存在时判定 S 资格；不根据结果调阈值或创造第三候选 | 所有既定可选路径耗尽后，记录产品验收拒绝。继续完成能独立验收的工程修复、文档与归因，不能强行晋升。新的策略假设才需要用户决定。 |
| D：工具权限、账单、required review、保护规则或平台硬限制 | 先检查已有授权与可用合规路径，完成其他不依赖工作，保存现场 | 确实需要用户动作时只报告具体阻塞；不能靠新增脚本伪造不存在的工具能力。 |

对 A/B，本次已经授权必要的工程修订与重新冻结，不再问“是否重新开始研究”。不设遇到第 N 个 bug 自动向用户交接的形式门；若重复失败，先集中排查共同边界，再修复受影响模块，不能无信息价值地反复全量运行。

观察过的经济结果继续记录为已见数据。工程修订不能把它们洗成独立样本外结果；也不能要求先产生新市场日期才允许把 symbol_count 修好并继续正式合同验证。保持假设/门槛不变时，既有场景可用于复现与合同验收，泛化结论另需真正未用于设计的数据。

## 5. 旧 530 条怎么处理

不默认全丢，也不默认全部复用。先读取真实 sealed records、源代码依赖与新消费者需求，做一次有界的复用判断。

1. 旧 v11 保持 INVALID。旧 archive、record hash、source SHA、P/R 和 run history 不改写。
2. 530 是 execution items，不是 530 个 unique scenario。复用前逐项检查完整前缀、ID/variant、input/data/config、原始 record hash、完备性和下游依赖；最后 30 条需要补验证，不能继承 500 条的验证结论。
3. 若证明错误仅在输出 metadata，而原始经济路径与必要 trace 完整，可用确定性的离线派生修复生成新 artifact。新 artifact 必须有新 hash，外部 provenance 明确链接旧 source/record 与修复代码，逐记录保持 orders/fills/equity/metrics 的实际值不变。不得伪称它由新 source 重新计算。
4. 新消费者与绑定必须明确接受这种带 lineage 的 derived evidence；如果现有源身份合同不接受、缺少完整 trace、字段实际影响过计算，或证明成本已高于重算成本，则直接按修复后稳定实现重算受影响项。不为了省 530 条开发通用跨版本迁移框架。
5. 校正 metadata 后重新运行相关 schema、公式、完整性和全部受影响下游选择判断。过去的 transport-valid 不等于 semantic-valid。
6. 新 execution revision 不得直接接着旧 INVALID logical run 写。用新绑定显式导入允许的已验证记录，或者干净重算；旧 logical run 永久终结。
7. W 组或其他相互依赖记录以既定完整依赖组验证/重建，不能拼接不同经济实现的互不相容记录。
8. 默认不混用旧记录充当新源码的正式 L4 证据。正式 runner 若要求精确 source，遵守原合同；在稳定最终候选上完成正式全量验收。

复用证明不足时，用户已授权必要重算，直接执行并说明原因，不把这个常规选择再次交给用户。

## 6. 重新绑定和渐进验证

先批量修复已知 producer/consumer/调度问题，完成 §3 的端到端预检，再构造新的 P/I_B/I_S/R 或仓库已经支持的等价显式修订身份。保留 Base 与 S 的独立、真实源码身份；看结果后不得把 S 偷塞入 Base。

修订记录只需复用现有任务/绑定载体，写清：旧/新 source 与受影响文件、根因、A/B 分类依据、经济合同是否改变、旧证据复用/失效范围、必须重跑阶段、数据与环境、实际命令和测试证据。不要再引入一套通用审批、签名、追踪或冻结框架。

执行顺序：

1. 工程修复与真实边界预检；修复阶段只运行受影响测试。
2. 稳定源码候选上完成一次完整且必要的非经济工程检查；Base/S 已有同输入同代码的有效证据按实际覆盖复用，必要时分别验证不同树。
3. 从有效新绑定恢复固定 Base L1。逐记录增量校验，完整阶段执行全部适用 diagnostic predicates。
4. 按已冻结规则计算全部 residual 和 S 资格；不能因可能由 breach→lock 导致的 Base retention 下降提前跳过合格 S。
5. S 合格才执行已冻结的 S L1；机械选择唯一候选并生成 D；无合格候选按类别 C 处理。
6. 完整通过 L2 后，在唯一稳定候选上执行官方 17/958，遵守当前正式精确 manifest/gates，不能用 765 或局部 shard 冒充正式结果。
7. 完成 required Actions、review 和合并条件后合并，不把 Draft PR 或绿色局部检查当完成。

工程修复后的验证以受影响范围为依据：可以仅重新生成元数据、重新消费已有结果或重跑依赖 shard；无法证明影响边界时扩大。生产策略/数据/核心运行环境实质变化后，旧经济验收不能复用。没有实质影响的文字、格式或结果文档不触发全矩阵重跑。

明确区分：进程成功封存一个 checkpoint、有效完整 L1、通过诊断条件、正式 accepted/canonical。exit 0 或 Actions 绿色不自动等于经济验收通过。

## 7. 10 分钟规则与连续执行

- 单个命令/Action 超过 10 分钟，不再阻塞轮询；记录 run URL、execution revision、attempt、有效 sealed checkpoint 与下一步。
- 已通过预检的 GitHub 端恢复链可以继续独立运行；不要为了等它持续占用 Work 会话或许诺没有落实的后台工作。
- 当前工具有 dispatch 权限时，自行填入从实际 workflow schema 读取的参数。不要复制旧参数名，也不要反复让用户手动触发。
- 自动链缺失但仍有可信 sealed checkpoint 时，验证旧 attempt 已终结、没有其他 successor，再走既有显式 dispatch。若调度代码有错，进入 A 类工程修复，不退出整个任务。
- 发现前缀内容无效时，停止其后继，不继续浪费分段。只对本任务明确无效/重复的运行做必要停止；保留证据，不触碰其他项目或其他 Work 会话。
- 必须等待外部 required condition 且没有其他可执行工作时，可以交付真实 pending 状态；这不是要求用户重新授权工程修复，也不是已完成合并。

## 8. 合并与 main 漂移：使用实际平台能力

在昂贵验收前，先核对 ruleset、required check/review、允许的 merge method、实际工具及 artifact provenance 合同。

本任务明确取消原 Prompt 自设的“没有 expected-base CAS 或 merge queue 就必停”的附加前提。若仓库仍采用 strict up-to-date required checks，使用 GitHub 受保护 PR 合并：服务端检查必须真实生效，merge 请求绑定工具支持的 expected head SHA，不使用 bypass/admin override。不能宣称这等同于 GitHub API 未提供的 expected-base CAS。

若 main 更新：

1. 重新读取 diff 与依赖，允许正常 merge main 到当前功能分支，不 rebase/force push。
2. 无经济影响的同步按实际依赖重新验证工程与 provenance，不机械重跑全量经济矩阵；source binding 若不支持等价映射，则使用真实新 source 并完成它确实要求的验证，不能伪造旧 SHA。
3. 经济代码、配置、数据、指标或核心 runtime 的实质变化，重新绑定并重跑受影响经济阶段；最终候选完成必要正式全量验收。
4. 普通冲突在已有目标内自主解决。只有冲突涉及不可兼容的产品选择且无法从合同判断时，才提出具体问题。

合并前验证真实 head/base、完整 diff、无遗漏工作、required checks/reviews/conversations、正式 source 与 artifact 关联。squash 后验证 main 中的实际改动与已验证候选对应；结果文档/provenance 追加不得掩盖策略或运行代码变化。不要修改业务 gate 来适配 merge；只修复能够被真实树/来源证据支持的 provenance 实现错误。

如果缺少必要的原生保护能力或权限，保留实际合并阻塞并完成其余工作；不能自动降低仓库保护。已明确无经济行为变化的调度/格式修复可按仓库现有独立发布合同拆成窄 PR 验收合并，不能夹带被拒的 Base/S。

## 9. 完成标准与停止边界

完成标准：

- symbol_count 与相关生产者/消费者边界已修复，真实原生场景和完整记录通过验证。
- 无效 checkpoint 不再自动续跑；有效 checkpoint 能经已验证的恢复链继续。
- 旧 evidence 保留、复用/重算范围真实；当前结果身份与代码、数据、环境一致。
- 已授权的 Base/S 机械流程得到有效结论；正式门通过且满足仓库条件的候选已合并，或真实经济拒绝及可独立发布的工程结果均已收口。
- 已完成 Actions 中的相关已知失败已修复；pending 与 passed 明确区分。
- 代码、测试、相关文档和 PR 状态一致，重要工作已 commit/push 并回读。

允许阻止经济晋升的情况：真实有效的经济门失败、无合格候选、权限/保护/required approval 的真实限制、输入无法恢复或可信性无法建立、用户明确停止。工程错误本身先触发修复与受影响验证，不自动终止授权。

最终报告只需列出：已修复问题、旧 530 条实际复用/重算数及依据、必要测试、当前 run/正式结果、PR/main SHA、已合并内容、真实剩余阻塞。不要承诺回测一定通过 18%，也不要再次把“需要修复后重新冻结”当作请用户说“继续”的理由。

## 10. 生成本 Prompt 的核验来源

- [PR #63：INVALID 原因、历史冻结对象、530/3875 终止记录及合并前提](https://github.com/ychenracing/trade/pull/63)。进度计数与终止状态来自 PR 的现场记录；本 Prompt 生成时未重新下载并独立重验全部 checkpoint 字节。
- [冻结诊断生产者](https://github.com/ychenracing/trade/blob/c76b09c8636a129150c20a7fddcee23a903445d0/quantfusion/application/c6_diagnostics.py)：已直接读取 scenario.get 投影实现。
- [原生场景生成器](https://github.com/ychenracing/trade/blob/c76b09c8636a129150c20a7fddcee23a903445d0/quantfusion/application/stress_scenarios.py)：已直接读取原生场景结构。
- [自动续跑脚本](https://github.com/ychenracing/trade/blob/c76b09c8636a129150c20a7fddcee23a903445d0/.github/scripts/c6_auto_resume.py)：已直接读取静态 run.name 判断。
- [GitHub protected branches 文档](https://docs.github.com/repositories/configuring-branches-and-merges-in-your-repository/defining-the-mergeability-of-pull-requests/about-protected-branches)：strict 模式要求分支与 base 同步；具体仓库生效规则和工具能力仍须执行时读取。

这是一份续作授权与实施说明，本次生成文件没有修改 GitHub 仓库、触发回测或宣称正式验收通过。
