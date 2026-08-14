# Evaluation 结果索引

`evaluation/results/` 保存本地 benchmark 报告、日志和缓存派生产物，目录默认被 Git 忽略。本文件只定义
命名约定和已公开的发布口径；JSON、Markdown 报告、数据库、日志和模型响应不进入仓库。

| 实验族 | 建议命名 | 当前发布口径 |
|---|---|---|
| LongMemEval HL-Mem | `longmemeval_<run>_shard<N>.json` | v0.25.2：43/50（86.0%） |
| LongMemEval full-context | `longmemeval_fullcontext_shard<N>.json` | 46/50（92.0%） |
| LongMemEval native RAG | `longmemeval_nativerag_shard<N>.json` | 45/50（90.0%） |
| MemDaily | `memdaily_<run>.json` + `.md` | 180 case：97.2% accuracy |
| PerLTQA | `perltqa_<run>.json` + `.md` | 378 question：R@5 84.9%，MRR 69.6% |
| Embedding / extraction / misc | `<experiment>_<run>.json` | 只作为实验产物，不自动构成发布基线 |

运行日志放 `_logs/`，smoke、canary、局部重放和辅助产物放 `_archive/`。正式结果必须能通过 manifest 或报告
元数据追溯 dataset hash、代码版本、模型、配置和 scorer；不同提取缓存或不同 scorer 的数字不得直接横向比较。

当前运行方法见 [`evaluation/README.md`](../README.md)，中文三层评测口径见
[`tests/eval/README.md`](../../tests/eval/README.md)。
