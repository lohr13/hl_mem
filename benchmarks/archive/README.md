# Historical benchmark archive

This directory contains frozen experiment infrastructure that is useful for research reproducibility but is not part of the supported HL-Mem runtime.

`v030/` contains the v0.30 Batch 4, plan/price, latest-wins, corpus-generation, replay, scoring, and remote-refreeze experiments together with their original tests. It is excluded from wheels, source distributions, normal CI, and release gates. Run it only from a source checkout:

```powershell
uv run --frozen python -m pytest benchmarks/archive/v030/tests -q
```

Stable `hl-mem eval`, the full-chain quality smoke, and state-lifecycle scoring remain under `src/hl_mem/evaluation/` and continue to ship in the wheel.
