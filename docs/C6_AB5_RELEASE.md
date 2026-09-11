# AB5：明确例外下的本次发布

本文件描述原 PR63 的发布路径，不改写历史冻结 P/I/R/D，也不将旧拒绝结果改成通过。
最新用户授权保存在 PR63 评论 5579535549，完整终止标记为
`TRADE_C6_WATCH_EXECUTION_CONTRACT_20260908_END`。

## 范围与风险披露

本次唯一候选为 `C6-Base+AB5`，原始经济实现为
`4659a2b6d265f45256777da6a2fc25d1369308bd`，已完成 Base 运行 `34509818018`。
用户一次性相对放宽收益、回撤和交易数验收限制 15%，并接受以下已知结果：

| 指标 | 放宽后标准 | 本次已接受的已知结果 |
| --- | ---: | ---: |
| 最大回撤绝对值 | 20.7% | 21.10621724651241% |
| prefix-05 终值财富保留率 | 84.15% | 72.3166270514689% |
| 其他 prefix 最低财富保留率 | 80.75% | 33.223093416649163% |
| 相邻 prefix 最坏终值财富变化 | -34.5% | -45.57191766929257% |

财富保留率是 `(1+候选收益)/(1+固定参考收益)`，不是净利润保留率或本金亏损比例；
相邻股票池终值变化也不是路径回撤。例外只覆盖本次 AB5、相同数据/窗口/参考的上述已知包络。
更差结果、其他候选和未豁免的完整性/排列不变性等失败仍然拒绝。
本次发布不再要求由用户恢复 main 保护；满足实际验收后允许普通 squash merge。
这些决策不保证未来收益或未来最大回撤。

## 两个不同的事实层

原 18% 等判据和 `QUALIFICATION_REJECTED` 原样保留。新发布工件保留原始指标、原始门结果，
另附带 `release_acceptance`，其中有独立 assessment ID、例外名称、适用阈值、源码绑定及 L2 来源。
不能只看原 `absolute_hard_gates.passed` 或单个 job 的退出码判断这次发布是否完成。

验收修订不会修改策略的 0.82 预算地板、风险触发、账户锁、成本、订单队列或下一交易日成交规则。
普通 Base 与 AB5 必须有不同的 checkpoint 身份，不能借用相同源码 SHA 混用结果。
当前 AB5 通过明确的候选/配置接入：`account_risk_budget_enabled=True`。
未指定 AB5 的旧研究命令不会被偷偷转换成 AB5；部署/日常使用入口是否选中 AB5 必须另外核验。

## 明确来源的执行路径

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

最后还需完成剩余现金/HWM/订单因果审计、完整测试、五池回归、实际使用入口检查、
最终 diff/HEAD 检查、合并及 main 核验。本文本身不是正式验收通过或已合并的证明。
