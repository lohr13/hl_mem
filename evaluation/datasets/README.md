# v0.30 状态评测冻结资产

- `v0300_state_sealed_*`（v1）已在2026-08-22首次终验中烧毁，仅为审计保留；不得再用于调参、重跑或发布判定，也不得人工读取样本内容。
- `v0300_state_sealed_r2_*` 曾由r2/r3终验使用，现已烧毁；其语料、gold及运行产物不得再次消费。
- `v0300_state_sealed_r4_*` 是独立held-out generation：五类quota保持36/24/24/24/12；真实上下文严格晚于r2资产冻结时点，来源比例22条真实去标识上下文/98条fresh-salt合成对抗样本。当前已冻结，等待一次性终验。
- manifest只允许做hash、quota、标识闭合与不重叠证明等aggregate检查；检查过程不得输出JSONL记录内容或样本ID。
- dev资产继续只用于开发回归，不能替代held-out守门。
