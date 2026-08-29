# Prompt 密度 A/B（2026-08-29）

本目录是 eval-only 装备，不修改 `src/hl_mem/**`。冻结协议为：A 使用生产 prompt 与
`maxItems=20`；B 仅增加两条密度/防凑数规则并将 `maxItems` 提到 30。两臂均调用
`qwen3.8-flash` 的百炼 compatible-mode，关闭 thinking，使用 strict JSON Schema。

## 命令

```powershell
.venv/Scripts/python.exe var/eval/prompt_density_ab_20260829/run_prompt_density_ab.py prepare
.venv/Scripts/python.exe var/eval/prompt_density_ab_20260829/run_prompt_density_ab.py run
.venv/Scripts/python.exe var/eval/prompt_density_ab_20260829/run_prompt_density_ab.py score
```

`prepare` 从旧 manifest 的仍可按 ID+SHA 复验总体中，以 seed 20260829 冻结 20 个
语言×正文长度四分位密集案，并从冻结 MemDaily 样本中选取六类各一个短事件。
`run` 按 case hash 决定 AB/BA 顺序，4 并发、90 秒、单次尝试；前 10 个密集配对执行
冻结止损，结算成本达到 ¥0.80 后不再发新请求，in-flight 预留保证不超过 ¥1.00。
`score` 生成 `report.json`、`gate_table.md` 和 `comparison.csv`。
判分从逐案/逐事实盲审明细重新推导聚合值，并核对 manifest、显式 run metadata、
每条运行记录配置及预跑 gold hash；已发出请求若无法精确结算（含非 2xx 或缺少 usage），
预算守卫会按完整预留计费并停止新请求。

## 本次结论

本次执行 52/52 请求成功，usage 按百炼华北 2 官方原价估算实耗 ¥0.331260。最终
FAIL：B 臂仅 14/20 个密集案达到 12 claims；预跑冻结的 5 案 50 条关键事实覆盖
27/50（54%），虚构 0。其余硬门通过，因此不生成生产落地 diff，也不继续搜索后续
prompt 版本。

`runs.jsonl` 与 `manual_review.json` 含源证据或人工 gold，受仓库 `/var/` ignore 规则
保护，仅保留在本地；提交物只包含不带正文的 manifest、聚合报告、对照表、runner 与测试。
