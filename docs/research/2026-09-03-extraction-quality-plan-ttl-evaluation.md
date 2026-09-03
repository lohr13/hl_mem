# Extraction quality plan/TTL evaluation (2026-09-03)

## Decision

**Status: NEEDS_CONTEXT (run2 authorization pending).** The compact-prompt smoke gate passed for all three extractors. Qwen3.7-Plus and GLM-5.3-Flash produced valid fresh 40-case run1 artifacts. The original local Qwen3.8-27B run1 attempt is invalid and preserved, but the subsequently authorized local-only `recovery1` completed as a valid fresh 40-case artifact. The prescribed challenger tie-break selects local Qwen3.8-27B because local and GLM have zero extraction-layer QA misses and local has higher extraction coverage (42/42 versus 40/42). No run2 was authorized or started.

Every provider is mathematically release-ineligible regardless of run2: Qwen3.7-Plus scored 34/40 QA, GLM-5.3-Flash 33/40, and local Qwen3.8-27B 32/40, each below the gate requiring the worse of two runs to be at least 36/40. GLM also missed the 41/42 extraction floor. The challenger replacement rule already fails because local trails Qwen by two QA cases in run1, where at most one is allowed. Qwen3.7-Plus therefore remains the recommended runtime default; no Provider configuration was changed.

## Evaluated identity and invariants

- Evaluated commit: `ca752d9e55e4d1af0ede084f208d013d37b23e8e`.
- Candidate prompt hash: `297ffd68bf0a`; baseline prompt hash: `7a02a17a7bd3`.
- Smoke fixture SHA-256: `c902f6d48b98b667ee5a91bbe655578a55642f9ca006f232ff47f631a5dc310f`.
- Fixed 40-case manifest SHA-256: `d49101237480a1d859993d99fffbaa5f62176b5b63ab10ce55d8c0a6d32b1786`; schema 3, 28 PerLTQA plus 12 MemDaily questions. All declared source hashes were verified: MemDaily `1b3a7928eeaab2e1c56b6b6200586078aa1af17eafcb4b80379cf9752b383a8f`, PerLTQA memory `f83d99fcb4d8954614aefb2768b32597fa80fdabf08c7217900a64e377d4f1e9`, and PerLTQA QA `e59536c160200ebe41385064c150406a44f7a08c23cd91f96953cbdf77a7a149`.
- Read-only operator config hashes: GLM `12078238a1ed11b5d117938325d5ac3f25079db1759237912e46128b4054136b`, Qwen3.7-Plus `3d34de2c61b44483db84a329498a150d98053d26d342e80e559e086e7869395f`, local 27B `00d5f13d8997a4ac260409ef5ba5e755355ed957b182c80c6778bb95c289c941`.
- The candidate virtual environment imported `hl_mem` from this worktree. All arms used `qwen3.7-text-embedding`, `qwen3-rerank`, and the fixed `qwen3.7-plus` QA reader. Query expansion, relation discovery, procedure recall, and recall side effects retained the E2E runner's fixed overrides.
- Provider coordinates were exact: `zhipu/glm-5.3-flash`, `dashscope/qwen3.7-plus`, and loopback `openai_compatible/qwen3.8-27b-ud-iq4-xs`. Credentials were loaded by variable name only and are not recorded here.
- Every official arm used forced refresh and an isolated cache/report root. The authorized local recovery retained the unchanged operator TOML, including `llm_timeout=120.0`, `llm_max_attempts=3`, and `llm_schema_retries=2`. Only the server launch added `-c 16384 --kv-unified`.

Official fresh-cache paths:

```text
var/eval/v114/candidate/full40/qwen37/run1/cache
var/eval/v114/candidate/full40/qwen37/run1/report.json
var/eval/v114/candidate/full40/glm53/run1/cache
var/eval/v114/candidate/full40/glm53/run1/report.json
var/eval/v114/candidate/full40/qwen38-27b/recovery1/cache
var/eval/v114/candidate/full40/qwen38-27b/recovery1/report.json
```

The original failed local attempt remains preserved at `var/eval/v114/candidate/full40/qwen38-27b/run1/` and is superseded, not an official result.

## Fixed smoke baseline versus candidate

All candidate reports are complete `extraction-quality-smoke-v1` artifacts with eight cases and one LLM call per case. Maximum retained count was 2 for GLM and 3 for both Qwen arms, below 16. Candidate negative-memory violations were zero, and every provider reported input/output tokens.

| Extractor | Phase | Passed | Coverage | Negative violations | Calls | Input | Output | Wall/recorded latency | Max retained |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| GLM-5.3-Flash | baseline | 7/8 | 10/11 | 0 | 8 | 19,184 | 870 | 24.294 s | 2 |
| GLM-5.3-Flash | candidate | 8/8 | 11/11 | 0 | 8 | 20,624 | 919 | 21.096 s | 2 |
| Qwen3.7-Plus | baseline | 7/8 | 11/11 | 1 | 8 | 19,266 | 2,061 | 42.600 s | 3 |
| Qwen3.7-Plus | candidate | 7/8 | 10/11 | 0 | 8 | 20,730 | 1,961 | 39.987 s | 3 |
| Qwen3.8-27B | baseline | 7/8 | 11/11 | 1 | 8 | 19,250 | 1,885 | 418.543 s | 3 |
| Qwen3.8-27B | candidate | 8/8 | 11/11 | 0 | 8 | 20,714 | 1,702 | 394.579 s | 3 |

Qwen3.7-Plus's only candidate semantic miss was one of two targets in `attributed_viewpoint_and_speaker`; it was not a hard safety failure. The worst input increase was +183 tokens/call, below the +250 limit (+180 for GLM; +183 for both Qwen arms).

Smoke report hashes: GLM `37f530945a9644146d8f7968713ca63b80a8e92d0ffc022b62f24938d3e905ba`; Qwen `0ad1ac20764c54dd1f854bd69de6c52e34277c8982b763c76fe860ace8f085d4`; local `52238caa5077376885544eab4cacd3b883388b102012936042b44e3d3e69385e`.

## Fresh 40-case run1 results

All three official artifacts have schema 3, `status=completed`, exactly 40 unique expected case rows, no case errors, and exactly 16 `fresh_ingest` units. Nonzero pytest exits reflect the classified metric gates.

| Extractor | Official artifact | Cases/errors | QA | F1 | R@5 | MRR | Extraction | Entity@5 | Negatives | Extraction in/out/total | QA tokens | Wall |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Qwen3.7-Plus | `run1`, valid | 40/0 | 34/40 (.8500) | .567305 | .937500 | .936458 | 42/42 (1.0000) | .250000 | 0 | 291,800 / 32,307 / 324,107 | 21,147 | 1,038.3 s |
| GLM-5.3-Flash | `run1`, valid | 40/0 | 33/40 (.8250) | .605265 | .950000 | .962500 | 40/42 (.952381) | .283333 | 0 | 292,324 / 21,280 / 313,604 | 21,080 | 801.6 s |
| Local Qwen3.8-27B | `recovery1`, valid | 40/0 | 32/40 (.8000) | .555978 | .937500 | .950000 | 42/42 (1.0000) | .283333 | 0 | 286,493 / 25,731 / 312,224 | 22,342 | 976.2 s |

Official report hashes: Qwen `be8724c7dc08d8931260c66abde238e094f54572bc697c5eceac8d785301a6a3`; GLM `bed4e6ecd330f08afa0f7a7bc99b25de4c6c8af2a09d3bbd3fa902695a3965db`; local recovery `f21ac6049575cc4b5bec7c46af7e9b90d2437a1314438582810021f2a102a9be`.

Ingest fingerprints: Qwen `9039fd8d05367cf31d682614314176e25c17a3d586693cf7f0a51016c78aa537`; GLM `1a94cd29f1cc004011c0e6f47dd9e08a4bc1be43c17fcc859d669a7c650398bb`; local `aab3d962b7e38f981f89877b8444bdf45e6a1d7bd2b0671465d036544ecd7fe0`.

The superseded partial local checkpoint hash is `2d0484578477fe0817715cae166e8f6935aa3359a2f65f391a380f29e1daa214`; sanitized error hash `3f9a5227f38ff9af26d348c55c4ff7d37c8ac6fa82909e24f935291b4f4dd7fb`.

Dataset metrics:

| Extractor / dataset | Cases | QA | F1 | R@5 | MRR | Extraction | Entity@5 | Negatives |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Qwen / PerLTQA | 28 | 23/28 (.821429) | .417579 | .964286 | .944940 | 16/16 | .178571 | 0 |
| Qwen / MemDaily | 12 | 11/12 (.916667) | .916667 | .875000 | .916667 | 26/26 | .416667 | 0 |
| GLM / PerLTQA | 28 | 22/28 (.785714) | .471807 | 1 | .982143 | 16/16 | .190476 | 0 |
| GLM / MemDaily | 12 | 11/12 (.916667) | .916667 | .833333 | .916667 | 24/26 | .500000 | 0 |
| Local / PerLTQA | 28 | 21/28 (.750000) | .401396 | .964286 | .964286 | 16/16 | .190476 | 0 |
| Local / MemDaily | 12 | 11/12 (.916667) | .916667 | .875000 | .916667 | 26/26 | .500000 | 0 |

Slice metrics (`cases; QA; F1; R@5; MRR; extraction; entity@5`):

| Slice | Qwen3.7-Plus | GLM-5.3-Flash | Local Qwen3.8-27B |
| --- | --- | --- | --- |
| `memdaily_aggregative` | 2; 1; 1; 1; 1; 8/8; .7083 | 2; 1; 1; 1; 1; 8/8; .7083 | 2; 1; 1; 1; 1; 8/8; .7083 |
| `memdaily_comparative` | 2; 1; 1; 1; 1; 4/4; .5000 | 2; 1; 1; 1; 1; 4/4; 1 | 2; 1; 1; 1; 1; 4/4; 1 |
| `memdaily_conditional` | 2; 1; 1; 1; 1; 4/4; .4167 | 2; 1; 1; .7500; 1; 3/4; .4167 | 2; 1; 1; 1; 1; 4/4; .4167 |
| `memdaily_noisy` | 2; .5; .5; .25; .5; 4/4; 0 | 2; .5; .5; .25; .5; 3/4; 0 | 2; .5; .5; .25; .5; 4/4; 0 |
| `memdaily_post_processing` | 2; 1; 1; 1; 1; 4/4; .5 | 2; 1; 1; 1; 1; 4/4; .5 | 2; 1; 1; 1; 1; 4/4; .5 |
| `memdaily_simple` | 2; 1; 1; 1; 1; 2/2; .375 | 2; 1; 1; 1; 1; 2/2; .375 | 2; 1; 1; 1; 1; 2/2; .375 |
| `perltqa_dialogues` | 8; .875; .5048; 1; 1; 4/4; 0 | 8; .375; .3696; 1; .9375; 4/4; 0 | 8; .625; .3537; 1; 1; 4/4; 0 |
| `perltqa_events` | 8; .625; .4301; .875; .8906; 4/4; .1042 | 8; .875; .7400; 1; 1; 4/4; .0625 | 8; .625; .5104; .875; .875; 4/4; 0 |
| `perltqa_profile` | 4; 1; .5584; 1; 1; 4/4; 0 | 4; 1; .5417; 1; 1; 4/4; 0 | 4; .75; .5398; 1; 1; 4/4; 0 |
| `perltqa_social_relationship` | 8; .875; .2475; 1; .9167; 4/4; .5208 | 8; 1; .2709; 1; 1; 4/4; .6042 | 8; 1; .2709; 1; 1; 4/4; .6667 |

All slice rows have zero failed cases and zero negative violations. Gate failures:

- Qwen: PerLTQA QA `.821429 < .85`; overall QA `.85 < .90`; noisy R@5 `.25 < .50`.
- GLM: PerLTQA QA `.785714 < .85`; overall QA `.825 < .90`; noisy R@5 `.25 < .50`.
- Local: PerLTQA QA `.75 < .85`; overall QA `.80 < .90`; noisy R@5 `.25 < .50`.

GLM's uncovered units are `memdaily:conditional:events:73` and `memdaily:noisy:events:10`. Both cases passed QA but fail extraction coverage.

### Local recovery and superseded failed attempt

The original local attempt timed out after bounded provider attempts during its second PerLTQA ingest bundle. Its `status=running` checkpoint has 14 rows and seven provider errors, so it is invalid and preserved only for audit.

The authorized recovery used a brand-new `recovery1` root, the same binary/GGUF/host/port/GPU layers/threads/parallelism, and added `-c 16384 --kv-unified`. It completed all 40 cases with no case error, cancellation, context overflow, or runtime error. Server logs contain 117 completed timing rows and 25,731 generated tokens, exactly matching the report. The 112 source messages imply five bounded additional successful calls. Weighted generation throughput was 47.03 tokens/s (45.36-47.90). This recovery supersedes the failed attempt for official comparison.

## Failure attribution

A covered gold event with top-five supporting evidence is assigned to QA/scorer. A covered, non-expired target absent from top five is retrieval. No official QA miss met the extraction or TTL definitions.

| Extractor | Case ID | Layer | Evidence |
| --- | --- | --- | --- |
| Qwen | `perltqa:0709ec234e33:events:4a3607094e6c` | QA/scorer | Support rank 1; reviewed rubric failed. |
| Qwen | `perltqa:23d905b73c57:dialogues:836f6182a0a9` | QA/scorer | Support rank 1; reviewed rubric failed. |
| Qwen | `perltqa:2ceebb337754:social_relationship:dc0a4055bb49` | QA/scorer | Support rank 3; official anchor failed. |
| Qwen | `perltqa:2ceebb337754:events:2825172d6952` | retrieval | Four active linked claims; none expired; first support rank 8. |
| Qwen | `perltqa:2ceebb337754:events:5e2fae77f0b5` | QA/scorer | Support rank 1; official anchor failed. |
| Qwen | `memdaily:noisy:events:29` | retrieval | Two active non-expiring linked claims; no top-five support. |
| GLM | `perltqa:0709ec234e33:events:4a3607094e6c` | QA/scorer | Support rank 1; reviewed rubric failed. |
| GLM | `perltqa:0709ec234e33:dialogues:7336d023b16e` | QA/scorer | Support rank 1; reviewed rubric failed. |
| GLM | `perltqa:23d905b73c57:dialogues:1edd39074cf1` | QA/scorer | Support rank 1; official anchor failed. |
| GLM | `perltqa:23d905b73c57:dialogues:836f6182a0a9` | QA/scorer | Support rank 1; reviewed rubric failed. |
| GLM | `perltqa:2ceebb337754:dialogues:24f99bbbc8ec` | QA/scorer | Support rank 2; reviewed rubric failed. |
| GLM | `perltqa:2ceebb337754:dialogues:375593a84122` | QA/scorer | Support rank 1; reviewed rubric failed. |
| GLM | `memdaily:noisy:events:29` | retrieval | Linked claims not expired; no top-five support. |
| Local | `perltqa:0709ec234e33:events:4a3607094e6c` | QA/scorer | Support rank 1; reviewed rubric failed. |
| Local | `perltqa:0709ec234e33:dialogues:7336d023b16e` | QA/scorer | Support rank 1; reviewed rubric failed. |
| Local | `perltqa:23d905b73c57:profile:5eac85f982dc` | QA/scorer | Support rank 1; official anchor failed. |
| Local | `perltqa:23d905b73c57:dialogues:1edd39074cf1` | QA/scorer | Support rank 1; official anchor failed. |
| Local | `perltqa:23d905b73c57:dialogues:836f6182a0a9` | QA/scorer | Support rank 1; reviewed rubric failed. |
| Local | `perltqa:2ceebb337754:events:2825172d6952` | retrieval | One active non-expired linked claim; no top-five support. |
| Local | `perltqa:2ceebb337754:events:5e2fae77f0b5` | QA/scorer | Support rank 1; official anchor failed. |
| Local | `memdaily:noisy:events:29` | retrieval | Two active non-expiring linked claims; no top-five support. |

Totals: Qwen 0 extraction, 0 TTL, 2 retrieval, 4 QA/scorer; GLM 0, 0, 1, 6; local 0, 0, 2, 6. Overall: 0 extraction, 0 TTL, 5 retrieval, 16 QA/scorer.

## Challenger selection and pending run2

The exact tie-break selects local Qwen3.8-27B:

1. Extraction-layer QA misses: GLM 0, local 0.
2. Extraction coverage: local 42/42, GLM 40/42.

No further tie-break was needed. No run2 path has been created. The exact pair awaiting authorization is:

```text
var/eval/v114/candidate/full40/qwen37/run2/cache
var/eval/v114/candidate/full40/qwen37/run2/report.json
var/eval/v114/candidate/full40/qwen38-27b/run2/cache
var/eval/v114/candidate/full40/qwen38-27b/run2/report.json
```

Each arm would run once from a new fresh cache. GLM and individual cases would not be repeated.

## TTL assertions

The production database was opened only via a SQLite URI with `mode=ro`. The exact aggregate query returned:

| Aggregate | Count |
| --- | ---: |
| All plan claims | 1,739 |
| `expires_at <= recorded_from` | 0 |
| `expires_at < occurred_start` | 0 |
| `expires_at < occurred_end` | 0 |

No production rows or private source text were recorded. Cache checks found no target claim expired at or before question as-of.

## Release, replacement, and run2 value

| Gate | Qwen3.7-Plus | GLM-5.3-Flash | Local Qwen3.8-27B |
| --- | --- | --- | --- |
| Worse-run QA >= 36/40 | **Fail: 34/40** | **Fail: 33/40** | **Fail: 32/40** |
| Worse-run extraction >= 41/42 | Pass: 42/42 | **Fail: 40/42** | Pass: 42/42 |
| Negative violations = 0 | Pass | Pass | Pass |
| No claim-count retry storm | Pass | Pass | Pass; 117 calls for 112 messages |
| Two fresh runs | No | No | No |

Because the gate uses the worse run, no run2 score can erase a sub-36 run1. Every provider is therefore release-ineligible regardless of run2. Local also trails Qwen by two QA cases, permanently failing the run1 side of the paired replacement rule. Run2 can add repeatability/variance evidence and complete the written protocol, but cannot change release or replacement decisions.

## Cost and latency

No currency cost is reported because price schedules were not pinned. Extraction-token totals were Qwen 324,107, GLM 313,604, and local 312,224. Wall observations were 1,038.3 s, 801.6 s, and 976.2 s. A two-arm run2 would plausibly consume roughly another 636k extraction tokens plus 43.5k QA tokens and about 33.6 minutes of sequential wall time if run1 usage repeated. This is an estimate, not a quote. Given the mathematically fixed release and replacement outcomes, that spend buys only reproducibility evidence.

The failed local attempt is excluded from official comparisons. The 5 retrieval and 16 QA/scorer misses are outside the extraction/TTL implementation decision; GLM's two uncovered units remain relevant even though those cases passed QA.

## GLM-5.3-Flash thinking reader replay

The reader-only replay completed from `2026-09-03T10:54:42.909269+00:00` through `2026-09-03T11:06:54.937048+00:00`: `status=completed`, 120 logical calls, 40 unique cases per arm, and zero failures. It reused each frozen source artifact's complete recorded evidence sequence and the unchanged prompt/scorers; it did not rerun extraction, embedding, reranking, recall, or database work.

The source report hashes exactly match the official artifacts above, preserving extractor identities `qwen3.7-plus`, `glm-5.3-flash`, and `qwen3.8-27b-ud-iq4-xs`, respectively:

| Arm | Frozen source SHA-256 | Unchanged extractor | Original QA reader |
| --- | --- | --- | --- |
| Qwen3.7-Plus | `be8724c7dc08d8931260c66abde238e094f54572bc697c5eceac8d785301a6a3` | `qwen3.7-plus` | `qwen3.7-plus` |
| GLM-5.3-Flash | `bed4e6ecd330f08afa0f7a7bc99b25de4c6c8af2a09d3bbd3fa902695a3965db` | `glm-5.3-flash` | `qwen3.7-plus` |
| Local Qwen3.8-27B | `f21ac6049575cc4b5bec7c46af7e9b90d2437a1314438582810021f2a102a9be` | `qwen3.8-27b-ud-iq4-xs` | `qwen3.7-plus` |

The original reader identity was `qwen3.7-plus` with thinking enabled, thinking budget `2048`, and answer budget `512`. The replay reader identity was `glm-5.3-flash` at `https://open.bigmodel.cn/api/paas/v4` with `thinking={"type":"enabled"}`. It retained prompt `memdaily-qa-prompt-v1`, QA scorer `deterministic-rubric-v2`, and answer-entity scorer `answer-entity-packet-v1`. The required Qwen-arm canary `perltqa:23d905b73c57:dialogues:836f6182a0a9` produced a non-empty final answer and positive thinking verification with exact metadata `attempts=1`, `input_tokens=330`, `output_tokens=396`, `reasoning_tokens=296`, `total_tokens=726`, `latency_seconds=13.639505699975416`, and `thinking_verified=true`. No reasoning content or private source text is recorded here.

| Frozen extractor arm | Qwen-reader QA / F1 | GLM-reader QA / F1 | Paired accuracy / F1 delta |
| --- | ---: | ---: | ---: |
| Qwen3.7-Plus | 34/40 (`0.85`) / `0.567305` | 36/40 (`0.9`) / `0.665215` | `0.050000000000000044` / `0.09791000000000005` |
| GLM-5.3-Flash | 33/40 (`0.825`) / `0.6052649999999999` | 33/40 (`0.825`) / `0.6167475` | `0.0` / `0.011482500000000062` |
| Local Qwen3.8-27B | 32/40 (`0.8`) / `0.5559775` | 33/40 (`0.825`) / `0.6007024999999999` | `0.02499999999999991` / `0.044724999999999904` |

Original Qwen-reader ranking: Qwen3.7-Plus, GLM-5.3-Flash, Local Qwen3.8-27B. GLM-reader replay ranking: Qwen3.7-Plus, GLM-5.3-Flash, Local Qwen3.8-27B.

Paired flip buckets below list every case ID from the completed summary.

### Qwen3.7-Plus extractor arm

- `unchanged_correct` (34): `perltqa:0709ec234e33:profile:121b3776babc`, `perltqa:0709ec234e33:social_relationship:240facd9c629`, `perltqa:0709ec234e33:social_relationship:7b6b2c31b857`, `perltqa:0709ec234e33:events:76906e779dac`, `perltqa:0709ec234e33:dialogues:77cdfec7b17e`, `perltqa:0709ec234e33:dialogues:7336d023b16e`, `perltqa:23d905b73c57:profile:5eac85f982dc`, `perltqa:23d905b73c57:social_relationship:ee0f8dfaec16`, `perltqa:23d905b73c57:social_relationship:fb560d262dc7`, `perltqa:23d905b73c57:events:4d6dcb33c587`, `perltqa:23d905b73c57:events:7471a4be405b`, `perltqa:23d905b73c57:dialogues:1edd39074cf1`, `perltqa:2ae36aa475cc:profile:91b059d0d213`, `perltqa:2ae36aa475cc:social_relationship:362879793b73`, `perltqa:2ae36aa475cc:social_relationship:5a9b1bb8a2bb`, `perltqa:2ae36aa475cc:events:03de9c2f47ea`, `perltqa:2ae36aa475cc:events:0dc75b658086`, `perltqa:2ae36aa475cc:dialogues:0d532ef7647f`, `perltqa:2ae36aa475cc:dialogues:33f8eac60585`, `perltqa:2ceebb337754:profile:fd16e584ecdd`, `perltqa:2ceebb337754:social_relationship:c645b95d388e`, `perltqa:2ceebb337754:dialogues:24f99bbbc8ec`, `perltqa:2ceebb337754:dialogues:375593a84122`, `memdaily:simple:events:40`, `memdaily:simple:events:60`, `memdaily:conditional:events:73`, `memdaily:conditional:events:60`, `memdaily:comparative:events:60`, `memdaily:comparative:events:27`, `memdaily:aggregative:events:13`, `memdaily:aggregative:events:8`, `memdaily:post_processing:events:15`, `memdaily:post_processing:events:93`, `memdaily:noisy:events:10`.
- `right_to_wrong` (0): none.
- `wrong_to_right` (2): `perltqa:23d905b73c57:dialogues:836f6182a0a9`, `perltqa:2ceebb337754:social_relationship:dc0a4055bb49`.
- `unchanged_wrong` (4): `perltqa:0709ec234e33:events:4a3607094e6c`, `perltqa:2ceebb337754:events:2825172d6952`, `perltqa:2ceebb337754:events:5e2fae77f0b5`, `memdaily:noisy:events:29`.

### GLM-5.3-Flash extractor arm

- `unchanged_correct` (32): `perltqa:0709ec234e33:profile:121b3776babc`, `perltqa:0709ec234e33:social_relationship:240facd9c629`, `perltqa:0709ec234e33:social_relationship:7b6b2c31b857`, `perltqa:0709ec234e33:events:76906e779dac`, `perltqa:0709ec234e33:dialogues:77cdfec7b17e`, `perltqa:23d905b73c57:profile:5eac85f982dc`, `perltqa:23d905b73c57:social_relationship:ee0f8dfaec16`, `perltqa:23d905b73c57:social_relationship:fb560d262dc7`, `perltqa:23d905b73c57:events:4d6dcb33c587`, `perltqa:23d905b73c57:events:7471a4be405b`, `perltqa:2ae36aa475cc:profile:91b059d0d213`, `perltqa:2ae36aa475cc:social_relationship:362879793b73`, `perltqa:2ae36aa475cc:social_relationship:5a9b1bb8a2bb`, `perltqa:2ae36aa475cc:events:03de9c2f47ea`, `perltqa:2ae36aa475cc:events:0dc75b658086`, `perltqa:2ae36aa475cc:dialogues:0d532ef7647f`, `perltqa:2ae36aa475cc:dialogues:33f8eac60585`, `perltqa:2ceebb337754:profile:fd16e584ecdd`, `perltqa:2ceebb337754:social_relationship:c645b95d388e`, `perltqa:2ceebb337754:social_relationship:dc0a4055bb49`, `perltqa:2ceebb337754:events:5e2fae77f0b5`, `memdaily:simple:events:40`, `memdaily:simple:events:60`, `memdaily:conditional:events:73`, `memdaily:conditional:events:60`, `memdaily:comparative:events:60`, `memdaily:comparative:events:27`, `memdaily:aggregative:events:13`, `memdaily:aggregative:events:8`, `memdaily:post_processing:events:15`, `memdaily:post_processing:events:93`, `memdaily:noisy:events:10`.
- `right_to_wrong` (1): `perltqa:2ceebb337754:events:2825172d6952`.
- `wrong_to_right` (1): `perltqa:23d905b73c57:dialogues:836f6182a0a9`.
- `unchanged_wrong` (6): `perltqa:0709ec234e33:events:4a3607094e6c`, `perltqa:0709ec234e33:dialogues:7336d023b16e`, `perltqa:23d905b73c57:dialogues:1edd39074cf1`, `perltqa:2ceebb337754:dialogues:24f99bbbc8ec`, `perltqa:2ceebb337754:dialogues:375593a84122`, `memdaily:noisy:events:29`.

### Local Qwen3.8-27B extractor arm

- `unchanged_correct` (32): `perltqa:0709ec234e33:profile:121b3776babc`, `perltqa:0709ec234e33:social_relationship:240facd9c629`, `perltqa:0709ec234e33:social_relationship:7b6b2c31b857`, `perltqa:0709ec234e33:events:76906e779dac`, `perltqa:0709ec234e33:dialogues:77cdfec7b17e`, `perltqa:23d905b73c57:social_relationship:ee0f8dfaec16`, `perltqa:23d905b73c57:social_relationship:fb560d262dc7`, `perltqa:23d905b73c57:events:4d6dcb33c587`, `perltqa:23d905b73c57:events:7471a4be405b`, `perltqa:2ae36aa475cc:profile:91b059d0d213`, `perltqa:2ae36aa475cc:social_relationship:362879793b73`, `perltqa:2ae36aa475cc:social_relationship:5a9b1bb8a2bb`, `perltqa:2ae36aa475cc:events:03de9c2f47ea`, `perltqa:2ae36aa475cc:events:0dc75b658086`, `perltqa:2ae36aa475cc:dialogues:0d532ef7647f`, `perltqa:2ae36aa475cc:dialogues:33f8eac60585`, `perltqa:2ceebb337754:profile:fd16e584ecdd`, `perltqa:2ceebb337754:social_relationship:c645b95d388e`, `perltqa:2ceebb337754:social_relationship:dc0a4055bb49`, `perltqa:2ceebb337754:dialogues:24f99bbbc8ec`, `perltqa:2ceebb337754:dialogues:375593a84122`, `memdaily:simple:events:40`, `memdaily:simple:events:60`, `memdaily:conditional:events:73`, `memdaily:conditional:events:60`, `memdaily:comparative:events:60`, `memdaily:comparative:events:27`, `memdaily:aggregative:events:13`, `memdaily:aggregative:events:8`, `memdaily:post_processing:events:15`, `memdaily:post_processing:events:93`, `memdaily:noisy:events:10`.
- `right_to_wrong` (0): none.
- `wrong_to_right` (1): `perltqa:23d905b73c57:dialogues:836f6182a0a9`.
- `unchanged_wrong` (7): `perltqa:0709ec234e33:events:4a3607094e6c`, `perltqa:0709ec234e33:dialogues:7336d023b16e`, `perltqa:23d905b73c57:profile:5eac85f982dc`, `perltqa:23d905b73c57:dialogues:1edd39074cf1`, `perltqa:2ceebb337754:events:2825172d6952`, `perltqa:2ceebb337754:events:5e2fae77f0b5`, `memdaily:noisy:events:29`.

| Frozen extractor arm | Input | Output | Reasoning | Total | Recorded latency | Attempts | Failures |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Qwen3.7-Plus | `11090` | `6132` | `5316` | `17222` | `231.19763559964485` s | `40` | `0` |
| GLM-5.3-Flash | `12824` | `5102` | `4439` | `17926` | `180.13196009979583` s | `40` | `0` |
| Local Qwen3.8-27B | `11178` | `7124` | `6272` | `18302` | `211.30587820033543` s | `40` | `0` |
| **Total** | **`35092`** | **`18358`** | **`16027`** | **`53450`** | **`622.6354738997761` s** | **`120`** | **`0`** |

Interpretation: holding extraction and reader evidence fixed, the GLM thinking reader improved the Qwen extractor arm by two correct cases, left the GLM extractor arm unchanged in net correctness after one improvement and one regression, and improved the local arm by one correct case. The arm ranking was unchanged. These paired flips demonstrate reader sensitivity but do not reclassify extraction, retrieval, or TTL failures.

This reader-only replay is **not extraction run2**. Its GLM-reader scores are diagnostic and must not replace the official Qwen-reader run1 scores in the approved release or challenger rules. It does not change the approved release gate, does not authorize or complete run2, and does not switch any runtime or Provider configuration. Qwen3.7-Plus remains the recommended runtime default from the official extraction evaluation.

## Cleanup and scope

Failed-attempt server PID 34276 and recovery server PID 2320 were each stopped exactly. Both processes were confirmed gone and port 8090 was confirmed closed. Generated reports, caches, and logs remain ignored under `var/eval/v114/candidate/**`. This research report is tracked in local commits on branch `extraction-quality-plan-ttl`; extraction run2 remains separate and pending. No runtime Provider configuration or default was switched by the replay/report work, and nothing has been pushed, tagged, deployed, or published.
