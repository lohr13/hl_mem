"""Fail-closed loopback client for frozen local-qwen evaluation calls."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

from hl_mem.evaluation.experiment_guards import assert_gold_free

Transport = Callable[[str, Mapping[str, Any]], Mapping[str, Any]]
TokenCounter = Callable[[str], int]
_COT_KEYS = frozenset({"analysis", "chain_of_thought", "cot", "reasoning", "thinking"})
_RESULT_KEYS = frozenset(
    {
        "ambiguity_flags",
        "confidence",
        "decision",
        "decisive_evidence_ids",
        "evidence_ids",
        "protected_atoms",
        "rationale_code",
        "summary",
        "winner_candidate_key",
    }
)


class UnsafeModelPayload(ValueError):
    """Raised before a payload containing gold or hidden reasoning is sent."""


class OversizedDocket(RuntimeError):
    """Raised when lossless evidence-card packing cannot satisfy the limits."""

    reason = "oversized_docket"


class ModelResponseError(RuntimeError):
    """Raised for malformed structured model output."""


@dataclass(frozen=True)
class QwenLimits:
    context_window: int = 16384
    max_input_tokens: int = 11500
    max_output_tokens: int = 1024
    max_chunk_tokens: int = 7500
    max_card_calls: int = 4
    max_calls: int = 6


@dataclass(frozen=True)
class QwenRunConfig:
    base_url: str = "http://127.0.0.1:8090/v1"
    model: str = "qwen3.8-27b-ud-iq4-xs"
    source_dir: str = "D:/qwen38-local/"
    prompt_version: str = "v030-judge-v1"
    tokenizer_identity: str = "qwen3.8-gguf-embedded"
    seed: int = 20260825
    enable_thinking: bool = False
    timeout_seconds: float = 90.0
    limits: QwenLimits = field(default_factory=QwenLimits)

    def __post_init__(self) -> None:
        host = (urlparse(self.base_url).hostname or "").casefold()
        if host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("local qwen base_url must use loopback")
        if self.enable_thinking:
            raise ValueError("enable_thinking must remain false")
        if self.limits.max_card_calls + 2 > self.limits.max_calls:
            raise ValueError("qwen card and decision call limits are inconsistent")

    @property
    def context_window(self) -> int:
        return self.limits.context_window

    @property
    def max_input_tokens(self) -> int:
        return self.limits.max_input_tokens

    @property
    def max_output_tokens(self) -> int:
        return self.limits.max_output_tokens

    @property
    def max_chunk_tokens(self) -> int:
        return self.limits.max_chunk_tokens

    @property
    def max_card_calls(self) -> int:
        return self.limits.max_card_calls

    @property
    def max_calls(self) -> int:
        return self.limits.max_calls


def _assert_no_cot(value: Any, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).casefold() in _COT_KEYS:
                raise UnsafeModelPayload(f"chain_of_thought field forbidden at {path}.{key}")
            _assert_no_cot(item, f"{path}.{key}")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, item in enumerate(value):
            _assert_no_cot(item, f"{path}[{index}]")


def _safe_response(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: copy.deepcopy(item) for key, item in value.items() if key in _RESULT_KEYS}


class LocalQwenRunner:
    """Execute at most four evidence-card calls and two permuted decisions."""

    def __init__(
        self,
        *,
        token_counter: TokenCounter,
        transport: Transport | None = None,
        config: QwenRunConfig | None = None,
    ) -> None:
        self.config = config or QwenRunConfig()
        self.token_counter = token_counter
        self.transport = transport or self._http_transport
        self.payload_snapshots: list[dict[str, Any]] = []

    def _http_transport(self, url: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        response = httpx.post(url, json=payload, timeout=self.config.timeout_seconds)
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, Mapping):
            raise ModelResponseError("local qwen response root must be an object")
        return value

    def _decode_response(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        if "choices" not in raw:
            return _safe_response(raw)
        try:
            content = raw["choices"][0]["message"]["content"]
            value = json.loads(content)
        except (IndexError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise ModelResponseError("local qwen structured response is malformed") from error
        if not isinstance(value, Mapping):
            raise ModelResponseError("local qwen structured content must be an object")
        return _safe_response(value)

    @staticmethod
    def _message(task: str, input_payload: Mapping[str, Any]) -> str:
        return json.dumps(
            {"task": task, "input": input_payload}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

    def _input_tokens(self, task: str, input_payload: Mapping[str, Any]) -> int:
        return int(self.token_counter(self._message(task, input_payload)))

    def _call(self, task: str, input_payload: Mapping[str, Any], token_limit: int) -> dict[str, Any]:
        tokens = self._input_tokens(task, input_payload)
        if tokens > token_limit:
            raise OversizedDocket(f"{task} input exceeds {token_limit} tokens")
        if len(self.payload_snapshots) >= self.config.max_calls:
            raise OversizedDocket(f"case exceeds {self.config.max_calls} model calls")
        request = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": "Return only the requested structured JSON."},
                {"role": "user", "content": self._message(task, input_payload)},
            ],
            "temperature": 0,
            "seed": self.config.seed,
            "max_tokens": self.config.max_output_tokens,
            "response_format": {"type": "json_object"},
            "chat_template_kwargs": {"enable_thinking": False},
            "metadata": {
                "input_tokens": tokens,
                "source_dir": self.config.source_dir,
                "prompt_version": self.config.prompt_version,
                "tokenizer_identity": self.config.tokenizer_identity,
            },
        }
        self.payload_snapshots.append(copy.deepcopy(request))
        endpoint = f"{self.config.base_url.rstrip('/')}/chat/completions"
        return self._decode_response(self.transport(endpoint, request))

    def _chunks(self, case_id: str, evidence: Sequence[Mapping[str, Any]]) -> list[list[Mapping[str, Any]]]:
        chunks: list[list[Mapping[str, Any]]] = []
        current: list[Mapping[str, Any]] = []
        for item in evidence:
            trial = [*current, item]
            payload = {"case_id": case_id, "evidence": trial}
            if self._input_tokens("evidence_card", payload) <= self.config.max_chunk_tokens:
                current = trial
                continue
            if not current:
                raise OversizedDocket(f"single evidence item exceeds {self.config.max_chunk_tokens} tokens")
            chunks.append(current)
            current = [item]
            if (
                self._input_tokens("evidence_card", {"case_id": case_id, "evidence": current})
                > self.config.max_chunk_tokens
            ):
                raise OversizedDocket(f"single evidence item exceeds {self.config.max_chunk_tokens} tokens")
        if current:
            chunks.append(current)
        if len(chunks) > self.config.max_card_calls:
            raise OversizedDocket(f"case requires more than {self.config.max_card_calls} evidence cards")
        return chunks

    def run_case(self, case: Mapping[str, Any]) -> dict[str, Any]:
        """Run one gold-free case with lossless packing and order verification."""

        self.payload_snapshots = []
        try:
            assert_gold_free(case)
        except ValueError as error:
            raise UnsafeModelPayload(str(error)) from error
        _assert_no_cot(case)
        decision_input = copy.deepcopy(dict(case))
        evidence = decision_input.get("evidence") or []
        if not isinstance(evidence, list) or not all(isinstance(item, Mapping) for item in evidence):
            raise UnsafeModelPayload("evidence must be a list of objects")
        if self._input_tokens("decision", decision_input) > self.config.max_input_tokens:
            if not evidence:
                raise OversizedDocket(f"decision input exceeds {self.config.max_input_tokens} tokens")
            decision_input.pop("evidence", None)
            cards = [
                self._call(
                    "evidence_card",
                    {"case_id": str(case.get("case_id") or ""), "evidence": chunk},
                    self.config.max_chunk_tokens,
                )
                for chunk in self._chunks(str(case.get("case_id") or ""), evidence)
            ]
            decision_input["evidence_cards"] = cards
        if self._input_tokens("decision", decision_input) > self.config.max_input_tokens:
            raise OversizedDocket(f"final docket exceeds {self.config.max_input_tokens} tokens")
        reverse_input = copy.deepcopy(decision_input)
        candidates = reverse_input.get("candidates")
        if isinstance(candidates, list):
            candidates.reverse()
        first = self._call("decision", decision_input, self.config.max_input_tokens)
        second = self._call("decision_verify", reverse_input, self.config.max_input_tokens)
        first_identity = first.get("decision"), first.get("winner_candidate_key")
        second_identity = second.get("decision"), second.get("winner_candidate_key")
        consistent = first_identity == second_identity and first.get("decision") is not None
        request_hashes = [
            hashlib.sha256(json.dumps(item, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            for item in self.payload_snapshots
        ]
        result: dict[str, Any] = {
            "case_id": str(case.get("case_id") or ""),
            "consistent": consistent,
            "decision": first.get("decision") if consistent else "manual_required",
            "decisions": [first, second],
            "call_count": len(self.payload_snapshots),
            "request_sha256": request_hashes,
        }
        if consistent:
            result["winner_candidate_key"] = first.get("winner_candidate_key")
            result["confidence"] = min(float(first.get("confidence", 0)), float(second.get("confidence", 0)))
        else:
            result["failure_reason"] = "candidate_order_disagreement"
        return result
