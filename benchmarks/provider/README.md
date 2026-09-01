# Provider live smoke

This repository-only harness runs one disposable, governed Provider smoke. It is intentionally absent from normal CI
and from the wheel. Unit tests use recording adapters and never call paid services.

Run it only with explicit config, credential, price-book, and output paths:

```powershell
uv run --frozen python benchmarks/provider/live_smoke.py `
  --config C:\secure\hl-mem-provider-smoke.toml `
  --env-file C:\secure\hl-mem-provider-smoke.env `
  --price-book C:\secure\provider-prices.json `
  --output C:\secure\provider-live-smoke-result.json
```

The config is an immutable template. `database.path` must be a simple relative filename such as `smoke.db`; absolute,
nested, parent-relative, drive and UNC paths are rejected. Extraction and embedding must be real, reranking must be
real, and Fake components are rejected. Only the file named by `--env-file` supplies Provider credentials; adjacent
`.env` files and the process environment are ignored.

`--price-book` must name a versioned CNY price book accepted by the host pricing loader and cover the configured LLM,
embedding and reranker models. The harness fingerprints the explicit config, environment, and price-book files by
size, modification time, and SHA-256 before the run, verifies all three afterward, and executes from copied inputs in a
new temporary root. Symlink, junction, and parent-escape input paths are rejected. The root is removed in `finally`
cleanup.

Hard ceilings cannot be raised: 10 LLM requests, 30 embedding items, 100 rerank documents and CNY 20 (20,000,000
microunits). Preflight happens before the first Provider call. Missing rules, unknown cost under the money limit, final
ledger overruns or active reservations fail closed.

The atomically written result includes the Provider kind, core commit, UTC run time, fixed counters/checks/latencies,
hashes/fingerprints, bounded labels, and safe categories only. `passed=true` requires every fixed check to be present and
true. It never contains fixture/provider response text, credentials, service endpoints, database paths, or temporary-root
paths. Price-book provenance is limited to the validated effective date and declared HTTPS source URLs; userinfo, query,
and fragment components are rejected.

## Recorded built-in evidence

The sanitized [2026-09-01 built-in result](results/1.1.0-builtin-summary.json) records one disposable run against the
built-in adapters. The LLM was Zhipu GLM-5.3-Flash under Coding Plan quota; embedding and reranking used DashScope
Qwen models. The run settled 1 LLM request, 9 embedding items, and 16 reranked documents at an aggregate price-book
cost of 732,077 microunits (CNY 0.732077), with zero active reservations. Every fixed persistence, recall, fallback,
settlement, budget, and temporary-database check passed.

This is integration evidence for one bounded synthetic run. It is not a benchmark of recall quality, Provider
availability, or future billing.

## Recorded external-plugin evidence

The sanitized [2026-09-01 mixed result](results/1.1.0-external-plugin-summary.json) records a clean installation of the
independent `hl-mem-provider-dashscope` wheel. The built-in Zhipu adapter supplied the Coding Plan LLM while the external
plugin supplied both DashScope Embedding and Reranker adapters. The run settled 1 LLM request, 10 embedding items, and
15 reranked documents at a conservative price-book estimate of 760,000 microunits (CNY 0.76), with zero active
reservations. Every persistence, recall, controlled-failure fallback, settlement, budget, and temporary-database check
passed.

This proves the public Provider contract, Entry Point discovery, host governance, and two real external model paths for
one bounded synthetic run. It does not claim that the external LLM adapter was exercised against a live service, nor is
it a benchmark of recall quality, Provider availability, or future billing.
