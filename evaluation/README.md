# Benchmark 运行入口

从源码运行 benchmark 时统一使用仓库 launcher。以下示例从仓库根目录调用；launcher 基于自身脚本位置定位仓库，因此使用其绝对路径时也可从任意工作目录调用。launcher 会先切换到仓库根目录，再运行对应 runner。

```bash
# LongMemEval-S
bash scripts/hlmem-python.sh evaluation/tools/run_longmemeval_benchmark.py \
  --dataset evaluation/longmemeval/longmemeval_s_cleaned.json \
  --output evaluation/results/longmemeval_s_benchmark.json

# LongMemEval-S 全上下文上限对照（不提取、不检索）
bash scripts/hlmem-python.sh evaluation/tools/run_longmemeval_benchmark.py \
  --mode full-context \
  --dataset C:/Users/Administrator/hl_mem_eval_data/evaluation/datasets/holdout50_mix_shard0.json \
  --config evaluation/tools/configs/longmemeval_deepseek_v4_flash.toml \
  --output evaluation/results/longmemeval_fullcontext_shard0.json

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

## LongMemEval 全上下文对照

`--mode full-context` 是独立的 retrieval-free control。runner 按 `occurred_at` 升序渲染该 case 的全部原始
session；时间相同则保持数据集来源顺序。每个 session 都带标准化时间戳，消息只保留原始 `role`/`content`，
不截断、不建立 case DB，也不初始化 extractor、embedder、reranker 或生产 recall。该模式仍使用相同的题型
reader 规则和官方兼容 judge：`deepseek-v4-flash-0731` reader 开 thinking，thinking budget 为 2048、正文预算为
512，单次 reader timeout 放宽为 300 秒；judge 关闭 thinking。

默认输出为 `evaluation/results/longmemeval_fullcontext_control.json`，分片输出必须继续使用
`longmemeval_fullcontext_*` 前缀，避免和 `hl_mem+reader` 主结果混淆。报告根节点标记
`control: full-context`，同时固定数据集 SHA-256、控制协议、模型与预算身份；`--resume` 会校验这些字段。
每题的 `retrieval` 仅记录 `selector=all-sessions`、session/message 数、gold session coverage、字符数和无截断
标记，并设置 `applicable=false`，所以 R@K/MRR 与 extraction coverage 不参与聚合。`qa.usage` 保存 reader/judge
各自的 input/output/reasoning/answer token，另记录 reader/judge 延迟和成本。成本仅对
`deepseek-v4-flash*` 按 2026-08-12 固定费率快照（输入 1 元/百万 token、输出 2 元/百万 token）估算；模型
override 没有固定费率时保留 token，但成本显式为不可计价，不能补猜。

工程冒烟只跑普通题和指定长题，不等同于全量分数。例如普通题可用 `--limit 1`；定位
`0a995998` 后用其所在 shard 的 `--offset`/`--limit 1` 单独运行。全量对照应逐 shard 串行运行，并为每个
shard 使用独立的 `longmemeval_fullcontext_*.json` 输出。

⚠️ 百炼内容审查仍作用于完整 payload。2026-08-12 工程冒烟中，`1d4e3b97` 的 45 sessions（509,176
context chars、112,962 reader input tokens）无截断完成；`0a995998` 的 44 sessions（521,079 context chars）
在进入 reader 前被端点以 `data_inspection_failed` 拒绝。后者不是上下文长度或 300 秒 timeout 失败，控制模式
也不会为追求分数而删除原文、拆题或加入 case 特判；全量报告必须将它保留为 provider failure。

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

### v0.25.0 holdout50 冻结基线

- 官方 50 题口径：**40/50（80%）**。配置为 `deepseek-v4-flash-0731`，所有 reader 调用开启 thinking，judge 关闭 thinking，reader evidence 固定 Top-10。
- temporal gate 诊断口径：40/48（83.3%）。它排除 2 道问题时点无有效答案的题，只用于误差分析，发布报告必须同时给出且优先报告官方 40/50。
- 内容审查隔离跳过 2 个输入 Event；这是结果解释所需的已知限制，不得静默省略。
- reader、excerpt 和 ordinal fallback 位于 `evaluation/tools/`，不是生产 `POST /v1/recall` 的回答生成层；benchmark Top-10 与生产可配置召回及 token packing 不可混为同一口径。

### 当前提取身份

- 当前 `PROMPT_HASH` 为 `86c522e45f92`，`LLM_EXTRACTOR_VERSION` 为 `llm-v2+86c522e45f92`；旧值 `fff10cabee53` 的提取缓存会按既有 manifest/fingerprint 规则拒绝复用。
- 中英文 prompt 都要求拆分复合事实、单独保留明确关系/动作与一次性事件，并逐项保留枚举数量和单位；只在原文明示时提取总数。
- 原始结构化响应恰好达到 20 条上限时记录 `extract/possible_under_extraction/claim_limit_reached`。这只是可能漏提取的审计信号，不会自动重试、扩容或补造 Claim。

### 排序可观测性

每个 case 的 retrieval 记录保存完整 `search_trace`；`retrieved` 中的每条候选同时保存 dense 通道原始分、
`reranker_raw_score`、各通道 rank/score、reranker 前后 rank、recall 最终 rank、reader 最终顺序和
`score_path`。归因时应优先使用这些原始字段，不要从最终混合分反推 dense 或 reranker 行为。旧报告缺少字段时
必须标为不可得，不能补算或猜测。

### 召回前轻量维护

标准 LongMemEval runner 在每个 case 的 fresh ingest 完成后，或打开 `--skip-ingest` 缓存数据库后、召回前，执行一次
`deterministic-dedup-conflicts-v1` 维护协议。它只调用有界、无 LLM 的 pending near-copy review 和生产
`auto_resolve_conflicts`；不会运行 TTL、decay、purge、派生记忆扫描或日任务。case 结果的 `maintenance` 字段保存
dedup 与 conflict 统计，run metadata 保存协议版本；协议不一致的 resume 报告会被拒绝。

`--skip-ingest` 因此会对缓存数据库做幂等维护写入，而不只是只读检索。embedding config-compare 不执行该步骤，
因为其候选 embedding 会被逐配置替换，复用生产 embedding 生成的 pair 会污染 A/B。当前 40/50 是该协议加入前的
冻结全量基线；在重新跑完 50 题前不得报告新的总体分数。

对于尚未形成 `dedup_pairs` 的跨 subject 近复述，生产 recall 仍会在现有 `recall.dedup_candidate_limit` 窗口内用
同一确定性安全门动态折叠；这不是全库扫描，也不写回 pair。`eeda8a6d` 的局部重放由这条兜底释放了一个 Top-10
位置并恢复为正确答案，但单例结果不得外推为新的 holdout50 分数。

## LongMemEval 提取分块与诊断协议

- benchmark 仍原样持久化 turn event；仅在调用 extractor 时，对超过字符预算的单 turn 生成临时 fragment。fragment 优先在段落、句子或词边界结束，无法容纳的纯非文本 envelope 保持单个无损 JSON，不静默丢字段。
- 临时 fragment 的 `fragment_index`、`total_fragments`、字段来源、前后续接标记和相邻 turn context 只放入本次提取上下文，不写回 event。每个可分 fragment 都是合法 JSON；即使 `content` 与 `text` 异值，也可分别按原顺序无损还原。
- 本次分块协议标识为 `semantic-turn-fragments-v1`，reader 协议标识为 `session-turn-window-v2`；ingest manifest 记录分块配置，resume identity 另记录 reader 协议。reader v2 不再向模型暴露 benchmark 实际摄入时钟生成的 `recorded_at` / `recorded_from` / `recorded_to`，仅保留数据集时间线中的 occurred/valid 时间。v1 resume 报告会因协议身份不同而被拒绝；存量 ingest DB 与 manifest 未改变，可继续配合 `--skip-ingest` 使用。分块协议与 43bb3ae 的 hard-split 结果不可混合，缺少当前分块身份的旧 ingest 缓存也需重新生成。
- `stored_claims_per_event` 使用 `claims` 表实际物理行数，不使用 `store_extracted()` 的 `status="stored"` 返回次数。0.82 仅是相邻复述诊断的词法阈值，不是生产 semantic/cosine dedup 阈值。
- `--skip-ingest` 会从缓存数据库重算物理 claim 指标；resume case 缺少这些字段时，读取阶段显式写入 `unavailable_legacy_resume` 和空值而不推测历史数值；若报告还缺少当前协议身份，后续 resume 校验会拒绝整份报告。
