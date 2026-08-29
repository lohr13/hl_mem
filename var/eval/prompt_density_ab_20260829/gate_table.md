# Prompt 密度 A/B 门禁对照表

- 总判定：FAIL
- usage 原价估算实耗：¥0.331260

| 门禁 | 实测值 | 门槛 | 判定 |
|---|---:|---:|:---:|
| B 臂高密度案 claim 数 | {"qualifying_cases":14,"dense_cases":20} | 20 案中至少 18 案 >=12 claims | FAIL |
| B 臂延迟 p50 | 15.287976 | <=60s | PASS |
| B 臂延迟 p95 | 31.471823 | <=90s | PASS |
| 请求数/arm-case | 1.000000 | <=1.2 | PASS |
| reasoning tokens | {"nonzero_records":0,"missing_arm_cases":0} | 全部为 0 | PASS |
| B 臂输出 tokens p95 | 3742.750000 | <=4000 | PASS |
| B 臂平均成本/案 | 0.007351 | <=¥0.02 | PASS |
| 严格 schema 成功率 | 1.000000 | >=95% | PASS |
| 运行记录配置冻结完整性 | {"mismatch_count":0,"mismatches":[]} | 52 arm-cases 全部与 manifest 配置一致 | PASS |
| B 臂案内重复率 | {"duplicate_rate":0.0,"duplicates":0,"claims":344} | <=2% | PASS |
| 5 案关键事实覆盖与虚构 | {"reviewed_cases":5,"covered":27,"total":50,"coverage":0.54,"hallucinated_claims":0,"details_valid":true,"aggregate_consistent":true} | 5 案、coverage>=90%、hallucination=0 | FAIL |
| 6 个短事件零凑数 | {"reviewed_cases":6,"padding_claims":0,"details_valid":true,"aggregate_consistent":true} | 6 案、padding=0 | PASS |
| 运行 metadata 输入绑定 | {"passed":true,"mismatches":[],"expected":{"protocol_id":"prompt_density_ab_20260829_v1","manifest_sha256":"15b85acfedf0e4f4bdd5923b2369f0e23a54eb9ac1ea6b4d735283d72d3b5fc9","gold_definition_sha256":"983890fc12e76df71699031551ee622af2afa7449eda8ed5d7c9eec350661977"}} | metadata 存在且 protocol/manifest/gold 与本次判分输入一致 | PASS |
| 盲审 gold 冻结完整性 | {"pre_run":"983890fc12e76df71699031551ee622af2afa7449eda8ed5d7c9eec350661977","score_time":"983890fc12e76df71699031551ee622af2afa7449eda8ed5d7c9eec350661977"} | pre-run hash == score-time hash | PASS |
