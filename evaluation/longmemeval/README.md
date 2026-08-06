# LongMemEval adapter

本目录只保存适配器 manifest，不把 LongMemEval 正文打包进 wheel。

下载并遵守上游数据集许可后，可运行：

```powershell
hl-mem eval --benchmark longmemeval --subset core --source D:\datasets\longmemeval.json --output reports\longmemeval\core
```

`manifest.json` 固定适配器、lifecycle 规则、缺失时间 epoch 和 core ID。更新公开 core 样本时，应在同一提交中更新 revision、source SHA-256 与 ID 列表；仓库自带 fixture ID 仅供离线 smoke run。
