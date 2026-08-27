# compact==20 软拆分 A/B 装备

本目录实现冻结协议 `softsplit_ab_20260827_v1`。运行时产物包含生产事件 ID、LLM 请求/响应和 claims，受仓库 `/var/` 忽略规则保护，不应提交或外发。

## 1. 导出无正文 manifest

```powershell
.venv\Scripts\python.exe var/eval/softsplit_ab_20260827/export_corpus.py `
  --database var/hl_mem.db `
  --output var/eval/softsplit_ab_20260827/manifest.json `
  --since 2026-08-19T00:00:00+00:00 `
  --expected-cases 83
```

manifest 只保存 case/source event ID、来源库路径、导出时间与 `content_sha256`，不复制 `content_json`。审计使用 UTC 时间，冻结起点是 `2026-08-19T00:00:00+00:00`。已从来源库消失的 event 会保留 case 并标记 `available=false`；runner 会在调用 API 前校验存在性与哈希，缺失或正文变化时该 case 失败关闭。

## 2. 运行真实 A/B

```powershell
.venv\Scripts\python.exe var/eval/softsplit_ab_20260827/run_ab.py `
  --manifest var/eval/softsplit_ab_20260827/manifest.json `
  --config hl_mem.toml `
  --env-file .env `
  --output var/eval/softsplit_ab_20260827/runs.jsonl `
  --concurrency 8
```

runner 强制 `qwen3.7-plus`、百炼 coding endpoint、DashScope provider、`enable_thinking=False` 和 verifier off；并发参数只接受 1–8。每个 case 先跑 A；B 的根请求按请求指纹重放 A 的响应，左右子块才访问真实 API，从而固定同提取缓存。输出逐 case 记录完整请求/响应、claims、审计事件、token、`net_new_after_split`、重复画像与失败原因。重复画像使用配置中的真实 embedding 和生产 `dedup.threshold`，统计 fact-hash 精确重复及 `is_safe_near_duplicate` 可合并的语义近重复。

脚本默认断点续跑：`runs.jsonl` 中已有的 case 不会再次调用 API。需要重跑时应先显式改用新的输出路径，避免误覆盖原始结果。

## 3. 冻结门禁判分

```powershell
.venv\Scripts\python.exe var/eval/softsplit_ab_20260827/score_results.py `
  --manifest var/eval/softsplit_ab_20260827/manifest.json `
  --runs var/eval/softsplit_ab_20260827/runs.jsonl `
  --output var/eval/softsplit_ab_20260827/score.json
```

判分器逐线输出 PASS/FAIL：中位数净新增不少于 3 且至少 50% case 净新增不少于 2；重复率增幅不超过 5pp；B 臂失败或缺失请求率不超过 2%。语料不完整、case 重复或指标缺失时相应门禁 fail closed。
