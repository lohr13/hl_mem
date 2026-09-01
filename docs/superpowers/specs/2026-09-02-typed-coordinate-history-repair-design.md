# HL-Mem Typed Coordinate and History Repair

## Goal

Publish `v1.1.0rc3` with a deterministic fix for operational-model coordinate drift, exact task isolation for state changes, and a bounded repair command for clearly obsolete historical extraction-model Claims.

## Problem

An operational model statement is only safe to supersede when its full coordinate is known:

```text
canonical subject + canonical slot + required qualifiers
```

For model choices the required qualifier is `task`. Statements about extraction, answering, embedding, and reranking must never enter the same conflict chain. RC2 repairs extraction-specific wording, but other task-decorated HL-Mem subjects can still remain free-form and uncoordinated. Historical free-form statements also remain active even after an authoritative runtime projection exists.

TTL expiry is a separate lifecycle concern. RC3 does not change it.

## Design

### 1. Deterministic typed coordinate resolver

Generalize the RC2 source-bounded projection into one pure resolver for the closed operational-model task registry. The resolver recognizes extraction, answering, reader, judge, embedding, reranking, summarization, compression, translation, code generation, image generation, vision, verification, and testing aliases.

For `choice.model`, a task is accepted only when exactly one task meaning is present in both original Evidence and the Claim's public subject/value. Task-decorated HL-Mem subjects are normalized to `hl_mem` only when they match a closed grammar. Arbitrary named subjects remain unchanged. Currentness produces the existing non-coordinate `state_change=true` signal only when supported by Evidence.

The resolver validates the slot after qualifiers are complete. Ambiguous or unsupported statements remain storable but receive no operational slot or conflict key. The LLM prompt and output schema do not change.

The task registry is small and closed, so RC3 uses no Embedding, Reranker, or LLM call. Neural entity linking would add cost and unsafe forced matches without improving this bounded problem.

### 2. Exact-coordinate state changes

Once subject, slot, and task are complete, the existing ingest transaction, conflict resolver, and supersede path perform the state change atomically. RC3 does not broaden the version-specific `state_latest_wins` module and does not add another cleanup worker.

Only statements with identical complete coordinates can supersede each other:

```text
project:hl_mem / choice.model / task=extraction
project:hl_mem / choice.model / task=answering
```

These are separate chains. Missing or ambiguous task coordinates never trigger automatic supersession.

### 3. Bounded historical repair

Add a CLI application service that treats the unique active runtime-config extraction Claim as the authoritative winner. It inspects older active Claims and selects only candidates whose stored Evidence independently resolves to the same complete extraction coordinate.

Dry-run is read-only and reports the winner, candidate IDs, exclusions, and expected count. Apply requires `--expected-count`, repeats the selection in one immediate transaction, fails closed if the target set changed, and uses the existing repository supersede operation. It does not rewrite Claim text, invent coordinates, delete rows, or touch unrelated task families. The mutation remains visible through existing audit and supersede history.

The command is manual and explicit. Package installation, startup, migration, and Worker maintenance never run it automatically.

### 4. Lifecycle boundary

Existing TTL expiry continues to handle expired Claims. RC3 only handles a newer authoritative value replacing an older, non-expired value at the same complete coordinate. No new lifecycle mode, table, configuration, or scheduled task is added.

## Safety boundaries

- Zero new model calls and zero new database migrations.
- No broad fuzzy subject merge and no best-candidate guessing.
- No cross-task supersession.
- No automatic historical rewrite or deletion.
- No change to public REST, MCP, Provider, configuration, or database contracts.
- No expansion of `state.latest_wins_slots`.

## Verification

- Extraction, answering, embedding, and reranking statements resolve to distinct coordinates.
- Ambiguous and unsupported statements fail closed.
- A new extraction state supersedes only the old extraction state.
- Historical dry-run is read-only; apply is count-guarded, idempotent, and auditable.
- TTL tests remain unchanged and green.
- Entity recall, Core 1.0 recall, no-answer, and forbidden-content gates do not regress.
- Full unit, type, formatting, complexity, contract, migration, build, clean-install, security, and remote release gates pass before publishing the immutable RC3 tag.
