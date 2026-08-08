# LongMemEval Retrieval/QA Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 LongMemEval reader 在固定预算内优先看到命中 claim 对应的 answer-bearing turn，执行 relation/state-aware 证据合成，并对 reader/judge 超时做有限重试。

**Architecture:** 改动限定在 LongMemEval runner 与对应 benchmark 单元测试。runner 从现有 event messages 计算 query-aware turn window，保留 `head` A/B 路径；reader prompt 内部执行结构化候选/关系/状态/时间合成；现有 QA retry loop 增加指定 transport timeout。

**Tech Stack:** Python 3.11、SQLite、httpx、unittest/pytest CI、Black、isort、Ruff、mypy。

## Global Constraints

- 不修改提取侧、生产 schema、RecallService、MemDaily 或 PerLTQA。
- 不切换索引默认值，不调整 episodic、reranker 权重或 judge 模型/题型规则。
- 不在本地运行 benchmark、canary 或 pytest；pytest 行为仅由 push 后 GitHub CI 验证。
- 本地运行 `black --check`、`isort --check-only`、`ruff check`、`mypy src/hl_mem/` 和 Python 编译检查。
- 默认 reader 模式为 `windowed`，`head` 保留旧行为；总预算 6000、单 event 预算 1200。

---

### Task 1: Query-aware reader event window

**Files:**
- Modify: `evaluation/tools/run_longmemeval_benchmark.py`
- Test: `tests/unit/test_longmemeval_batching.py`

**Interfaces:**
- Consumes: `LongMemEvalCase.question`、retrieved claim/value/evidence IDs、event `content_json.messages`。
- Produces: `_build_reader_user_prompt(connection, case, retrieved, context_mode="windowed") -> str`。

- [ ] **Step 1: 先写窗口行为测试**

构造包含 5 个 turns、gold 原句位于 turn 3 的 event；断言 `windowed` 包含 turn 2/3/4、排除 session 头部噪声、匹配 turn 排在 content 首部，并维持总/单 event token 上限。另写 `head` 断言保留旧头截断行为，以及无 `messages` 时回退文本的断言。

- [ ] **Step 2: 写 CLI 与报告身份测试**

断言 `parse_args([]).reader_context_mode == "windowed"`，`--reader-context-mode head` 可选；报告写入模式，resume 与 shard merge 对不同模式报 configuration mismatch。

- [ ] **Step 3: 实现最小窗口逻辑**

增加规范化、词/CJK bigram 单元、字符串相似度、event-to-claim needles、turn 选择和 focus-first 三 turn 渲染 helper。`_load_reader_events` 在 windowed 模式读取 messages，在 head 模式保持 `_event_content_text`；`_fit_reader_event` 继续统一执行 1200/6000 预算。

- [ ] **Step 4: 连接 runner 数据流**

给 CLI、`_run_case`、`_run_qa` 和 `_build_reader_user_prompt` 增加尾部默认参数；在报告、resume identity 与 merge identity 中记录并校验模式。现有直接调用不传参数时继续使用 `windowed`。

### Task 2: Relation/state-aware reader synthesis

**Files:**
- Modify: `evaluation/tools/run_longmemeval_benchmark.py`
- Test: `tests/unit/test_longmemeval_batching.py`
- Verify unchanged: `tests/unit/test_longmemeval_rejudge.py`

**Interfaces:**
- Consumes: 结构化 claims/events、Current Date、Question Type。
- Produces: reader system prompt；judge prompts 与题型分支原样保留。

- [ ] **Step 1: 先写 prompt 合约测试**

断言 reader prompt 要求内部候选答案/relation/state/time notes，明确 audition/participation、location/duration/distance、plan/executed 不可混同，并保留 genuinely insufficient/unavailable abstention；断言用户输出仍只要 final answer。

- [ ] **Step 2: 实现最小 prompt 改写**

把现有泛化合成说明替换为四步 Chain-of-Note 风格内部流程，不要求暴露推理；保留确定性共指/算术/日期推理边界和禁止猜测字段。不要修改 `_longmemeval_judge_prompts`。

### Task 3: Reader/judge transport timeout retry

**Files:**
- Modify: `evaluation/tools/run_longmemeval_benchmark.py`
- Test: `tests/unit/test_longmemeval_batching.py`

**Interfaces:**
- Consumes: 直接或 `__cause__`/`__context__` 链中的 `httpx.ReadTimeout`、`httpx.ConnectTimeout`，以及现有 HTTP status error。
- Produces: `_qa_call_with_retry(call, max_attempts=3)` 的统一有限退避。

- [ ] **Step 1: 先写超时与非重试测试**

分别让前两次调用抛 ReadTimeout、一次调用抛 wrapped ConnectTimeout 后成功；断言尝试 3 次及 sleep `[2.0, 4.0]`/`[2.0]`。另断言 `httpx.WriteTimeout` 和 HTTP 400 不重试。

- [ ] **Step 2: 实现异常链识别**

增加通用异常链 iterator 和 `_find_qa_timeout`，复用现有 retry loop。429 继续优先 `Retry-After`，5xx/timeout 使用 `2 * 2**(attempt-1)`；日志打印稳定 error label。

### Task 4: Verification, review, and delivery

**Files:**
- Review: `evaluation/tools/run_longmemeval_benchmark.py`
- Review: `evaluation/tools/merge_longmemeval_results.py`
- Review: `tests/unit/test_longmemeval_batching.py`
- Review: `docs/superpowers/plans/2026-08-09-longmemeval-retrieval-qa-phase2.md`

**Interfaces:**
- Consumes: 完整 diff 与用户允许的验证命令。
- Produces: main 提交、远端 push、GitHub CI 结论。

- [ ] **Step 1: 本地静态验证**

运行四项指定工具，目标 exit code 0；再对改动 Python 文件运行 `python -m py_compile`。不得运行 pytest/benchmark/canary。

- [ ] **Step 2: 范围与 mutation 审查**

核对三项需求逐条有测试；确认将匹配 turn 改回 event head、删除 timeout branch、删除 relation/state 指令时均至少有一条测试失败。确认 4 个既有未跟踪文件未暂存。

- [ ] **Step 3: 独立代码审查**

以阶段 2 设计、起始 SHA `c13f229` 和最终 diff 请求 reviewer；修复全部 Critical/Important，重新运行静态验证。

- [ ] **Step 4: 提交、推送与 CI**

提交实现与测试，推送 `main`。查询该 commit 的 GitHub Actions checks 直到完成；任何未运行或失败项目用 ⚠️ 如实报告。
