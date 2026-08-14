# Recall v2 离线评测

本目录实现 M6 的可复现召回评测。数据集保存人可读的关键词绑定，不保存会随重建而变化的 claim/event ID；运行时在指定 SQLite 快照中解析 ID。一个关键词组可以绑定多个同义事实，多段历史则用 `claim_keyword_groups` 合并绑定；任一关键词组完全无匹配时立即报错。

## 提取评测 v2 gold

`fixtures/extraction_v2_synthetic.json` 是可公开提交的全合成 hard-case 契约，冻结 atomic fact、来源 Event、角色—动作—对象、精确专名集合、speaker、canonical subject、禁止传播和 modality 标注，并包含平衡 dedup pair。预测 claim 必须由 runner 显式补充 `source_event_indices`；专名覆盖仅接受 Unicode NFC 等价。dedup 判分单独报告 false reuse，困难负例包含共享实体下的值、方向、关系和 modality 差异。加载器与确定性指标位于 `hl_mem.evaluation.extraction_v2`：

```powershell
bash scripts/hlmem-python.sh -m pytest tests/eval/test_extraction_v2.py -q
```

真实或含个人信息的语料不得写入本目录或 `var/`，统一放在仓库外的 `~/hl_mem_eval_data/`，使用相同 schema 并将 `data_classification` 标为 `private_external`。

## 运行

```powershell
uv run pytest tests/eval/ -v -m "not real_api"
uv run pytest tests/eval/ -v --eval-db var/eval/recall-v2.db --eval-report var/eval/recall-v2.json
uv run python -m tests.eval.runner --database var/eval/recall-v2.db --report var/eval/recall-v2.json
```

### 隔离中文真实 API 评测

中文评测不读取 `var/hl_mem.db`。先从上游真实数据生成固定分层样本。真实基准共
112 cases：PerLTQA 64 条，四种 memory type 各 16 条；MemDaily 48 条，六种题型
各 8 条。这个规模不是为了机械复刻旧 110 条，而是让总体比例在最差方差下的
95% 抽样误差约为 ±9 个百分点，同时强制每个关键 slice 都有独立门禁。

PerLTQA 每类包含 14 条 answerable 和 2 条 hard no-answer；每个 persona 使用独立
namespace，corpus 同时放入该 persona 四类记忆中每类最多 8 条干扰项。MemDaily
每类包含 7 条 answerable 和 1 条 hard no-answer，每个完整消息流使用独立
namespace。固定 SHA-256 排序保证重建确定性，并优先分散角色、source ref 和场景。

```powershell
bash scripts/hlmem-python.sh -m tests.eval.real_chinese_data
```

两个 suite 分别构建临时数据库，使用真实 embedding 和 reranker，但关闭 query
expansion；query intent 由生产路由自动判断。默认运行 PerLTQA breadth：

```powershell
bash scripts/hlmem-python.sh -m pytest tests/eval/test_chinese_fts.py -m real_api -s -q
bash scripts/hlmem-python.sh -m pytest tests/eval/test_chinese_fts.py -m real_api -s -q --chinese-eval-suite depth
```

每次运行只产生一次 corpus embedding batch、一次 query embedding batch，以及每个
case 一次 reranker 调用。case 使用稳定 `expected_memory_ids` 绑定 corpus；
除 Hit@K/MRR 外，还报告 gold evidence recall 和完整证据命中率，避免多步题只命中
任意一条消息就被算作完整成功。

### 覆盖与负例

PerLTQA answerable 中固定至少 12 条天然 preference query，分别来自
social_relationship、events、dialogues；MemDaily 固定 8 条，来自数据本身有偏好
问题的 simple、conditional、noisy。所有 query 都由生产 `route_recall_intent()`
自动路由，不写 intent override。

PerLTQA no-answer 复用真实 QA，但刻意不把其 `Reference Memory` 放进对应 persona
快照，并额外检查答案文本未出现在该 namespace；这模拟“认识此人，但该段证据未被
记住”。MemDaily no-answer 询问已知场景人物未记录的护照号，保留实体强重叠，避免
用完全域外问题制造虚假的高拒答率。

### 基线与门禁

2026-08-13 的真实 API 基线如下。`answerability` 单独设门禁，因为真实语料中高度
重叠的 profile 字段、event 与 dialogue 会触发生产逻辑的 top-2 `0.05` margin 判定，
即使 top candidate 已被 relevance 判为 relevant 且 gold 位于 Top 5。

No-answer 口径统一把 `no_evidence`（无候选的 hard abstention）与 `low_confidence`
（有候选但不得断言的 soft abstention）都计为拒答，同时分别报告两类的 precision、
recall 与 F1。固定快照 recall runner 的报告 schema 为 v3；旧 schema v2 baseline
不能与该口径直接比较。reader 收到任一拒答信号时直接返回“信息不足”，不继续调用 QA 模型。

| suite | cases | Hit@1 | Hit@5 | MRR | gold recall@5 | 完整证据 | answerability | no-answer | intent |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| PerLTQA breadth | 64 | 0.857 | 1.000 | 0.923 | 1.000 | 1.000 | 0.714 | 0.375 | 1.000 |
| MemDaily depth | 48 | 0.976 | 1.000 | 0.984 | 0.899 | 0.810 | 0.595 | 0.500 | 1.000 |

门禁按“基线下允许约 1–2 个 case 波动”设置，而不是要求全满分。PerLTQA 的
Hit@1/Hit@5/MRR 下限为 0.82/0.95/0.89，answerability/no-answer 为 0.68/0.25；
MemDaily 分别为 0.93/0.95/0.95 和 0.55/0.33。另设 gold recall、完整证据、每个
slice 的 Hit@1/Hit@5、preference Hit@1/Hit@5 门禁；intent 必须保持 1.0。

原有 12/24 条虚构评测不删除，但降级为兼容性回归，不计入真实质量基准。普通离线
测试校验其 schema、no-answer 和 intent 契约；以下真实 API 入口保留用于手工核查
历史行为和 CLI 兼容性：

```powershell
bash scripts/hlmem-python.sh -m pytest tests/eval/test_chinese_fts.py -m real_api -s -q --chinese-eval-suite legacy-smoke
bash scripts/hlmem-python.sh -m pytest tests/eval/test_chinese_fts.py -m real_api -s -q --chinese-eval-suite legacy-full
```

不带 `-m real_api` 时付费测试会被跳过。真实运行按项目 `.env` 配置
embedding/reranker；生成的 manifest 固定上游文件 SHA-256，源数据变化后必须重新生成。

### 中文提取→召回→QA 端到端门禁

`test_chinese_e2e.py` 是常规可执行的付费端到端层，不替代上面的 112-case 隔离检索层。
固定样本共 40 题：PerLTQA 28 题（4 个 persona，四类 memory 全覆盖）和 MemDaily
12 题（六种题型各 2 题）。PerLTQA 每个 persona 的 8 条目标/干扰源只提取一次并供
7 个 QA 共享；MemDaily 每条完整 message stream 单独提取。因此固定样本完整运行有 112 次
extractor 调用，而不是按 40 个问题重复提取全部上下文。

```powershell
bash scripts/hlmem-python.sh -m pytest tests/eval/test_chinese_e2e.py -m real_api -s -q
```

默认复用 `var/eval/chinese_e2e_cache/` 中经过完整身份校验的提取数据库；QA、embedding
query、reranker 和 recall 每次仍真实执行。数据哈希、固定样本、extractor 版本、模型、
chunk/admission/retention 或 embedding/index 配置变化都会使缓存失效。强制重新提取：

```powershell
HL_MEM_CHINESE_E2E_REFRESH=1 bash scripts/hlmem-python.sh -m pytest tests/eval/test_chinese_e2e.py -m real_api -s -q
```

报告写到 `var/eval/chinese_e2e_report.json`，包含逐题 extraction coverage、R@5、MRR、
QA accuracy/F1、缓存状态和本次 token 用量。PerLTQA QA accuracy 优先使用数据集官方
`Memory Anchors` 全锚点命中；只有 manifest schema v2 中显式配置 `accepted_rubrics` 的题目
才会回退到人工审查的确定性 rubric（rubric 间 OR、必要概念间 AND、概念表达间 OR）。报告
保存逐题 `verdict_basis`、`scorer_version` 和 rubric，overall QA accuracy 门禁为 0.90；字符 F1
仍单独报告。首次零错误完整基线为：

PerLTQA 查询统一固定在 `2026-08-14T00:00:00+00:00` 评测，避免缓存中的 TTL
随执行日期变化；该时间只影响查询可见性，不改变提取缓存身份。

| dataset | cases | QA accuracy | QA F1 | R@5 | MRR | extraction coverage |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| PerLTQA | 28 | 0.429 | 0.400 | 0.929 | 0.839 | 1.000 |
| MemDaily | 12 | 1.000 | 1.000 | 1.000 | 0.896 | 1.000 |

门禁按一侧 95% Wilson 下界和离散 case 容差设置：PerLTQA 下限依次为
0.30/0.28/0.78/0.65/0.75，MemDaily 为 0.75/0.75/0.75/0.70/0.75。任何执行错误
都会直接失败；最终结果必须恰好包含 manifest 固定的 28+12 个 case，不能以高分子集通过。
单个 category/qtype 不设易抖动的高分门槛，但每个 slice 的 extraction coverage 与 R@5
均不得低于 0.50，用于拦截整类能力塌缩。测试有 7200 秒专属 timeout；
不完整缓存没有 manifest，下次执行会只重提取该单元。

## 构建快照

```powershell
uv run python -m tests.eval.fixtures.build_snapshot --source var/hl_mem.db --target var/eval/recall-v2.db --manifest tests/eval/datasets/recall_v2.manifest.json
```

构建器通过 SQLite backup API 从只读源连接生成一致副本。manifest 只包含哈希、迁移版本、event/claim 数量及 claim 状态计数，不复制敏感原文。快照数据库属于本地测试资产，受仓库的 `*.db` 规则忽略。

## 标签规则

- `binding.claim_keywords`：必须全部出现在同一 claim 的 subject、predicate、value 或 qualifiers 中；所有匹配项都进入 relevant 集合。
- `binding.claim_keyword_groups`：多组 `claim_keywords`，适合旧值/新值分别位于不同 claim 的历史问题；每组至少命中一个 claim。
- `binding.evidence_keywords`：可选；在已绑定 claim 的 event 证据中筛选允许的 evidence ID。
- `expected_keywords` + `keyword_match`：校验返回文本，支持 `all` 或 `any`。
- `expected_type=empty`：不得配置 binding、confidence 或 expected keywords。
