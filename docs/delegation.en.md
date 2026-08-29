# Delegation Host Integration

This guide is for an adjudication host running outside HL-Mem. The host performs bounded polling, reads complete
dossiers, makes decisions, and calls the write endpoint. HL-Mem does not install a cron job or systemd unit and no
longer contains an LLM conflict judge.

> **Trust boundary:** HL-Mem is a trusted, local, single-tenant service. If the host is not on the same machine, put
> authentication, authorization, and TLS in front of the API. Never expose it directly to the public Internet.

## Choose a conflict owner and action path

Every deployment must assign a conflict owner: either an automated delegation loop that performs bounded polling and
adjudication, or a human/on-demand owner that acts after a notice. Without a loop, `manual_required` cases do not
disappear on their own. The Hermes conflict notice is constrained by session-level system-prompt construction and the
cached health snapshot, so it can be delayed. It notifies only when a session first observes a nonzero count or when
that count changes; rebuilding with the same count does not repeat the notice. It is a notification, not a background
adjudicator.

Installing the Hermes provider alone does not give the host conflict-adjudication tools. The provider exposes only the
read-only `hl_mem_recall` tool; the REST contract below is the complete pair/group review and resolution surface. The
CLI can list cases, but its `resolve` command does not cover group cases and is not a complete substitute. In manual
mode, a host with HTTP or shell access can call REST. Otherwise, an external operator with API access must own the
workflow.

Installation acceptance must exercise the complete path in a controlled environment: a nonzero
`manual_required_count` can be expanded into its open cases, a dossier can be read, and the designated owner can submit
one CAS-guarded decision using that dossier's revision and fingerprint. Confirming only that the notice appears or that
`/healthz` reports a count is not sufficient.

## REST contract

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/conflicts?status=manual_required&limit=20&offset=0` | Page through open cases; `limit` is 1–100 |
| `GET` | `/v1/conflicts/{case_id}` | Read the compact review, `revision`, v2 `fingerprint`, and group candidates |
| `GET` | `/v1/conflicts/{case_id}/dossier` | Read the complete pair/group dossier, bitemporal fields, evidence, tips, and lineages |
| `POST` | `/v1/conflicts/{case_id}/resolve` | Submit one CAS-guarded decision for the identified case type |

The list item's `group_key` identifies the case type: `null` means pair and a non-null value means group. Do not infer
the type from candidate counts or Claim text. A dossier is capped at 1 MiB; an oversized dossier returns `413` and a
missing case returns `404`.

Every decision must include `expected_revision` from the snapshot just read. Hosts should also include that snapshot's
`expected_fingerprint`. If either value is stale, the service returns `409` before mutating a Claim, case, or audit row.
After a `409`, fetch the dossier again and make a new decision; never blindly replay the old request. The fingerprint is
sensitive to tips, supersession edges, and adjudication fields. The dossier exposes both tips and complete lineages, so
the old Claims' `valid_to`, `recorded_to`, and evidence remain visible.

### Pair request

The exact action vocabulary is `{keep_left, keep_right, coexist, reject}`:

- `keep_left` / `keep_right` selects that tip as the winner.
- `coexist` means both sides can be true; both Claims become active and the case closes.
- `reject` rejects this pair-conflict assertion; both Claims become active and neither is retracted.

```json
{
  "action": "coexist",
  "expected_revision": 3,
  "expected_fingerprint": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "rationale": "The records describe different non-overlapping valid-time windows",
  "resolver": "agent:delegation-host"
}
```

A successful response uses the pair projection:

```json
{
  "case_id": "case-01",
  "generation": 1,
  "revision": 4,
  "status": "resolved",
  "decision": "coexist",
  "winner_id": null,
  "resolved_at": "2026-08-30T02:30:00+00:00",
  "closed_case_ids": ["case-01"]
}
```

### Group request

The exact action vocabulary is `{select_candidate, reject_candidate}`. `candidate_key` must come from the current
review or dossier:

```json
{
  "action": "select_candidate",
  "candidate_key": "candidate-key-from-current-dossier",
  "expected_revision": 7,
  "expected_fingerprint": "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
  "rationale": "The evidence chain and valid time support this candidate",
  "resolver": "agent:delegation-host"
}
```

`reject_candidate` is destructive: it retracts every member Claim of the candidate and returns the case to
`manual_required` so the remaining candidates can be adjudicated. The request must also include
`"confirm_retraction": true`; omission or `false` fails closed with `422`. This confirmation does not apply to
`select_candidate` or any pair action.

A terminal `resolved` or `rejected` case has immutable rationale. One successful request writes one governance action
in the same transaction; replaying the old CAS returns `409` and does not create a second audit row.

## Adjudication rules

1. Prefer `coexist` when both pair sides can be true, including equivalent wording, non-overlapping time windows, or
   different qualifiers.
2. If evidence or coordinates are incomplete, or the meaning remains uncertain, do not POST. Preserving
   `manual_required` is safer than guessing.
3. After `409`, refetch the dossier and decide again. Do not reuse the old conclusion or fingerprint.
4. Unattended group automation should allow only `select_candidate`; do not let a model emit `reject_candidate`.
5. Reserve `reject_candidate` for an explicit human destructive operation. Show the member Claims and impact count
   before sending confirmation.
6. Do not confuse pair `reject` with group `reject_candidate`: the former restores both sides; the latter retracts a
   candidate's members.

## Bounded host loop

Bound each pass by pages, cases, and total elapsed time. Language-neutral pseudocode:

```text
deadline = now + 45s
offset = 0
handled = 0
while offset < 200 and handled < 20 and now < deadline:
    page = GET /v1/conflicts?status=manual_required&limit=20&offset=offset
    for case in page.cases:
        if handled == 20 or now >= deadline: break
        dossier = GET /v1/conflicts/{case.case_id}/dossier
        kind = "pair" if dossier.group_key is null else "group"
        decision = adjudicate(kind, dossier)
        if decision is uncertain: continue
        if kind == "group" and decision.action != "select_candidate": continue
        POST once with dossier.revision + dossier.fingerprint
        if 409: record stale; continue       # refetch next pass; do not retry now
        if POST failed: record failure; continue
        handled += 1
    if offset + page.limit >= page.total: break
    offset += page.limit
```

Disable automatic HTTP retries for POST, especially for `reject_candidate`. A GET may have a small number of backed-off
transient retries within the pass budget; failures must never become an unbounded loop.

## Linux host example

Install the real runner as `/opt/hl-mem-delegation/run-once`. It performs exactly one pass and uses
`--fail --max-time` for every curl call. For example, a read request is:

```bash
curl --fail --silent --show-error --max-time 5 \
  'http://127.0.0.1:8200/v1/conflicts?status=manual_required&limit=20&offset=0'
```

The runner should create a one-use JSON file or stream the body for a POST, with retries disabled:

```bash
curl --fail --silent --show-error --max-time 8 \
  -H 'Content-Type: application/json' \
  --data-binary @/run/hl-mem-delegation/decision.json \
  'http://127.0.0.1:8200/v1/conflicts/case-01/resolve'
```

A systemd oneshot permits only one pass at a time and sends process output to journald:

```ini
# /etc/systemd/system/hl-mem-delegation.service
[Unit]
Description=HL-Mem bounded delegation pass
After=network-online.target hl-mem.service

[Service]
Type=oneshot
User=hlmem
Group=hlmem
RuntimeDirectory=hl-mem-delegation
RuntimeDirectoryMode=0750
ExecStart=/usr/bin/flock -n /run/hl-mem-delegation/host.lock /opt/hl-mem-delegation/run-once
TimeoutStartSec=60
StandardOutput=journal
StandardError=journal
```

```ini
# /etc/systemd/system/hl-mem-delegation.timer
[Unit]
Description=Run HL-Mem delegation every five minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
RandomizedDelaySec=30s
Persistent=false

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now hl-mem-delegation.timer
journalctl -u hl-mem-delegation.service --since today
```

If a timer is not used, cron can call the same bounded runner. `flock -n` skips overlapping passes. During deployment,
verify that the `hlmem` user can create a file in the chosen lock directory:

```cron
*/5 * * * * /usr/bin/flock -n /run/lock/hl-mem-delegation.lock /opt/hl-mem-delegation/run-once 2>&1 | /usr/bin/systemd-cat -t hl-mem-delegation
```

Host logs should include at least the `case_id`, case type, input revision/fingerprint, action, HTTP status, and elapsed
time. Do not log keys or complete sensitive Claim text. Alert on consecutive failed passes, a surge in `409`/`413`, or
growing backlog age; do not respond to a failure by rapidly replaying write requests.
