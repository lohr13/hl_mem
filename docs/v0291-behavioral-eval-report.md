# v0.29.1 Behavioral Evaluation Report

生成日期：2026-08-20。此报告冻结当前可获得的离线证据；未获得的行为或线上证据按 fail-closed 处理。

## 三字段结论

| 字段 | 结论 |
| --- | --- |
| `offline_structural_pass` | `true` |
| `offline_behavioral_pass` | `false` |
| `canary_ready` | `false` |

结构层 200 点 × 4 臂已全量通过，800 个 decision 均导出了精确最终 Context Packet 正文。
付费前置 sentinel 已 9/9 通过；行为层结论由完整 aggregate 与人工盲核共同决定。
线上 observe/canary 证据尚未测量。

## 付费与冻结身份

- 固定模型：`qwen3.7-plus-2026-05-26`
- 评测启动时 HEAD：`07dc79365a6b68ecf93436d2c1976c31d9bd918f`
- sentinel 最坏预留：¥—
- 最后一次增量 provider usage：input=5619, output=1368
- 最后一次增量估算实付：¥0.022182
- 预算硬上限：¥14.796848；reserved=0, outstanding=0

冻结 manifest 还记录了 behavioral/structural/sentinel fixture、agent system prompt、tool contract、judge prompt
与 strict JSON Schema 的 SHA-256。行为输入按完整盲输入 SHA-256 精确去重，80 点 × 4 臂共 320 个 assignment
物化为 131 个不同模型输入；去重不改变 paired denominator。

## 完整门禁表

| Gate | 类别 | 状态 | 阈值 | 观测 |
| --- | --- | --- | --- | --- |
| `structure.full_200x4` | structural | **pass** | 200 points x four exact arms | echo_off__freshness_off=200, echo_enforce__freshness_off=200, echo_off__freshness_render=200, echo_enforce__freshness_render=200 |
| `structure.context_packet_body` | structural | **pass** | exact Context Packet body on all 800 decisions | 800 |
| `echo.suppression_recall` | structural | **pass** | >=80% gold echo suppression recall | 1 |
| `echo.useful_retention` | structural | **pass** | >=99% useful non-echo retention | 1 |
| `echo.slice_equivalence` | structural | **pass** | 100% cross-session, historical/as-of, and hard-negative ID equivalence | cross_session=true, historical_and_active=true, proper_noun_hard_negative=true |
| `echo.false_suppression` | structural | **pass** | <=1% overall and zero on proper-noun/number hard negatives | 0 |
| `echo.empty_no_evidence` | structural | **pass** | empty packet delta <=1pp; no useful query becomes no_evidence | 0 |
| `freshness.claim_token_delta` | structural | **pass** | <=18 estimated tokens per annotated claim | 18 |
| `freshness.packet_budget_p95` | structural | **pass** | packet delta p95 <=3% of packed budget | 0.009 |
| `freshness.useful_no_evidence` | structural | **pass** | >=99% useful item retention; no useful query becomes no_evidence | 1 |
| `behavior.sentinel_9x9` | behavioral | **pass** | 9/9 valid schema/evidence/applicability and 9/9 gold match | valid_count=9, matched_count=9, passed=true |
| `behavior.blind_review_9` | behavioral | **blocked** | 3 stale + 3 stable + 3 boundary real traces manually reviewed and matched | — |
| `freshness.unsafe_acceptance` | behavioral | **pass** | treatment <=10% and >=50% relative reduction from control | control=0.0731707, treatment=0, relative_reduction=1 |
| `freshness.verification_action` | behavioral | **pass** | treatment verification action rate >=80% | 1 |
| `freshness.stable_retention` | behavioral | **fail** | frozen stable suite >=98% and <=2pp below control | control=0.95, treatment=0.95 |
| `freshness.false_staleness` | behavioral | **pass** | stable preference/identity increment <=1pp | control=0.05, treatment=0.05 |
| `runtime.observe_window` | runtime | **not_measured** | Production observe/canary evidence window completed | — |
| `runtime.freshness_packet_p95` | runtime | **not_measured** | Freshness packet delta p95 on production traffic | — |
| `runtime.freshness_renderer_p95` | runtime | **not_measured** | Freshness renderer latency p95 <= max(2ms, 2%) | — |
| `runtime.echo_recall_p95` | runtime | **not_measured** | Echo recall latency p95 <= max(5ms, 5%) | — |
| `runtime.echo_source_resolution` | runtime | **not_measured** | Echo source-session resolution >=95% and missing/read-error fail-open | — |

`stable_negative` 仅作为 20-case frozen acceptance suite 使用，不作总体误伤率外推。结构层的合成 token、
source-session 信号及耗时也不能替代生产 observe 数据。

## 本地 artifact

- `structural_replay.json` — SHA-256 `92b82128c4c018648c4b2efa5bcd7e4c8ba77a8cdf078c116a6b2e5e3b44f73a`
- `sentinel_smoke.json` — SHA-256 `32b16ffedfce2ac44ed71709d26ffa040f2705b6840371191a0629e8feee5290`
- `behavioral_aggregate.json` — SHA-256 `cf76b567b130763e1ea0a574352ff69b8175c0c03912c3157bbb716eee4e58e2`
- `budget_summary.json` — SHA-256 `cf6484c6ae50c6ebae9504e02a14576261c02c7fce2c726ccdc43a26125e8967`
- `run_manifest.json` — SHA-256 `d7c274123d6865b3296a2a61640f239fc0192c78ce4b5beef7793c3a18936e0e`
- `expanded_structural.jsonl` — SHA-256 `ecdf0e977f4f07f96a9330bb3812e1e54a58e897c0b42656f367eeb5c4c75ad1`

这些结果位于 gitignored 的 `evaluation/results/v0291_behavioral_20260820/`，不会进入提交。当前 tracked 报告
保留门禁状态和对应内容哈希。产品数据库未被修改。

## 解除阻断后的唯一续跑入口

将 worktree cwd `.env` 中的 `LLM_API_KEY` 替换为有效百炼 key 后执行：

```powershell
$env:PYTHONPATH=$null
& '.\.venv\Scripts\python.exe' -m scripts.run_v0291_behavioral_eval --phase all
```

该入口会重新执行结构层；已有通过的 9/9 sentinel 会被复用，缺失或失效时才重跑 sentinel。只有 sentinel 9/9 全对才会进入全量行为阶段。全量完成后还需
填写 `blind_review_result.json`（stale/stable/boundary 各 3 条人工盲核）并重新生成此报告。即使离线行为
通过，没有五项真实运行证据时 `canary_ready` 仍保持 `false`。
