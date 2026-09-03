# HL-Mem 评测体系

`tests/eval/` 保存可公开、可确定性执行的评测契约；真实 PerLTQA、MemDaily、LongMemEval 数据和含个人信息的
gold 统一保存在 `~/hl_mem_eval_data/`，数据库、缓存和报告保存在 `var/eval/`，均不进入仓库。

## 三层结构

| 层次 | 规模 | 验证目标 |
|---|---:|---|
| 提取评测 v2 | 24 synthetic hard case + 40 dedup pair | 原子性、角色方向、专名、speaker、modality、稳定性 |
| 中文隔离检索 | PerLTQA 64 + MemDaily 48 | embedding + reranker + 生产 RecallService，不调用提取/QA |
| 中文 E2E | PerLTQA 28 + MemDaily 12 | 真实提取 → 召回 → reader → rubric-v2 |

前两层用于确定性定位；E2E 层包含 LLM 采样波动。完整 LongMemEval、MemDaily 和 PerLTQA runner 见
[`evaluation/README.md`](../../evaluation/README.md)。

## 提取评测 v2

`fixtures/extraction_v2_synthetic.json` 冻结 atomic fact、来源 Event、角色—动作—对象、精确专名集合、speaker、
canonical subject、禁止传播和 modality。预测 Claim 必须显式带 `source_event_indices`；专名只接受 Unicode NFC
等价。dedup pair 单独报告 false reuse，负例覆盖共享实体下的值、方向、关系和 modality 差异。

`fixtures/chinese_e2e_rubric_v2.json` 覆盖枚举完整性、简短语义答案和“推荐≠执行”。

```bash
bash scripts/hlmem-python.sh -m pytest tests/eval/test_extraction_v2.py -q
```

## 中文隔离检索 112 case

PerLTQA 四种 memory type 各 16 条；MemDaily 六种题型各 8 条。每个 persona/trajectory 使用独立 namespace，
语料通过固定 SHA-256 顺序构建临时 SQLite；真实 embedding 和 reranker 开启，query expansion 关闭，intent 由生产
路由自动判断。

```bash
bash scripts/hlmem-python.sh -m tests.eval.real_chinese_data
bash scripts/hlmem-python.sh -m pytest tests/eval/test_chinese_fts.py -m real_api -s -q
bash scripts/hlmem-python.sh -m pytest tests/eval/test_chinese_fts.py -m real_api -s -q --chinese-eval-suite depth
```

指标包括 Hit@1/5、MRR、gold evidence recall、完整证据命中、intent 和 answerability。no-answer 总体指标使用
hard/soft 并集，同时分列：

- `no_evidence`：无候选的 hard abstention，reader 不调用 QA。
- `low_confidence`：有候选的 soft abstention；observe 模式继续 QA，并随答案返回标签。

固定快照报告 schema 为 v3，不能和旧 schema v2 的 no-answer 数字直接比较。门禁常量以测试代码为准，更新时必须
同时提交可审计的同快照 A/B 证据。

## 中文 E2E 40 case

### Three isolated extraction arms

For each of the three extraction-provider runs, set a unique `HL_MEM_CHINESE_E2E_CACHE_ROOT`, a unique
`HL_MEM_CHINESE_E2E_REPORT`, and `HL_MEM_CHINESE_E2E_REFRESH=1`. Pin the shared Qwen reader with:

```text
HL_MEM_EVAL_QA_API_KEY
HL_MEM_EVAL_QA_BASE_URL=https://coding.dashscope.aliyuncs.com/v1
HL_MEM_EVAL_QA_MODEL=qwen3.7-plus
```

The dedicated QA key affects only the QA reader; it never changes the extraction Provider configured by TOML.

PerLTQA 的 4 个 persona 共 28 题，MemDaily 六类各 2 题共 12 题。默认复用经过 dataset、extractor、prompt、
admission、retention、embedding 和索引配置身份校验的 `var/eval/chinese_e2e_cache/`；QA、query embedding、
reranker 和 recall 每次仍真实执行。

```bash
bash scripts/hlmem-python.sh -m pytest tests/eval/test_chinese_e2e.py -m real_api -s -q

# 强制重新提取
HL_MEM_CHINESE_E2E_REFRESH=1 bash scripts/hlmem-python.sh \
  -m pytest tests/eval/test_chinese_e2e.py -m real_api -s -q
```

`deterministic-rubric-v2` 保留 official anchor 全量命中，只对人工审核过的开放描述题使用 rubric 间 OR、必要
概念组 AND、同义表达 OR。报告保存 `verdict_basis`、scorer 版本、rubric、R@5、MRR、QA accuracy/F1、
extraction coverage、answerability 和 token。

历史口径：v0.25.3 的 `90%` 是对 live anchor `77.5%` 做 rubric-v1 离线重评分的数字；v0.26 rubric-v2 对两份
既有 40-case 输出离线得到 87.5% 和 92.5%。它们说明 scorer 行为，不是 fresh live run 的确定性下界。

代码回归必须使用同一提取缓存、同一 scorer 和同一配置做版本 A/B。不同缓存或 fresh QA 采样的单轮绝对分数只做
质量观测，不得直接判定代码回归。

## 默认离线运行

```bash
bash scripts/hlmem-python.sh -m pytest tests/eval/ -q -m "not real_api"
```

不带 `real_api` marker 不会调用付费模型。

## 快照与绑定

```bash
bash scripts/hlmem-python.sh -m tests.eval.fixtures.build_snapshot \
  --source var/hl_mem.db \
  --target var/eval/recall-v2.db \
  --manifest tests/eval/datasets/recall_v2.manifest.json
```

快照使用 SQLite backup API；manifest 只保存 hash、migration、数量和状态计数。绑定使用稳定 memory ID，或在兼容
fixture 中使用同一 Claim 内全部命中的 `claim_keywords` / 分组 `claim_keyword_groups`；关键词仅用于绑定和诊断，
不能替代真实相关性 gold。
## Required public recall gate

`public/recall_core_v1.jsonl` is the tracked, synthetic, zero-network Core 1.0 regression corpus. Its manifest binds the
dataset and protocol hashes, and its baseline is reproducible with fake providers. It is release evidence for stable
retrieval behavior, not a claim about real-provider semantic quality.

```bash
bash scripts/hlmem-python.sh -m tests.eval.ci_gate
```

The command is mandatory in CI. Missing or modified dataset, manifest, protocol, or baseline artifacts fail loudly;
private evaluation data is never used as a fallback.
