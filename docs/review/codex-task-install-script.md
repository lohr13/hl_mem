# Task: Create install_to_hermes.py + Full Hermes Plugin Adapter

## Objective

Create two files:

1. **`D:/workspace/hl_agent/hl_mem/src/hl_mem/adapters/hermes/plugin/__init__.py`** — Full-featured Hermes memory provider plugin (replaces the current minimal 125-line one)
2. **`D:/workspace/hl_agent/hl_mem/src/hl_mem/adapters/hermes/plugin/plugin.yaml`** — Updated plugin metadata
3. **`D:/workspace/hl_agent/hl_mem/install_to_hermes.py`** — Standalone install/upgrade script

## Background

The Hermes agent at `C:/Users/Administrator/AppData/Local/hermes/hermes-agent/` loads memory providers from `plugins/memory/<name>/__init__.py`. The current hl_mem plugin (`plugins/memory/hl_mem/__init__.py`, 125 lines) only implements basic `sync_turn` + `prefetch`. It is MISSING:

- `on_memory_write()` — explicit memory writes don't reach hl_mem
- `on_pre_compress()` — compressed messages aren't synced
- Episode/Trace sync — Experience channel data isn't pushed from Hermes
- Circuit breaker — no graceful degradation
- `on_delegation()` — subagent results aren't tracked

The hl_mem repo has `src/hl_mem/adapters/hermes/provider.py` (194 lines) with these features, but it uses `httpx` (not installed in Hermes venv) and doesn't inherit from `MemoryProvider`.

## File 1: Full Plugin Adapter (`plugin/__init__.py`)

### Requirements:
- Inherit from `agent.memory_provider.MemoryProvider`
- Use **ONLY `urllib.request`** (stdlib) — NO httpx, NO requests, NO external deps
- Use `threading.Thread` for background prefetch (same pattern as current plugin)
- Implement circuit breaker: 5 consecutive failures → open circuit for 60s

### Methods to implement:

```python
class HlMemProvider(MemoryProvider):
    def __init__(self) -> None:
        # Read config from env: HL_MEM_URL (default http://localhost:8200), HL_MEM_TIMEOUT (default 10)
        # Circuit breaker state: _failure_count, _failure_threshold=5, _circuit_open_until=0.0
        # Threading: _lock, _thread, _cache dict

    @property
    def name(self) -> str: return "hl_mem"

    def is_available(self) -> bool:
        # Check HL_MEM_ENABLED env != "false"

    def initialize(self, session_id, **kwargs) -> None:
        # Store session_id, hermes_home

    def get_tool_schemas(self) -> list: return []

    def system_prompt_block(self) -> str:
        return "# hl_mem Memory\nActive. Relevant memories injected into context."

    def _post(self, path: str, payload: dict) -> str:
        """Synchronous POST with circuit breaker. Returns response body or empty string."""
        # urllib.request.Request with json data, method="POST"
        # On success: reset failure count, return response body
        # On failure: increment failure count, maybe open circuit, return ""

    def _can_call(self) -> bool:
        # Check circuit breaker: time.monotonic() >= _circuit_open_until

    def sync_turn(self, user_content, assistant_content, *, session_id="", messages=None) -> None:
        """Called after each turn. Posts events + syncs episodes."""
        active_session = session_id or self._session_id
        # POST /v1/events for user message
        # POST /v1/events for assistant message
        # If messages provided and has tool_calls: call self._sync_episode(messages, active_session)

    def _sync_episode(self, messages: list, session_id: str) -> None:
        """Extract tool calls from messages, create Episode + Traces."""
        # Extract tool_calls from messages (both OpenAI format and tool-role messages)
        # If < 2 tool calls: skip
        # Build observations dict from tool-role messages
        # Find goal (first user message)
        # Determine task_type from tool names
        # POST /v1/episodes → get episode_id
        # For each tool call: POST /v1/episodes/{episode_id}/traces
        # Determine status/reward based on errors
        # PATCH /v1/episodes/{episode_id} with status+reward+outcome_summary

    def queue_prefetch(self, query, *, session_id="") -> None:
        # Thread-based: POST /v1/recall, cache result by session_id

    def prefetch(self, query, *, session_id="") -> str:
        # Return cached result (or empty string)

    def on_memory_write(self, action, target, content, metadata=None) -> None:
        """Mirror explicit memory writes to hl_mem."""
        # POST /v1/memories with {"text": content, "qualifiers": {"action": action, "target": target}}

    def on_pre_compress(self, messages) -> str:
        """Sync messages about to be compressed."""
        # For each message: POST /v1/events
        # Return "" (no contribution to compression prompt)

    def on_delegation(self, task, result, *, child_session_id="", **kwargs) -> None:
        """Track subagent results."""
        active_session = self._session_id
        # POST /v1/events for task
        # POST /v1/events for result

    def shutdown(self) -> None:
        # Join prefetch thread with timeout

def register(ctx) -> None:
    ctx.register_memory_provider(HlMemProvider())
```

### Tool call extraction logic (from messages):

```python
def _extract_tool_calls(messages):
    """Returns list of {"id": str, "action": str}."""
    # First try OpenAI format: message["tool_calls"][i]["function"]["name"]
    # Fallback: message where role=="tool", use message["name"] or message["tool_call_id"]
    structured = []
    for msg in messages:
        for call in (msg.get("tool_calls") or []):
            func = call.get("function") or {}
            structured.append({"id": str(call.get("id", "")), "action": str(func.get("name") or "tool")})
    if structured:
        return structured
    # Fallback
    for idx, msg in enumerate(messages):
        if msg.get("role") == "tool":
            structured.append({"id": str(msg.get("tool_call_id", idx)), "action": str(msg.get("name") or "tool")})
    return structured
```

### Task type detection:

```python
def _detect_task_type(actions):
    lowered = [a.lower() for a in actions]
    if any(any(m in a for m in ("terminal", "read_file", "patch", "write_file", "search_files")) for a in lowered):
        return "coding"
    if any("web_search" in a or "web_extract" in a for a in lowered):
        return "research"
    return "general"
```

### Error signature detection:

```python
def _detect_error(observation):
    if observation and any(m in observation.lower() for m in ("error", "failed", "exception", "traceback")):
        return observation[:500]
    return None
```

## File 2: plugin.yaml

```yaml
name: hl_mem
version: 2.0.0
description: 'hl_mem local-first memory with Experience channel'
pip_dependencies: []
requires_env: []
hooks:
  - sync_turn
  - queue_prefetch
  - on_memory_write
  - on_pre_compress
  - on_delegation
  - on_session_end
```

## File 3: install_to_hermes.py (repo root)

### Requirements:
- Standalone Python script, run from repo root: `python install_to_hermes.py`
- Auto-detect HERMES_HOME from env var or common paths:
  1. `os.environ.get("HERMES_HOME")`
  2. `C:/Users/Administrator/AppData/Local/hermes/hermes-agent`
  3. `~/.hermes/hermes-agent`
- Target: `{HERMES_HOME}/plugins/memory/hl_mem/`
- Files to copy:
  - `src/hl_mem/adapters/hermes/plugin/__init__.py` → target `__init__.py`
  - `src/hl_mem/adapters/hermes/plugin/plugin.yaml` → target `plugin.yaml`
- **Backup**: before overwriting, copy existing files to `{target}/backup_{timestamp}/`
- **Idempotent**: if target doesn't exist, create it; if it does, overwrite (with backup)
- **Verification**: after copy, read back both files and verify they match source
- Print clear summary: what was installed, where, backup location
- Support `--hermes-home PATH` argument override
- Support `--dry-run` to preview without writing
- Exit code 0 on success, 1 on error
- Color output if terminal supports it (optional)

### Script structure:

```python
#!/usr/bin/env python3
"""Install/upgrade hl_mem adapter into Hermes plugin directory.

Usage:
    python install_to_hermes.py [--hermes-home PATH] [--dry-run]
"""
import argparse, os, shutil, sys, time
from pathlib import Path

REPO_ROOT = Path(__file__).parent.resolve()
SOURCE_DIR = REPO_ROOT / "src" / "hl_mem" / "adapters" / "hermes" / "plugin"
FILES = ["__init__.py", "plugin.yaml"]

def find_hermes_home(arg_override):
    if arg_override: return Path(arg_override)
    if os.environ.get("HERMES_HOME"): return Path(os.environ["HERMES_HOME"])
    candidates = [
        Path("C:/Users/Administrator/AppData/Local/hermes/hermes-agent"),
        Path.home() / ".hermes" / "hermes-agent",
        Path.home() / "AppData" / "Local" / "hermes" / "hermes-agent",
    ]
    for c in candidates:
        if (c / "plugins" / "memory").exists(): return c
    raise RuntimeError(f"Cannot find HERMES_HOME. Tried: {[str(c) for c in candidates]}")

def backup_existing(target_dir):
    existing = [target_dir / f for f in FILES if (target_dir / f).exists()]
    if not existing: return None
    ts = time.strftime("%Y%m%d_%H%M%S")
    backup_dir = target_dir / f"backup_{ts}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    for f in existing:
        shutil.copy2(f, backup_dir / f.name)
    return backup_dir

def install(target_dir, dry_run):
    # Verify source files exist
    # Backup existing
    # Copy source → target
    # Verify by reading back
    ...
```

## Constraints
- Use ONLY stdlib (urllib, json, os, threading, time, pathlib) — no external packages
- Follow the existing code style (type hints, docstrings)
- Keep the adapter self-contained (no imports from hl_mem package — it runs in Hermes's venv)
- The adapter must work even if hl_mem service is down (graceful degradation via circuit breaker)
- Do NOT modify any existing files — only CREATE new ones
- Create `src/hl_mem/adapters/hermes/plugin/__init__.py` (new directory `plugin/` under `hermes/`)
