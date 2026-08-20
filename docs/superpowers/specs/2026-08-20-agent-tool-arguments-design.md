# Agent Tool Arguments Contract Repair

## Context and diagnostic evidence

The v0.29.1 behavioral run stopped before judging because only 63 of 131
unique agent inputs produced valid traces. The executor currently asks the
model for `tool_calls[].arguments_json` as a string, validates only its string
length, and then parses it with `json.loads`.

On 2026-08-20, an exact replay of failing input
`8f921e8fcf8246be4b4b4499dd0a3167c2139ed8732e3126fbe8f610783da069`
against `qwen3.7-plus-2026-05-26` returned request
`chatcmpl-8e6486ac-af37-9120-928d-bcd6d905fb34`. Its four tool calls all
contained the string `"{  "`. That value satisfies the frozen schema's
`minLength` constraint but is not JSON. The call used 422 input and 383 output
tokens and cost CNY 0.003908 under a CNY 0.02 diagnostic hard gate.

An object-arguments replay of mismatch input
`11c8de0567dcb208490d4c161d74e634916fd998b28f06586bed140fec9dfabe`
returned `{"package":"hl_mem"}` for a fixture containing
`{"package":"hl-mem"}`. Those distribution names are equivalent under the
standard Python package-name normalization rule. Replays for other mismatch
classes returned materially different targets (`celery` vs `hl-mem-worker`,
`deploy/compose.yml` vs `hl-mem`, `verify_current_config_path` vs
`configuration`, and `auth.header.name` vs `auth.header`); those must remain
invalid. The agent-contract diagnostic replays added CNY 0.020982. A later
judge replay added CNY 0.004436, for CNY 0.025418 total diagnostics.

Alibaba Cloud's structured-output documentation states that JSON Schema mode
is intended to enforce object structure and types for Qwen3.7-Plus. The
contract should therefore model arguments as their natural object type rather
than encode JSON inside a schema-validated string:
<https://help.aliyun.com/en/model-studio/qwen-structured-output>.

## Considered approaches

1. Keep `arguments_json` and add JSON5-style parsing. This would accept syntax
   outside the declared strict contract and would not repair the model's
   observed incomplete `"{  "` value.
2. Replace the string with a generic object. This fixes the double encoding,
   but still lets the model invent argument keys and targets which the runner
   cannot execute.
3. Use a per-input strict object schema bound to the deterministic fixture's
   single executable invocation. This matches the runner's actual capability,
   prevents duplicate calls, and preserves exact fail-closed execution.

Approach 3 is selected.

## Contract and data flow

`build_blind_agent_input` will copy each allowed tool and bind every fixture
argument into its parameter schema with `const`. This publishes only the
read-only invocation the runner can execute; fixture results, current truth,
cohort, arm, and gold labels remain hidden.

`build_agent_plan_schema` will derive the response schema from those bound
available tools. The frozen data set exposes zero or one tool per input, so
`tool_plan` and `tool_calls` will have a maximum of zero or one item.
`tool_calls[].arguments` will be an object copied from the tool's parameter
schema. The response schema version and name will advance to v2.

The blind-input identity will include a digest of both plan and final response
schemas. A contract change will therefore invalidate resumable agent records
instead of silently reusing traces produced under a different model request.

At execution, mapping values are used directly. A strict JSON string is still
accepted at the internal execution boundary for legacy callers, but comments,
single quotes, and trailing commas remain invalid. The fixture comparison is
ordinary mapping equality, which already ignores key order; the current
fixtures define no optional defaults. Only
`inspect_python_install.arguments.package` receives Python distribution-name
normalization, so `hl_mem`, `hl-mem`, and `HL.Mem` are equivalent. All other
keys and values remain exact.

## Full-run findings and bounded follow-up repairs

The repaired v2 contract produced 131 valid current agent traces from 131
unique schema-aware inputs with no new invalid agent records. This allowed the
runner to enter judging for the first time.

The first judge pass exposed two resumability/scorer defects that were
previously unreachable. Two distinct agent inputs can produce byte-identical
judge payloads; a result key based only on the judge payload therefore collided
on resume. Judge result identity now also includes `agent_input_sha256`, and
resume uniqueness uses the backward-compatible `(result_key,
agent_input_sha256)` pair so legacy collisions are distinguishable without
blocking later judge prompt/schema identities for the same agent input.

Fourteen judge responses initially failed exact evidence validation. Repeating
the same blind request gave the model no way to correct a missing dimension,
YAML-style rendering, ellipsis, or paraphrased quote. The first attempt remains
an independent blind judgment. Only after `validate_judgment` rejects an output
does the next request receive the previous output, the exact validation error,
and bounded exact text snippets grouped by allowed trace source. Retry response
schemas constrain `quote` to those snippets. The unchanged validator remains
the final authority; no invalid evidence reaches aggregation.

`--phase all` now reuses an existing passing 9/9 sentinel artifact as requested
and reruns it only when the artifact is missing, malformed, or fails its gate.
Stale agent records whose schema-aware hashes are outside the current input set
are ignored, while duplicate current records still fail closed.

## Failure handling

Unknown tools, extra or missing argument keys, non-object arguments, malformed
legacy JSON strings, and materially different fixture targets raise
`AgentTraceInvalid`. No malformed call produces a tool result or reaches the
judge. The expected-count gates and budget reservations are unchanged.

## Verification and acceptance

Regression tests must prove the v2 object schema, bound fixture values,
schema-aware input hashing, direct mapping execution, strict legacy JSON
support, package-name equivalence, and rejection of JSON5 or different tool
targets. The targeted suite and full unit suite must pass.

The final command is the requested full run:

```powershell
& '.\.venv\Scripts\python.exe' -m scripts.run_v0291_behavioral_eval --phase all --budget-cny 14.796848
```

The run may enter judging only after `agent valid_count == expected_count ==
131`; aggregate and report artifacts must then be regenerated. Sentinel
behavior remains a 9/9 gate, and the budget ledger must report zero outstanding
reservations. All code, tests, plans, evidence, and report updates are delivered
in one repair commit as requested.

The completed run reached agent 131/131, judge 131/131, and aggregate 320/320.
Its behavioral quality verdict remained fail-closed because stable retention
was 0.95 against a 0.98 threshold and the nine-item manual blind review was not
filled. Artifact-derived paid evaluation usage was CNY 1.717162; adding CNY
0.025418 diagnostics and the user's approximately CNY 0.27 prior spend gives
approximately CNY 2.01258, well below the CNY 14.796848 hard ceiling. The final
incremental ledger had zero reservations outstanding.
