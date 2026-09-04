# P1 提取收缩修复 A/B v2 预注册协议

> 状态：装备就绪、尚未执行。协议版本：`p1-extraction-ab-v2`。付费实验只由 Hermes 执行。

## 1. 目标与单变量

本轮只回答 atomic prompt 已恢复后，hard cap 应取 24 还是 30。两臂共享同一 prompt、软数量目标 20、代码基线、模型、配置、数据、reader、scorer 和全量 fresh extraction；唯一实验变量是 `MAX_CLAIMS_PER_CHUNK`。

不启用 soft-split 或 delta-repair，不改变 cap 遥测、rubric scorer、`entity_query.py`、召回参数或 reader prompt。2026-09-04 的旧 Arm A/Arm B 产物因导入主仓代码而全部作废，不得进入本轮比较。

## 2. 冻结臂身份

共同代码基线是 atomic prompt commit `486b69fafe232066fd7443f1ba38551aee737a5f`。协议文件位于后续文档 commit，但两个执行 worktree 都从该代码基线建立。

| 臂 | 唯一差异 | `ORDINARY_CLAIM_TARGET` | `MAX_CLAIMS_PER_CHUNK` | 预期 `LLM_EXTRACTOR_VERSION` |
| --- | --- | ---: | ---: | --- |
| A | 基线原样 | 20 | 24 | **`llm-v2+8c5bc4bebe26`** |
| B | 仅把 `schema.py` 的 hard cap 从 24 改成 30 | 20 | 30 | **`llm-v2+28f9fcda9609`** |

Arm B 保持同一 Git HEAD，只允许以下一行工作树差异；不得顺带更新测试、prompt 或其他代码：

```diff
-MAX_CLAIMS_PER_CHUNK: Final[int] = 24
+MAX_CLAIMS_PER_CHUNK: Final[int] = 30
```

两个 extractor version 均由项目自身 `compute_prompt_hash()` 在全新 Python 进程中计算；hash 覆盖 system prompt、response schema、英文 prompt/postprocess 指纹及后处理规则，不是手写标签。

## 3. 固定数据与运行条件

- Chinese E2E manifest：`tests/eval/fixtures/chinese_e2e_sample.json`，SHA-256 `706389f1f45a4eb8056ec8565c915507199a86f75088797c12dc258de2981b87`。
- 样本：40 questions（28 PerLTQA + 12 MemDaily）、16 个独立 ingest unit（4 PerLTQA + 12 MemDaily）。
- 私有源 hash：PerLTQA memory `f83d99fcb4d8954614aefb2768b32597fa80fdabf08c7217900a64e377d4f1e9`；PerLTQA QA `e59536c160200ebe41385064c150406a44f7a08c23cd91f96953cbdf77a7a149`；MemDaily `1b3a7928eeaab2e1c56b6b6200586078aa1af17eafcb4b80379cf9752b383a8f`。
- 模型冻结为 2026-09-04 诊断所用组合：extractor/reader `qwen3.7-plus@coding`、embedding `qwen3.7-text-embedding`、reranker `qwen3-rerank`。两臂使用同一 provider endpoint 和结构化输出设置。
- Hermes 在调度前冻结一份 byte-identical TOML 配置并在运行单中登记其小写 SHA-256；两臂必须传同一个登记值，禁止在臂内从当前文件动态生成“预期值”。密钥只来自进程环境或同一 `.env`。
- 每臂使用独立且起始为空的 cache/report 目录，设置 `HL_MEM_CHINESE_E2E_REFRESH=1`。不得复用 9 月 2 日、旧 A/B、真复刻或另一臂的 DB/manifest。
- 每臂只运行一次完整 extraction + recall + reader。不得看单题结果后改 prompt、阈值、样本、配置或重跑并沿用 `v2` 名称。

## 4. 执行身份 hard gate

身份 gate 是付费调用前置硬门，不是事后诊断。Hermes 必须从 arm worktree 作为 CWD，调用绝对 Python 路径并显式设置 `PYTHONPATH=<arm>/src;<arm>`；禁止使用 `scripts/hlmem-python.sh`，也不得相信 editable venv 自己会指向当前 worktree。

每臂在启动 `tests/eval/test_chinese_e2e.py` 前传入：

- `HL_MEM_EVAL_EXPECTED_GIT_HEAD=486b69fafe232066fd7443f1ba38551aee737a5f`
- `HL_MEM_EVAL_EXPECTED_REPO_ROOT`：该臂 worktree 的已解析绝对路径
- `HL_MEM_EVAL_EXPECTED_EXTRACTOR_VERSION`：Arm A 为 `llm-v2+8c5bc4bebe26`，Arm B 为 `llm-v2+28f9fcda9609`
- `HL_MEM_EVAL_EXPECTED_CONFIG_SHA256`：运行单中预登记的同一配置 hash
- `LLM_API_KEY`：值必须以 `sk-sp-` 开头；日志和报告只允许出现 `llm_api_key_prefix=true/false`，不得输出密钥或其片段

`evaluation/tools/run_identity_gate.py` 必须在首次付费调用前同时确认：Git HEAD、`hl_mem.__file__` 位于该臂 `src`、`tests.eval.chinese_e2e.__file__` 位于该臂 `tests`、extractor version、配置 SHA-256 和 API key 前缀。任一布尔值为 false 或任一预期环境变量缺失，立即终止，不得创建实验产物或继续另一阶段。

运行结束后，报告必须满足 `status=completed`、40 个唯一 case、16 个 fresh manifest 和 16 个对应 DB。集成后的 postflight 必须给出 `run.identity_gate.valid=true`、`manifest_count=16`、`matching_manifest_count=16`，且 16/16 manifest 的 `extractor_version` 等于该臂预期值。任一不符时报告状态必须是 `invalid`，整臂不得进入质量比较。

现有 pytest 质量 gate 仍以 overall 0.90 为门槛，因 reader 单次采样而可能返回退出码 1。只有当报告 `status=completed`、40 cases 和 identity postflight 全部成立时，这种退出码才可解释为“质量未达旧门槛”而非运行中止；`aborted`、`invalid`、缺报告或身份异常一律是无效实验。

## 5. 预注册判据

先应用身份/完整性判据，再应用质量判据；不得用质量分数豁免身份失败。

### 5.1 每臂必须全部满足

1. **提取量**：16 个唯一 DB 的 `claims` 总数不少于 280。
2. **总体准确率**：40 题至少答对 35 题，即 raw accuracy `>=0.875`，按两位小数报告为 `>=0.88`。名义水位仍是 36/40（0.90），只允许 reader 单次采样的 ±1 题容差；34/40 不通过。
3. **关键枚举密度**：事件 `perltqa:e2e:227a5ff7fc9e83f1752725c9` 至少关联 15 条不同 claim。计数口径固定为对应 DB 中 `evidence_links` 满足 `derived_type='claim'`、`evidence_type='event'`、`evidence_id` 等于该事件 ID 的 `COUNT(DISTINCT derived_id)`；不得用 event coverage=1.0 代替。
4. **MemDaily 无回归**：12/12 case 成功、extraction coverage=1.0、Recall@5 `>=0.875`、MRR `>=0.861111`。QA 参考水位是 11/12；考虑同一 reader 容差，操作下限为 10/12，低于该值视为回归。
5. **运行完整性**：0 failed cases；16 个 ingest unit 均为 `fresh_ingest`；报告、manifest、DB 和汇总查询结果完整保留。

提取量和关键事件密度都从 DB 复算，不能只信 `cases[*].ingest.extracted_claims`。cap telemetry 只记录观察值，不作为本轮修复对象，也不因计数为 0 或非 0 单独判胜负。

### 5.2 选臂规则

- 只有一臂满足全部判据：选择该臂。
- 两臂都满足：选择 Arm A（cap24），因为它提供更小的安全上限，而本轮没有证据需要额外容量。
- 两臂都不满足：不选臂；保留 atomic prompt，另开新协议调查，不得恢复默认关闭的补提取重试链。
- 任一臂身份无效：整轮 A/B 无效；即使另一臂分数较高也不作因果结论，必须先修复执行隔离后以新 run ID 重跑双臂。

## 6. Hermes 交付清单

每臂交付：worktree 绝对路径、完整 Git HEAD、`git status --short`、Python 可执行文件绝对路径、实际 `hl_mem.__file__` 与 E2E 模块路径、配置路径及预登记 SHA-256、preflight 布尔结果、原始 JSON 报告、16 个 manifest、16 个 DB、postflight 结果、DB 复算的 claims 总数、关键事件 claim 数和 MemDaily 指标。

最终比较表必须并列展示两臂的预期/实际 extractor version、identity 有效性、claims、35/40 判据、关键事件密度、MemDaily 四项指标、cap telemetry 和裁决。缺少任一身份或原始产物字段时，只能标记 `invalid`，不能补写推断值。
