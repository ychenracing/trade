# AB5：明确例外下的本次发布

本文件描述原 PR63 的发布路径，不改写历史冻结 P/I/R/D，也不将旧拒绝结果改成通过。
最新用户授权保存在 PR63 评论 5579535549 与唯一 `C6_WATCH_HEARTBEAT`；原执行合同完整终止标记为
`TRADE_C6_WATCH_EXECUTION_CONTRACT_20260908_END`。

## 范围与风险披露

本次唯一候选为 `C6-Base+AB5`，原始经济实现为
`4659a2b6d265f45256777da6a2fc25d1369308bd`，已完成 Base 运行 `34509818018`。
用户一次性相对放宽收益、回撤和交易数验收限制 15%，并接受以下已知结果：

| 指标 | RELAX15 标准 | 本次已接受的已知结果 |
| --- | ---: | ---: |
| 最大回撤绝对值 | 20.7% | 21.10621724651241% |
| prefix-05 终值财富保留率 | 84.15% | 72.3166270514689% |
| 其他 prefix 最低财富保留率 | 80.75% | 33.223093416649163% |
| 相邻 prefix 最坏终值财富变化 | -34.5% | -45.57191766929257% |
| 随机场景 date/symbol/side bucket P90 | 184 | 185 |

财富保留率是 `(1+候选收益)/(1+固定参考收益)`，不是净利润保留率或本金亏损比例；
相邻股票池终值变化也不是路径回撤。随机场景 P90 的 RELAX15 上限仍然保留为 184；
`185` 通过单独的 `AB5_KNOWN_RANDOM_P90_185_ENVELOPE` 记录，只覆盖已认证的本次 AB5 结果，
并不把通用阈值继续放宽。`>185`、其他更差结果、其他候选和未豁免的完整性/排列不变性等失败仍然拒绝。

上述例外只覆盖本次 AB5、相同数据/窗口/固定参考/场景身份和已认证来源。
本次发布不再要求由用户恢复 main 保护；满足实际验收后允许普通 squash merge。
这些决策不保证未来收益或未来最大回撤。

## 两个不同的事实层

原 18% 等判据和 `QUALIFICATION_REJECTED` 原样保留。正式 958 运行也保留其原生 gate 结果；
新发布工件在这些原始事实之上附带 `release_acceptance`，其中有独立 assessment ID、例外名称、
适用阈值、源码绑定及 L2 来源。不能只看原 `absolute_hard_gates.passed` 或单个历史 job 的退出码判断本次发布是否完成。

验收修订不会修改策略的 0.82 预算地板、风险触发、账户锁、成本、订单队列或下一交易日成交规则。
普通 Base 与 AB5 必须有不同的 checkpoint 身份，不能借用相同源码 SHA 混用结果。
当前生产配置 `account_risk_budget_enabled` 默认为真，正式压力入口默认候选也是 `C6-Base+AB5`。
历史 Base/S 与消融入口显式关闭预算，不能因新默认值而悄悄改变旧身份的经济含义。
共同的 F0（账本身份）、F1（保留风险卖单抑制同批买入）、U（固定参考评分）
正确性修复属于本次 C6 的共享生产路径；因此未启用 AB5 预算也不代表与 pre-C6 经济序列相同。

## 原始经济运行的来源路径

以下记录已完成运行的原始入口，不是要求重新执行经济矩阵。当前基线已存在时，不得重复使用首基线动作；
读取仓库正式工件和回执即可恢复本次验收事实。

1. 使用 GitHub 原生 D 工件 `10189149456`（运行 `34572966199`），校验 ZIP 与内部 JSON 哈希。
   重判其中认证的完整 L1 判据，生成派生选择，不重跑 Base。
2. `python -m scripts.c6_ab5_release --selection-receipt <D.zip> --output-dir <独立输出目录> --source-revision <真实HEAD>`
   核验实际源码与固定参考/数据，复用既有 `_l2_evaluate` 运行固定 77 场景。
   原生 schema、实际 telemetry、判据重算及新的有限例外都通过才允许后继。
   `--preflight-only` 只执行来源/选择检查，不能当作 L2 已完成。
3. 使用同一真实 HEAD 执行规范正式入口：

```bash
python -m quantfusion.application.stress \
  --candidate-id C6-Base+AB5 --source-revision <真实HEAD> \
  --establish-initial-baseline \
  --initial-baseline-reference artifacts/validation/candidates/stress-86fd22448b9aad9d5e6194c0c065c40d56d7bddd-rejected.json \
  --ab5-release-acceptance --ab5-release-evidence <独立输出目录>/l2-evidence.json
```

正式入口保持完整 958 场景及对应 17 个 prefix，不用 L1 的 765 场景替代。
来源不匹配、缺少 L2、非 canonical 计划、非原固定参考或不同 incumbent 路由均不能发布。
真实新经济失败保留为 rejected；工程失败先修根因，仅重新验证受影响依赖。

现金/HWM/订单因果审计已完成，精确范围与来源见 `C6_AB5_RELEASE_AUDIT.md`。

## 已完成的 official17/958 证据

正式运行 `34638696991` 已完成全部 **958/958** 场景与 **17/17** 个 prefix，复用了已认证的 77 场景 L2，
没有重新执行 Base 或 L2。该运行的 GitHub Actions 工件为 `10284452515`，ZIP SHA-256 为
`4aa197fe25e627cb8f3877e0c6e3c61ef5fd4067ad2ddcd27ae7ad1f1e23f5a3`；其中原生正式结果绑定到源码
`56e5743ff8999540f07270ca0ddd0b5c664d8033`，原始 rejected payload SHA-256 为
`314cd7401948f700e630d4536e567f5e1f88b1b915130d130018691abdb1ab7e`。

该原始运行必须继续保留 rejected 身份：当时生效的发布验收只有 RELAX15 的 P90 上限 184，
实际观测为 185，因此运行正确地失败关闭。用户随后明确接受这 **1 个 bucket** 的差异；
当前验收修订 `C6_AB5_P90_185_EXCEPTION_20260912` 只允许从上述已认证原始结果派生新的发布判断，
不修改其原始 gate、结果、标签或生产者身份，也不重新执行 958 个经济场景。
当前 canonical 文件已在仓库 `artifacts/validation/` 落库，原始 rejected 文件另外按原字节保留。
`c6_release_receipt.json` 记录完整两份发布文件的规范哈希、assessment ID、原始工件哈希及零经济重算；
其原生评估输入恢复发布前的 `artifact_status=current` 后，SHA-256 与原生产者记录的
`fd1f13176a3ce471eb530a7104ead9c74cdcdb6127a1373e8b4bdc68fb1a7e5b` 完全相等。
新判定为 10/10，源绑定字段区分评估执行源码与原经济源码，没有把旧成交改称为新源码重算。
最终 exact-HEAD CI、PR63 合并与 main 核验以对应 GitHub 实际运行结果为准。

## 日常入口默认使用账户风险预算

规范 `ProductionReplayEngine` 已接受严格布尔配置 `account_risk_budget_enabled`，
正式 AB5 工作者也是把同一个配置传给该引擎，不使用第二套撮合或风控实现。
复用已验证本地日线数据进行人工决策支持时，可按下面的现有公开接口运行：

```python
from quantfusion.config.paths import MARKET_DATA_DIR, REGIME_DATA_DIR
from quantfusion.config.universe import SYMBOL_NAMES
from quantfusion.engine.replay import ProductionReplayEngine

engine = ProductionReplayEngine(2_000_000)
result = engine.run(
    dict(SYMBOL_NAMES), "2025-04-01", "2026-07-20",
    data_dir=str(MARKET_DATA_DIR),
    regime_data_dir=str(REGIME_DATA_DIR),
    leader_data_dir=str(MARKET_DATA_DIR),
    indicator_state="warm",
)
print(result["total_return"], result["max_drawdown"])
```

日期必须与实际合法数据匹配；这个示例不是行情下载器，不连接券商，
也不是正式验收通过本身的证明。普通回放不生成 canonical 工件。
实际账户快照的时点建议默认调用相同预算计划，使用已有必填 `peak_equity` 和实际合计持仓；
不复制回放的虚拟袖套，也不声称从当天快照重建了历史成交或锁状态。估值不完整时失败关闭。
单账户/强制弱市回放同样评估预算；三袖套仍只有一次合并账户预算。日扫输出实际
`account_risk_budget` 回执，缺失或未评估时不发布正常成功信号。

## 历史关闭预算回归基线的来源

原 main `0250163dbe1b234e96339f9059f9a2074f19cb06` 的黄金指标对应 pre-C6 实现，
不能要求授权后的 F0/F1/U 正确性修复继续逐笔复制旧路径，也不能直接用当前结果覆盖预期来隐藏新增漂移。
发布核对运行 `34631177857` 在同一锁定 OCI 中独立执行旧 main、冻结 I_B42 和当前发布源码的
五池回放及单股自适应回放：六个用例的当前指标、逐笔成交和事件指纹均与冻结 I_B42 完全一致；
旧 main 也复现其旧黄金指标。这证明差异在冻结实现中已存在，不是发布接入造成的新经济漂移。

当时 `tests/fixtures/backtest_golden_metrics.json` 的预期由独立冻结源码输出生成，
不是从当前 PR 的失败输出采纳；原文件完整保留在上述旧 main，原 SHA-256 为
`2590ff7a7649f102a9680c57575291ad7ce1f4f4c2fa31dfa0146f246baf004a`。
当时文件的 `_source_binding` 记录源版本、只读验证运行及工件哈希。
整数与事件指纹仍精确比较，浮点回归容差不变。

这组历史五池使用 `BacktestEngine` 的共享 C6 路径，未开启 AB5 预算；它是源集成回归，
不替代带预算的 77 场景 L2 或正式 958 场景验收，也不把旧利润门重新解释为已通过。

## 首次完整 L2：已定位并修复的聚合错误（2026-09-11）

运行 `34630144631` 在源码 `c8d46db65b4ae1e72884399cbcaeb7999f6c400b`、锁定 OCI 中
完成 77/77 场景。最初的失败是既有 `l2.initial.worst_add_one` 实现偏离冻结公式，不是新的经济拒绝。
I_B42 原始 P 的该判据明确写的是：
`minimum current add-one wealth change minus exact-reference minimum add-one wealth change`。
旧代码却计算 `min(current_i - reference_i)`，与合同的 `min(current_i) - min(reference_i)` 不同。

先用两场景交叉反例复现（旧实现 -0.6，合同正确值 +0.1），再修正聚合；真正退化仍被拒绝。
对已认证的 77 个实际结果，不重跑经济计算：当前最小加一财富变化 `-0.5108032476430526`，
固定参考最小值 `-0.7145312965039285`，合同正确差值为 `+0.20372804886087592`。
它通过原来的 -0.03 及放宽后的 -0.0345 下限，不需要新增豁免。修正后 9/9 L2 发布判据通过。
其余八项判据数值及全部订单、成交、净值、指标、场景、参考和误差容限均未改变。

原失败运行、原生 L2 evidence 和检查点保留。原始工件 `10276433256` ZIP SHA-256：
`1771990a779f3a228b1e401699bd2be95b55320e3a47fb59086171f9b5991d16`；
原始 L2 evidence SHA-256：`a1f8054b29b948ba6eecc4bee062e8cb7b24387934fa56f9ebf47fa5c7c3c57d`。
重判使用明确的派生身份，`derivation` 固定记录原经济源码、原运行、原文件哈希和零次经济重算；
消费者重算完整 77 记录的规范哈希并拒绝任何替换。新的 `execution_source_revision` 指评估执行源码，
不是声称这些成交由新源码重新算出。可通过已有命令的 `--reuse-l2-evidence <原始l2-evidence.json>`
复用这一个已认证的原始文件；不建立通用跨版本迁移，不对其他失败或其他候选自动豁免。

在该 L2 运行结束时，后继正式 17/958 尚未执行；此处仅描述当时状态。后续正式运行
`34638696991` 已如上完成全部 17/958，并以独立原始 rejected 工件保留其当时的发布判据事实。

## 发布与基线读取的一致性

发布之前，17 个 prefix 的完整记录映射必须与 universe 的对应投影逐条相等；独立排序不影响比较。
不一致在任何正式文件或拒绝工件写入之前报错。读取 AB5 incumbent 时，必须重算已存原生输入的
发布 assessment、校验候选/数据/参考/L2 来源上下文，并用固定参考重算完整 958 场景原生 gates。
缺失、被篡改、已拒绝或包含未豁免失败的 assessment 不能凭 `accepted/canonical` 标签进入比较。
这不会要求历史经济源码等于后来维护源码，也不会重跑回测或加载数百 MB 的原始 L2 账本。

传输真实性由已认证的原始 GitHub 工件哈希与仓库发布证据保障；读取器的内部一致性校验不是数字签名。
发布核验在锁定环境中独立比较原经济源码树与指纹、原生 958/17 输入、已认证 L2 回执及派生文件。
完整账本核对复用已完成的 12 分片审计，不重复经济计算。原生 gate 和历史 rejected 身份始终保留。

## 默认启用的回归边界

当前黄金预期以已发布源码 `314254fe04a5fc8bc0fd9bc91bb3a2c2ed5af796` 的相同 Git 树、
显式开启已有 AB5 的独立回放为对照，不从当前修改后的失败结果选取预期。五池与两个自适应窗口
保持原指标、整数和事件指纹比较精度；局部同环境对照不冒充新的正式 958 或锁定环境 CI。
单账户、强制弱市及真实账户快照的接入另有合成执行测试，不冒充原正式运行曾覆盖这些入口。
2024 年自适应窗口的已启用模型收益只有约 2.72%，相对旧关闭预算的约 49.68% 有明显机会成本；
默认启用只是兑现同一风险机制，并不保证所有行情下收益改善。历史正式工件保持原来源和数值。
