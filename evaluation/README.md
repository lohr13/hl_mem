# Benchmark 运行入口

从源码运行 benchmark 时统一使用仓库 launcher。以下示例从仓库根目录调用；launcher 基于自身脚本位置定位仓库，因此使用其绝对路径时也可从任意工作目录调用。launcher 会先切换到仓库根目录，再运行对应 runner。

```bash
# LongMemEval-S
bash scripts/hlmem-python.sh evaluation/tools/run_longmemeval_benchmark.py \
  --dataset evaluation/longmemeval/longmemeval_s_cleaned.json \
  --output evaluation/results/longmemeval_s_benchmark.json

# MemDaily
bash scripts/hlmem-python.sh evaluation/tools/run_memdaily_benchmark.py \
  --source D:/datasets/MemDaily/memdaily.json \
  --output evaluation/results/memdaily_benchmark.json

# PerLTQA
bash scripts/hlmem-python.sh evaluation/tools/run_perltqa_benchmark.py \
  --source D:/datasets/PerLTQA/perltmem.json \
  --qa-source D:/datasets/PerLTQA/perltqa.json \
  --output evaluation/results/perltqa_benchmark.json
```

Windows `cmd.exe` 使用相同参数，将命令前缀换成 `scripts\hlmem-python.cmd` 即可。先运行对应 runner 的 `--help` 可查看完整参数。

必须使用 launcher，是因为 Hermes gateway 等宿主可能把自身虚拟环境的 `site-packages` 注入 `PYTHONPATH`。直接启动 hl_mem 的 Python 会优先看到宿主包，甚至把 Python 3.11 的二进制扩展加载进 Python 3.13。launcher 会清除 `PYTHONPATH` 和 `PYTHONHOME`，并固定使用本仓库 `.venv/Scripts/python.exe`，避免跨环境污染。
