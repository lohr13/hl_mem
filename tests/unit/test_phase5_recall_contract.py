from __future__ import annotations

import inspect
from typing import Any

import pytest

import hl_mem.application.recall as recall_module
from hl_mem.application.recall import RecallService


def test_recall_module_keeps_documented_runtime_patch_points() -> None:
    assert callable(recall_module.hybrid_claims)
    assert callable(recall_module.recall_procedure)
    assert recall_module.ClaimRepository.__name__ == "ClaimRepository"
    assert callable(recall_module.current_audit)
    assert callable(recall_module.time.sleep)


def test_recall_service_keeps_thin_compatibility_method_signatures() -> None:
    expected = {
        "_record_access": ["self", "claims"],
        "_assemble_results": ["self", "claims", "namespace"],
        "_assemble_observations": ["self", "claim_ids"],
        "_materialize_context_packet": ["self", "bundle"],
        "_context_candidates": ["claims", "observations", "policies"],
        "_assemble_context": ["claims", "observations", "policies", "token_budget"],
        "_bundle_from_context_items": ["query_id", "answerability", "context_items"],
        "_context_from_packed_bundle": ["context_items", "bundle"],
    }

    for method_name, parameter_names in expected.items():
        method = inspect.getattr_static(RecallService, method_name)
        if isinstance(method, staticmethod):
            method = method.__func__
        assert list(inspect.signature(method).parameters) == parameter_names


def test_recall_compatibility_methods_delegate_to_focused_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = object()
    service = object.__new__(RecallService)
    service.connection = connection  # type: ignore[assignment]
    sentinel: list[dict[str, Any]] = [{"delegated": True}]

    monkeypatch.setattr(recall_module, "context_candidates", lambda claims, observations, policies: sentinel)
    monkeypatch.setattr(
        recall_module,
        "assemble_recall_observations",
        lambda received_connection, claim_ids: (
            sentinel if received_connection is connection and claim_ids == ["c"] else []
        ),
    )
    monkeypatch.setattr(
        recall_module,
        "assemble_recall_results",
        lambda received_connection, claims, namespace, **_kwargs: (
            sentinel if received_connection is connection and claims == [{"id": "c"}] and namespace == "n" else []
        ),
    )

    assert RecallService._context_candidates([], [], []) is sentinel
    assert service._assemble_observations(["c"]) is sentinel
    assert service._assemble_results([{"id": "c"}], "n") is sentinel
