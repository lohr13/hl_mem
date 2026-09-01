# Core 1.0 public benchmark

The Core 1.0 release benchmark is a deterministic, public, zero-network regression gate. It uses 32 synthetic
cases covering exact entity lookup, paraphrase, preference, historical lookup, and no-answer behavior. It is not a
claim about real-provider answer quality.

The runner writes memories through `POST /v1/memories`, drains them with the shipped worker, and queries only
`POST /v1/recall`. Fake extraction and embedding are fixed by the protocol; any external model call fails the run.

Run and compare:

```powershell
uv run --frozen python -m benchmarks.release.core_v1 --label local --commit <40-hex-commit> --output Temp/core-v1.json
uv run --frozen python -m benchmarks.release.compare_core_v1 benchmarks/release/results/v0.36.1.json Temp/core-v1.json
```

The frozen release rules allow at most `0.01` regression in Recall@5, MRR, and hard/soft abstention precision and
recall. HTTP success must be 100%, forbidden-status hits and external model calls must remain zero, and candidate
P95 latency must not exceed `max(baseline + 150 ms, baseline × 1.25)`.

Latency is recorded as environment-specific evidence. Functional fields and hashes must be identical across two
runs of the same package; only latency fields may vary.

## Exact-entity regression protocol

The companion `hl-mem-entity-v1` protocol contains 24 deterministic synthetic cases for entity-scoped retrieval.
It covers unique and multilingual aliases, ambiguity, overlap, multiple entities, incomplete links, temporal views,
namespace isolation, and a controlled entity-resolution storage failure. It is a targeted regression fixture, not a
statistical claim about production Recall.

Run the frozen 1.0 behavior in observe mode:

```powershell
uv run --frozen python benchmarks/release/entity_v1.py --mode observe --output Temp/entity-v1.json
```

The result stores stable IDs, scope decisions, channel counts, call counts, and timings. It does not store query or
Claim content. The protocol performs no external model calls; each case makes one deterministic test embedding call.
