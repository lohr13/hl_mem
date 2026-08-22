# v0.30 状态评测冻结资产

- `v0300_state_sealed_*`（v1）已于 2026-08-22 的首次完整终验中烧毁，仅为审计保留；不得再用于调参、重跑或发布判定，也不得人工读取样本内容。
- `v0300_state_sealed_r2_*` 是独立 replacement generation，当前已冻结但尚未执行终验；只有用户第三次单次授权后，判分器才能消费 corpus/gold 并仅输出聚合指标。
- 两代 manifest 可做日常 hash、quota 与不重叠证明检查；这些检查不得输出 JSONL 记录内容。
- dev 资产继续只用于开发回归，不能替代 sealed 守门。
