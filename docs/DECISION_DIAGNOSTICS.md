# 决策诊断与建议复核

`python -m scripts.decision_diagnostics --help` 提供离线、一次性诊断。它复用账户建议入口、生产评分、规范撮合及费用函数，不更新生产参数、股票池、风险状态或冻结行情，不连接券商，不启动定时任务。日扫输入覆盖要求见[数据说明](../data/README.md)。

## 评分与账户建议

```bash
python -m scripts.decision_diagnostics --task score --output ../trade-research/decisions
```

固定研究范围为现有 17 股池、2025-04-01 至 2026-07-20，从窗口首个交易日起每五个交易日取样。只适配本地行情获取，实际路由、触发、评分、目标数量、风险预算及覆盖检查仍执行生产函数。每个时点是明确标识的合成全现金账户，不是把今天持仓倒灌到过去，也不是连续账户回测。

`score.json` 保存输入与源码身份、各次扫描状态、原始候选、确认数组、分项分布、动量封顶比例、同日并列比例，以及实际有限槽位选择与代码顺序选择的差异。`advice/` 复用 `account_signals_日期.json` 格式；`evaluations/` 是对应建议的离线标签，不是另一份账户账本。没有候选、输入未就绪、身份未验证的日期保留在扫描记录中。

生产账户输出 `candidate_diagnostics`，包括分数分项、确认数、排名、槽位选择、指示性目标数量及约束理由。评分、确认数目标权重和 AB5 阈值没有因诊断改变。股票评分能否改变有限槽位选择，与其是否能提高账户净收益，是两个不同问题。

同日高低组按排序上下半组固定，奇数中位样本单列，并披露并列情况。标签固定为下一可交易日开盘至其后第 5 或第 20 个交易日开盘；退出指令在前一交易日收盘后已确定。日历休市、涨跌停、停牌或无成交量、缺失、尚未成熟、入场及退出被阻止分别列状态，不以零收益填补。成交量或交易日不足时不假定成功成交。

标准标签使用独立资金的 **100 股一手**、原生执行与费用规则，只比较信号表现。它并不执行建议的全部目标数量，也不证明大额订单具有相同容量。即使标签成熟，零目标数量仍是零建议；未入选候选的标签也不是账户曾经持有。收益统计按日期等权，同时报告不重叠日期样本及同日高低组配对差值，避免把高度重叠事件当作独立样本显著性。

这段历史已反复使用，不是从未见过的样本外。固定历史股票池仍有选股与幸存者偏差；分布更分散、封顶比例高或高分组偶然上涨，都不能单独授权修改策略。

## 保护收益、机会成本与股票池分歧

以下六条命令为一次性研究示例；不是建议反复运行、寻找最好成绩。先运行五个固定对照，再汇总：

```bash
python -m scripts.decision_diagnostics --task replay13 --output ../trade-research/decisions
python -m scripts.decision_diagnostics --task replay17 --output ../trade-research/decisions
python -m scripts.decision_diagnostics --task replay17-no-budget --output ../trade-research/decisions
python -m scripts.decision_diagnostics --task weak5 --output ../trade-research/decisions
python -m scripts.decision_diagnostics --task weak5-no-budget --output ../trade-research/decisions
python -m scripts.decision_diagnostics --task summarize --output ../trade-research/decisions
```

13 与 17 股使用既有验证股票前缀，在固定 2025-2026 窗口调用 `BacktestEngine`。弱市固定对照使用既有五股池、2024 全年及 `ProductionReplayEngine` 的连续账户。后一条路径保留每日路由和账户状态，不把独立窗口曲线拼接为连续收益。

`-no-budget` 仅是隔离研究调用的 AB5 反事实，既不关闭日常风控，也不作为部署候选。基线和反事实的源码、数据、成本、初始资金、股票池、窗口及其他配置相同；汇总核对身份与对应运行条件，禁止将不同输入结果拼接比较。

原始报告保存成交、订单、风险事件、权益曲线和规范事件指纹。`risk-pool.json` 给出净资产、收益、回撤、成交桶、费用、换手的同条件差值，以及预算约束区间、约束解除后首笔买入、尚未解除的右截尾区间。计划减仓订单数与真正成交的预算减仓分开计算。重复被削减的买入数量是重复意图，不是独立错过的持仓，更不能将其后上涨直接加总成可实现收益。

弱市状态中的 `hurst`、`vol_percentile` 可能含尚不可观察的 `NaN`。序列化只将这两个已知指标的缺失值记为 JSON `null`，并以 `regime_observation_gaps` 保留原始位置、日期、字段和 `NaN` 类型；不修改原生回放状态或经济结果。此时 `fingerprint_status=PARTIAL_MISSING_REGIME_OBSERVATIONS`，`regime_state_series_sha256=null`，不能把转换后序列的哈希冒充原生指纹；其余原生事件指纹保留。金额、价格、未识别字段的非有限值及无穷值仍导致严格序列化失败，不通过放宽 JSON 或删除记录隐藏异常。

预算约束的解除在当日收盘后才成为已知事实，因此恢复参与只从之后的交易日买入开始归因；同日开盘买入早于该收盘事实，不能算作“解除预算后恢复”。这只修正诊断因果边界，不改变 AB5 阈值、冷却或生产执行顺序。

扩池报告定位首个订单、成交和风险事件分歧，保留分歧前按实际净现金流重建的现金与持仓、当日事件、有效组合政策。已有按池大小生效的政策也可能不同；新增股票后的资金竞争、排序替换、风险路径和成本需要结合原始记录解释，不能预设单一归因，也不要求扩池收益单调增加。

## 复核已保存的账户建议

```bash
python -m scripts.decision_diagnostics --task evaluate \
  --advice ../trade-runtime/output/account_signals_2026-07-01.json \
  --market-dir ../trade-runtime/evaluation-market \
  --cutoff 2026-07-20 \
  --output ../trade-private/evaluation
```

此处路径与日期是命令格式示例，输入文件必须实际存在并具有所需历史及后续行情。不要向公共仓库上传真实账户快照、现金、持仓或成交文件。此命令本身不上传任何输入或输出。

评估核对账户代码、配置、日历及建议当时使用的行情前缀身份。原始快照、指数文件没有随评估输入提供时，明确保留“仅记录哈希、未重新核验原字节”的限制。代码、配置或历史前复权价格改变导致不匹配时输出 `IDENTITY_UNVERIFIED`，不默默用新版本为旧建议补发认证。应使用对应源码和原始冻结输入；不要篡改旧建议哈希来通过校验。

日期身份也必须闭合：`snapshot_date`、顶层 `requested_as_of`、`scan_dates.requested_as_of`、`required_evidence_date`、`next_trading_date` 必须与有限交易日日历一致，各 `market_evidence.evidence_date` 必须落在该请求应覆盖的交易日；任一不一致都输出 `IDENTITY_UNVERIFIED`，不能用较新的行情替旧建议补证。

买入标签只提供上述标准化信号模拟，不把人工照单执行设为默认。卖出建议复核下一开盘日、可卖数量与开盘跌停，标记为价格层复核而非虚构持仓成交；没有原始连续持仓及真实成交账本，人工实际成交保持 `UNKNOWN`。未成熟标签无需等待未来数周才能结束本次工程运行。

## 结果与晋级边界

这些工件为 `canonical=false` 的诊断证据，不替代正式经济验收。所有对照保留失败、缺失和无法解释的风险变化；不改变黄金值、冻结输入、seed 或历史接受例外。

工程测试证明记录、身份校验、实际账户选择和离线评估链可用，不证明长期策略增益或实际人工账户收益。缺少有效证据时保留生产评分、仓位与风险机制；研究结论和工程交付状态分别记录在匹配 PR，不在本文件维护动态任务状态。
