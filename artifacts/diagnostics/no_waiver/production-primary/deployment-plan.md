# 验证与交付索引

当前实现已完成正式验证并合并至 main；系统功能与使用方法见 [README](../../../../README.md)，验证说明见 [VALIDATION](../../../../docs/VALIDATION.md)。

## 结果入口

- 完整 958 场景指标：`artifacts/validation/deployment-release/index.json`。
- 发布回执：`artifacts/validation/deployment-release-receipt.json`。
- 来源与验证记录：`deployment-verification.json`。
- 成本、跨窗口与原件资格：`acceptance-verification/deployment-qualification.json`。
- 交易统计：`acceptance-verification/workload-summary.json`。
- 原始回放与重建清单：`acceptance-verification/deployment-originals/` 和 `acceptance-verification/deployment-formal-originals/`。

上述路径保留测量来源。当前源码、运行入口和维护状态以 main 及其文档为准。
