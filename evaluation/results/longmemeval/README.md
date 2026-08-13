# LongMemEval 结果

正式结果以 `longmemeval_<方案>_shard<N>.json` 命名，`shard0` 至 `shard9` 共同组成 holdout50；同名日志位于 `_logs/`，历史、实验、smoke、canary 和调试产物位于 `_archive/`。

评测 JSON 和日志默认作为本地产物保存。本索引记录目录约定与已确认的发布汇总；`v025` 分片保留运行时版本字段，HL-Mem 标签采用 v0.25.2 发布口径。

代表结果（发布汇总口径）：

- `longmemeval_holdout50_v025_shard<N>.json`：HL-Mem v0.25.2，43/50（86.0%）。
- `longmemeval_fullcontext_shard<N>.json`：Full-Context 上限，46/50（92.0%）。
- `longmemeval_nativerag_shard<N>.json`：raw-session dense RAG，45/50（90.0%）。
