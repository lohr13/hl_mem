# HL-Mem 1.1 Release Hardening Design

## Goal

Close three upgrade and operations gaps before publishing 1.1.0 without adding a new service, secret-sync mechanism, process scanner, or public API.

## Decisions

### Disabled Query Expansion is inert

When `recall.query_expansion_mode="off"`, every dedicated Query Expansion field is parked configuration. Validation and component construction ignore incomplete or unsupported `provider`, `base_url`, and API-key combinations and create no client. Active modes retain the existing fail-closed complete-line validation.

Configuration migration preserves parked fields. Deleting them would lose intentional future configuration and is unnecessary once disabled behavior is inert.

### Hermes environment ownership is explicit

Hermes continues to read `<HERMES_HOME>/hl_mem.toml` and `<HERMES_HOME>/.env`; the general CLI/server continues to read files selected by its working directory or explicit arguments. The installer never copies secrets and never requires both `.env` files to contain equal values.

`hermes install/upgrade` prints the exact target config and secret paths, states that the repository `.env` is not used by Hermes, warns when the target files are absent, and prints the existing `hl-mem doctor --config ... --env-file ...` command for readiness validation.

### Runtime identity closes the restart blind spot

The loaded Hermes plugin records a non-secret, versioned runtime-status document under `<HERMES_HOME>/state/hl_mem-runtime.json`. It contains:

- package version and resolved package source path;
- Git commit when the imported source file is tracked by an editable checkout;
- process ID and plugin-load timestamp;
- latest registration status, attempt timestamp, consecutive failure count, and safe exception type.

The identity is captured once at plugin import, so a later checkout cannot rewrite the identity of the already-running process. Status writes are atomic and best-effort: diagnostic I/O must never cause or hide a registration failure.

`doctor` compares the last observed loaded identity with the identity of the package running `doctor`. A failed registration or identity mismatch is a failure with an explicit gateway-restart instruction. Missing status is a warning because Hermes may not have started yet. `hermes install/upgrade` reports the same last-observed mismatch but does not inspect OS processes or claim the recorded PID is alive.

## Rejected approaches

- Installer-only restart text does not detect a checkout performed without reinstalling the thin plugin copy.
- Automatic `.env` copying or secret equality checks couple independent runtimes and create a secret-handling risk.
- Process enumeration, log scraping, heartbeats, and a background watcher add platform-specific complexity without improving the core identity comparison.
- Hashing an entire source tree is unnecessary for the observed failure; editable Git commit plus package version/source path covers checkout drift, while installed wheels use their immutable package version and path.

## Failure and privacy boundaries

- Runtime status never stores configuration values, URLs, exception messages, tracebacks, or secrets.
- Malformed status is reported as a doctor failure but never repaired automatically.
- Failure to write status is logged safely and never replaces the original plugin registration outcome.
- No database migration, REST/MCP contract, Provider Plugin API, or `ops report` schema changes are required.

## Tests and release gates

- Off-mode Query Expansion accepts every incomplete dedicated-line shape in `validate`, `validate_runtime`, and component construction; active modes still reject them.
- Installer output identifies both Hermes-owned files and never copies a source `.env`.
- Plugin bootstrap tests observe successful and failed atomic status records without secret leakage.
- Doctor tests cover matching identity, Git mismatch, failed registration, missing status, and malformed status.
- Focused tests, the full unit suite, static checks, contract snapshots, wheel build, and clean-wheel import/install checks must pass before merge.
