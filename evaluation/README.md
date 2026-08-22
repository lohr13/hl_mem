# Benchmark 运行入口

`evaluation/` 保存可复现 benchmark runner、公开的确定性 smoke 数据和结果目录约定。真实数据、含个人信息的
gold、模型响应、数据库、缓存和运行报告不进入仓库，统一保存在 `~/hl_mem_eval_data/` 或 `var/eval/`。

从源码运行任何 Python benchmark 时都使用仓库 launcher：

```bash
bash scripts/hlmem-python.sh <runner> [args...]
```

Windows `cmd.exe` 可使用 `scripts\hlmem-python.cmd`。launcher 会定位仓库根目录、清理宿主注入的
`PYTHONPATH`/`PYTHONHOME`，并固定使用项目虚拟环境。

## 当前评测层次

| 层次 | 入口 | 数据 |
|---|---|---|
| 确定性 quality smoke | `scripts/run_quality_smoke.py` | tracked synthetic `datasets/smoke_v2.jsonl` |
| 提取评测 v2 | `tests/eval/test_extraction_v2.py` | tracked synthetic fixture；私有扩展放仓库外 |
| 中文隔离检索 112 case | `tests/eval/test_chinese_fts.py` | 仓库外 PerLTQA 64 + MemDaily 48 |
| 中文 E2E 40 case | `tests/eval/test_chinese_e2e.py` | 仓库外原始语料 + `var/eval/` 缓存 |
| 完整 benchmark | `evaluation/tools/run_*_benchmark.py` | 调用方提供的仓库外数据集 |

中文三层评测、hard/soft abstention 和 rubric-v2 的当前口径见
[`tests/eval/README.md`](../tests/eval/README.md)。

## LongMemEval

标准 HL-Mem、full-context 和 raw-session native RAG 共用同一 runner：

```bash
# HL-Mem structured-memory path
bash scripts/hlmem-python.sh evaluation/tools/run_longmemeval_benchmark.py \
  --dataset ~/hl_mem_eval_data/longmemeval/holdout.json \
  --output evaluation/results/longmemeval_run.json

# Retrieval-free upper bound
bash scripts/hlmem-python.sh evaluation/tools/run_longmemeval_benchmark.py \
  --mode full-context \
  --dataset ~/hl_mem_eval_data/longmemeval/holdout.json \
  --config evaluation/tools/configs/longmemeval_deepseek_v4_flash.toml \
  --output evaluation/results/longmemeval_full_context.json

# Raw-session dense RAG control
bash scripts/hlmem-python.sh evaluation/tools/run_longmemeval_benchmark.py \
  --mode native-rag \
  --dataset ~/hl_mem_eval_data/longmemeval/holdout.json \
  --config evaluation/tools/configs/longmemeval_deepseek_v4_flash.toml \
  --output evaluation/results/longmemeval_native_rag.json
```

runner 每个 case 后原子更新报告。配额或 429 中断后应使用完全相同的 dataset、config、切片、模式和输出路径
追加 `--resume`；manifest 会拒绝跨数据、模型、prompt、协议或配置复用。分片结果使用
`merge_longmemeval_results.py` 合并，已有答案可用 `rejudge_longmemeval_results.py` 重判。

## MemDaily 与 PerLTQA

```bash
bash scripts/hlmem-python.sh evaluation/tools/run_memdaily_benchmark.py \
  --source ~/hl_mem_eval_data/memdaily/memdaily.json \
  --output evaluation/results/memdaily_run.json

bash scripts/hlmem-python.sh evaluation/tools/run_perltqa_benchmark.py \
  --source ~/hl_mem_eval_data/perltqa/perltmem.json \
  --qa-source ~/hl_mem_eval_data/perltqa/perltqa.json \
  --output evaluation/results/perltqa_run.json
```

先运行 runner 的 `--help` 获取完整参数。不得把工程 smoke、局部重放或不同缓存的单轮结果当作正式基线。

## 结构化状态生命周期评分

状态坐标评分器只读 `claims` 的结构化状态列、`supersedes_id` / `superseded_by_id` 和
`evidence_links(relation='supersedes')`；`audit_log` 只计入诊断行数，不参与任何指标。Windows 单库基线：

```bat
scripts\hlmem-python.cmd -m hl_mem.evaluation.state_lifecycle ^
  --db var\hl_mem.db --namespace default ^
  --output evaluation\baselines\state_lifecycle.json
```

两库快照使用 `--before-db` / `--after-db`；同库记录时间区间使用
`--db`、`--before-at`、`--after-at`。所有数据库均通过 SQLite `mode=ro` 打开，不运行 migration。

## v0.30.0 状态候选实验

状态评测只提供离线、无轮次命名的投影协议，不接生产 ingest/recall。实验 runner 先调用
`make_projection_sample(bundle, raw_llm_json)`，再把显式 `projector` 与 `atomicity_policy` 传给
`project_response()`；checkpoint、重试、计费线和 JSONL 文件写入均由 repo 外 runner 管理。判分调用
`score_protocol()`，baseline 通过 `project_run()` 预投影后传入 `baseline_projection`，历史 arm 标签如需审计只放在
可选 report metadata，不进入算法分支。真实 supersede 边只通过只读数据库的 `claims.superseded_by_id` 和
`evidence_links` 消费。

`sample_id == bundle_id`，从而与 gold assertion id 对齐。真实来源行会把不可逆闭集 skeleton 作为明确标记的
“非事实证据”上下文写进模型可见输入，受控断言位于独立的“当前评测事件”段；原始事件文本不会进入语料。

真实来源结构必须从调用方显式指定的冻结快照采样，脚本不会读取 `hl_mem.toml` 或内置数据库路径：

```bat
scripts\hlmem-python.cmd evaluation\tools\sample_state_events.py ^
  --source-db <readonly-snapshot.db> --output <temp-redacted-seeds.jsonl> --limit 200
scripts\hlmem-python.cmd evaluation\tools\v0300_state_corpus_builder.py ^
  --seed-source <temp-redacted-seeds.jsonl> --output-dir evaluation\datasets
```

冻结文件为 dev corpus/gold 各一份、sealed corpus/gold 各一份和一个 manifest。开发调参与报告只读 dev；sealed
文件名固定含 `sealed`，交付后只允许判分器读取并输出聚合指标。日常完整性检查仅依据 manifest 对 sealed 文件做
字节级 SHA-256 校验，不输出记录内容。

## 结果与数据治理

- `evaluation/datasets/` 默认忽略，只显式跟踪公开 synthetic smoke 数据。
- `evaluation/results/` 默认忽略，只跟踪 [`results/README.md`](results/README.md) 的命名和发布口径索引。
- `evaluation/cache/`、数据库、日志和模型原始响应不跟踪。
- 正式报告必须记录数据摘要、代码版本、模型/配置身份、token、失败和适用指标；不可用指标应明确标为不适用。
- 发布基线见根 README 和结果索引；历史实验过程不作为当前运行规范。
