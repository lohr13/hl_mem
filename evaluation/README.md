# Benchmark 运行入口

从源码运行 benchmark 时统一使用仓库 launcher。以下示例从仓库根目录调用；launcher 基于自身脚本位置定位仓库，因此使用其绝对路径时也可从任意工作目录调用。launcher 会先切换到仓库根目录，再运行对应 runner。

```bash
# LongMemEval-S
bash scripts/hlmem-python.sh evaluation/tools/run_longmemeval_benchmark.py \
  --dataset evaluation/longmemeval/longmemeval_s_cleaned.json \
  --output evaluation/results/longmemeval_s_benchmark.json

# MemDaily
bash scripts/hlmem-python.sh evaluation/tools/run_memdaily_benchmark.py \
  --source D:/datasets/MemDaily/memdaily.json \
  --output evaluation/results/memdaily_benchmark.json

# PerLTQA
bash scripts/hlmem-python.sh evaluation/tools/run_perltqa_benchmark.py \
  --source D:/datasets/PerLTQA/perltmem.json \
  --qa-source D:/datasets/PerLTQA/perltqa.json \
  --output evaluation/results/perltqa_benchmark.json
```

Windows `cmd.exe` 使用相同参数，将命令前缀换成 `scripts\hlmem-python.cmd` 即可。先运行对应 runner 的 `--help` 可查看完整参数。

必须使用 launcher，是因为 Hermes gateway 等宿主可能把自身虚拟环境的 `site-packages` 注入 `PYTHONPATH`。直接启动 hl_mem 的 Python 会优先看到宿主包，甚至把 Python 3.11 的二进制扩展加载进 Python 3.13。launcher 会清除 `PYTHONPATH` 和 `PYTHONHOME`，并固定使用本仓库 `.venv/Scripts/python.exe`，避免跨环境污染。

## LongMemEval 429 / quota 恢复

全量评测应错峰运行，并保持单个 runner 进程；如果使用分片，不要同时启动多个真实 LLM/Embedding shard。遇到持续 429 时让默认熔断器停止运行，等待响应中的 `Retry-After` 或提供方配额窗口恢复后，再用完全相同的 dataset、config、output、offset、limit、QA 与 reader-context 参数追加 `--resume`：

```bash
bash scripts/hlmem-python.sh evaluation/tools/run_longmemeval_benchmark.py \
  --dataset evaluation/longmemeval/longmemeval_s_cleaned.json \
  --output evaluation/results/longmemeval_s_benchmark.json \
  --resume
```

runner 每完成一个 case 都会原子更新报告；resume 会校验数据集摘要、包版本、模型配置和运行切片身份。成功 case 与非 429 错误保持不动，`http_429`/`quota` case 会在恢复运行时自动重跑，因此不要改写中间报告，也不要在恢复时更换上述参数。可用 `--max-runtime-hours` 主动切成较短窗口；它和熔断退出都保留可恢复报告。

## LongMemEval 指标口径

- extraction coverage 的分母是所有成功 case，不依赖 claim relevance eligibility。
- claim R@K/MRR 只在 claim `eligible` case 上聚合；session R@K/MRR 使用独立的 `session_eligible` 分母。
- 报告中的 claim/session `*_eligible_numerator` 与 `*_eligible_denominator` 明确给出每个 K 的有效分子、分母；不要把两类 eligibility 混用。

## LongMemEval 提取分块与诊断协议

- benchmark 仍原样持久化 turn event；仅在调用 extractor 时，对超过字符预算的单 turn 生成临时 fragment。fragment 优先在段落、句子或词边界结束，无法容纳的纯非文本 envelope 保持单个无损 JSON，不静默丢字段。
- 临时 fragment 的 `fragment_index`、`total_fragments`、字段来源、前后续接标记和相邻 turn context 只放入本次提取上下文，不写回 event。每个可分 fragment 都是合法 JSON；即使 `content` 与 `text` 异值，也可分别按原顺序无损还原。
- 本次分块协议标识为 `semantic-turn-fragments-v1`，reader 协议标识为 `session-turn-window-v1`；manifest/resume identity 同时记录分块 target/overlap。它们与 43bb3ae 的 hard-split 结果不可混合，旧缓存或缺少协议身份的旧 resume 报告会被拒绝，需重新生成。
- `stored_claims_per_event` 使用 `claims` 表实际物理行数，不使用 `store_extracted()` 的 `status="stored"` 返回次数。0.82 仅是相邻复述诊断的词法阈值，不是生产 semantic/cosine dedup 阈值。
- `--skip-ingest` 会从缓存数据库重算物理 claim 指标；resume case 缺少这些字段时，读取阶段显式写入 `unavailable_legacy_resume` 和空值而不推测历史数值；若报告还缺少当前协议身份，后续 resume 校验会拒绝整份报告。
