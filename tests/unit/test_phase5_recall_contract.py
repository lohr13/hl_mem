from __future__ import annotations

import inspect

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
