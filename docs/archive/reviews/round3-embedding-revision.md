# Round 3: Embedding 选型修正

## 背景
用户质疑首版选择智谱 embedding-3 而非阿里 text-embedding-v4。

经查证两家最新 API 文档，对比结果：

| 维度 | 智谱 embedding-3 | 阿里 text-embedding-v4 |
|------|-----------------|----------------------|
| 最大维度 | 2000 | 2048 |
| 可选维度 | 512/1024/1536/2000 | 64/128/256/512/768/1024/1536/2048 |
| 单条最大token | ~8192 | 8192 |
| 批量上限 | 64条/批 | 10条/批 |
| 价格 | ~0.0005元/千token | 0.0005元/千token（Batch 0.00025） |
| MTEB中文 | 不错 | SOTA（8B版多语言#1） |
| 开源 | 闭源 | 全开源(0.6B/4B/8B) |
| Sparse向量 | 不支持 | 支持 dense+sparse 混合 |

## 修正决定
首版默认改为 text-embedding-v4 2048维。理由：
1. MTEB SOTA，中文表现更强
2. dense+sparse 混合对中文短文本检索有加分
3. 全开源，未来可本地部署
4. 同价位，百炼有独立配额池

智谱 embedding-3 降为 fallback，接口保留多 embedding column 设计。

## 需 Codex 确认
你是否同意此修正？有无其他顾虑？特别是：
1. text-embedding-v4 批量上限只有10条/批（vs embedding-3 的64条），对 batch 提取效率有影响吗？
2. sparse vector 在 SQLite BLOB 中存储和暴力检索是否需要额外设计？
