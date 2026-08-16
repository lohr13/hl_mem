# Evaluation 结果索引

`evaluation/results/` 保存本地 benchmark 报告、日志和缓存派生产物，目录默认被 Git 忽略。本文件只定义
命名约定和已公开的发布口径；JSON、Markdown 报告、数据库、日志和模型响应不进入仓库。

| 实验族 | 建议命名 | 当前发布口径 |
|---|---|---|
| LongMemEval HL-Mem | `longmemeval_<run>_shard<N>.json` | v0.25.2：43/50（86.0%） |
| LongMemEval full-context | `longmemeval_fullcontext_shard<N>.json` | 46/50（92.0%） |
| LongMemEval native RAG | `longmemeval_nativerag_shard<N>.json` | 45/50（90.0%） |
| MemDaily | `memdaily_v0260_full.json` | v0.26.0（2026-08-15）：180 条全量计分，accuracy 97.2%，F1 0.9855，R@5 97.5% |
| PerLTQA | `perltqa_v0260_full.json` | v0.26.0（2026-08-15）：378 question，R@5 96.8%，MRR 82.8% |
| v0.27.1 行为变更验证 | 无新增全量 benchmark 产物（沿用 v0.26.0 数字口径） | resurrection：2 次正确复活、0 次误伤，p95 12.7ms；activation：identity 零误杀，confidence 语义分离 |
| v0.28.0 维护与关系语义 A/B | 无新增全量 benchmark 产物（继续沿用 v0.26/v0.27 公开基线） | canonical-slot：16/16 误配修复、0 回退；source-first：packet RAO 12%、entity@5 34.7% 与基线持平、可扩展边 0，终局不产品化 |
| v0.28.1 冲突热修 | 无新增全量 benchmark 产物（继续沿用 v0.28.0 发布口径） | `config.port` 边界与 reject 孤儿事务后置条件回归通过；本地存量孤儿清零 |
| Embedding / extraction / misc | `<experiment>_<run>.json` | 只作为实验产物，不自动构成发布基线 |

运行日志放 `_logs/`，smoke、canary、局部重放和辅助产物放 `_archive/`。正式结果必须能通过 manifest 或报告
元数据追溯 dataset hash、代码版本、模型、配置和 scorer；不同提取缓存或不同 scorer 的数字不得直接横向比较。

当前运行方法见 [`evaluation/README.md`](../README.md)，中文三层评测口径见
[`tests/eval/README.md`](../../tests/eval/README.md)。
