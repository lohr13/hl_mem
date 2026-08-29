# GLM effort=low 20案终验门禁

- 总判定：FAIL
- 成本实耗：¥0.080195；均价 ¥0.004010/案
- GLM 价格口径：¥0.4/百万输入 tokens、¥1.4/百万输出 tokens（5折）实测
- 主语误绑单独报告，不并入虚构 claim。

| 门禁 | 实测 | 线 | 判定 |
|---|---:|---:|:---:|
| density | {"qualifying_cases":15,"cases":20,"failed_cases":[{"case_id":"3af1765478754e3f9764472f6e167da0","claims":11},{"case_id":"3d9eed608581474698063d108e85e9bb","claims":0},{"case_id":"819557d08c834cc49434b3d0fcb53c02","claims":9},{"case_id":"84d2d519d115428aab26b82e58a088f3","claims":2},{"case_id":"abe73898662c4bd093aa959d464c8b70","claims":0}]} | >=18/20 claims>=12 | FAIL |
| latency_p50 | 21.666608300 | <=40s | PASS |
| latency_p95 | 42.022064485 | <=90s | PASS |
| reasoning_tokens | {"max":331,"over_1024":0} | all <=1024 | PASS |
| cost_mean | 0.004009770 | <=¥0.02/case | PASS |
| schema_success | 0.950000000 | >=95% | PASS |
| duplicate_rate | {"duplicates":0,"claims":269,"rate":0.0} | <=2% | PASS |
| hallucination | {"reviewed_cases":5,"hallucinated_claims":0} | 0 fabricated claims in same 5-case gold review | PASS |
| subject_misbinding | {"reviewed_cases":5,"claims":1} | diagnostic only; excluded from hallucination gate | INFO |
| gold_coverage_corrected | {"covered":34,"total":40,"coverage":0.85} | >=90% on 4 non-boundary cases | FAIL |
| run_integrity | {"actual_api_calls":20,"final_cases":20,"max_actual_calls_per_case":1} | 20 calls, 20 final cases, exactly 1 actual call/case | PASS |
| freeze_integrity | {"mismatches":[]} | manifest/gold/runs hashes match pre-run freeze | PASS |
