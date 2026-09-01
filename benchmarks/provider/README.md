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
