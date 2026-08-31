# Provider live smoke

This repository-only harness runs one disposable, governed Provider smoke. It is intentionally absent from normal CI
and from the wheel. Unit tests use recording adapters and never call paid services.

Run it only with an explicit production-style config and output:

```powershell
uv run --frozen python benchmarks/provider/live_smoke.py `
  --config C:\secure\hl-mem-provider-smoke.toml `
  --output C:\secure\provider-live-smoke-result.json
```

The config is an immutable template. `database.path` must be a simple relative filename such as `smoke.db`; absolute,
nested, parent-relative, drive and UNC paths are rejected. Extraction and embedding must be real, reranking must be
real, and Fake components are rejected. Provider credentials remain in the normal `.env`/process environment boundary.

`usage.price_book_path` is mandatory. It must point to a versioned CNY price book accepted by the host pricing loader
and cover the configured LLM, embedding and reranker models. The harness validates that source file without modifying
it, copies it into a newly created temporary root, and binds both the one-run database and usage ledger to that root.
The root is removed in `finally` cleanup.

Hard ceilings cannot be raised: 10 LLM requests, 30 embedding items, 100 rerank documents and CNY 20 (20,000,000
microunits). Preflight happens before the first Provider call. Missing rules, unknown cost under the money limit, final
ledger overruns or active reservations fail closed.

The atomically written result contains hashes/fingerprints, bounded labels and counters, latency, safe categories and
boolean checks only. It never contains fixture/provider response text, credentials, service endpoints, database paths or
temporary-root paths. Price-book provenance is limited to the validated effective date and declared HTTPS source URLs.
