"""Offline-only state canonicalization and atomicity experiment arms."""

from __future__ import annotations

import copy
import json
import os
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, TypeAlias

from hl_mem.domain.claims.conflicts import coordinate_qualifier_key
from hl_mem.domain.claims.state_coordinates import StateCoordinate
from hl_mem.domain.entity import normalize_entity_id

ArmName: TypeAlias = Literal["A", "B1", "B2"]
AtomicityStrategy: TypeAlias = Literal["split", "reject"]
B2ResponseProvider: TypeAlias = Callable[[Mapping[str, Any]], Mapping[str, Any]]

_KIND_PREDICATES = {
    "preference": "偏好",
    "architecture": "事实",
    "identity": "身份",
    "config": "配置",
    "fact": "事实",
    "plan": "计划",
    "choice": "使用",
}
_NON_ASSERTED_RE = re.compile(
    r"(?i)(?:计划|打算|准备|将来|下周|明天|要求|必须|应该|引用|转述|文档写道|"
    r"据.+(?:说|称)|并不|不是|未曾|尚未|没有|否认)"
)
_HISTORICAL_RE = re.compile(r"(?i)(?:历史|曾经|过去|之前|当时|此前|回顾|(?:19|20)\d{2}\s*年(?:时|期间))")
_VERSION_RE = re.compile(
    r"(?i)(?:(?:当前|现在|目前|现用|已安装|运行中)[^。；;，,]{0,24})?"
    r"(?:版本|version|release)[\s:=为是到至]*(?:当前|现在|目前)?"
    r"[\s:=为是到至]*v?\d+(?:\.\d+){0,4}(?:[-+][\w.-]+)?"
)
_SERVICE_HEALTH_RE = re.compile(
    r"(?i)(?:(?:服务|service)[^。；;，,]{0,28}"
    r"(?:healthy|unhealthy|running|stopped|down|up|正常|异常|健康|挂了|可用|不可用)"
    r"|(?:healthy|unhealthy|running|stopped|down|up|正常|异常|健康|挂了|可用|不可用)"
    r"[^。；;，,]{0,18}(?:服务|service))"
)
_PROCESS_RE = re.compile(
    r"(?i)(?:进程|process)[^。；;，,]{0,28}" r"(?:running|stopped|failed|exited|启动|运行|停止|退出|失败)"
)
_DEPLOYMENT_RE = re.compile(
    r"(?i)(?:部署|deployment)[^。；;，,]{0,28}" r"(?:deployed|running|failed|ready|成功|失败|完成|回滚|就绪)"
)
_CONNECTIVITY_RE = re.compile(
    r"(?i)(?:连接|connectivity|endpoint|端点)[^。；;，,]{0,28}"
    r"(?:reachable|unreachable|connected|disconnected|timeout|超时|不可达|连通|断开)"
)
_JOB_RE = re.compile(
    r"(?i)(?:任务|job)[^。；;，,]{0,28}" r"(?:running|queued|failed|completed|cancelled|运行|排队|失败|完成|取消)"
)
_CLAUSE_SEPARATOR_RE = re.compile(
    r"\s*(?:[；;，,。！？!?]|\.(?=\s|$)|\b(?:and|while|but)\b|同时|并且|而且|且|另外)\s*",
    re.IGNORECASE,
)
_SERVICE_RE = re.compile(r"(?i)([a-z][a-z0-9_.-]{0,63})\s*(?:服务|service)")
_CONNECTIVITY_SERVICE_RE = re.compile(r"(?i)([a-z][a-z0-9_.-]{0,63})\s*(?:连接|connectivity|endpoint|端点)")
_PROCESS_NAME_RE = re.compile(r"(?i)([a-z][a-z0-9_.-]{0,63})\s*(?:进程|process)")
_JOB_NAME_RE = re.compile(r"(?i)([a-z][a-z0-9_.-]{0,63})\s*(?:任务|job)")
_INSTANCE_RE = re.compile(r"(?i)(?:实例|instance|节点|node)\s*[:#=-]?\s*([a-z0-9][a-z0-9_.-]{0,63})")
_DEPLOYMENT_NAME_RE = re.compile(
    r"(?i)(?:(?:部署|deployment)\s*[:#=-]?\s*([a-z0-9][a-z0-9_.-]{0,63})"
    r"|\b(blue|green)\s*(?:部署|deployment)|(?:蓝|绿色?)部署)"
)
_SUBJECT_SERVICE_SUFFIX_RE = re.compile(r"(?i)^(.+?)(?:\s*的)?\s*(?:(?:api|web|worker)\s*)?(?:服务|service)$")
_SUBJECT_INSTANCE_SUFFIX_RE = re.compile(
    r"(?i)^(.+?)(?:\s*的\s*|\s+)(?:实例|instance|节点|node)\s*[:#=-]?\s*" r"[a-z0-9][a-z0-9_.-]{0,63}$"
)
_SUBJECT_PROCESS_SUFFIX_RE = re.compile(r"(?i)^(.+?)(?:\s*的\s*|\s+)[a-z][a-z0-9_.-]{0,63}\s*(?:进程|process)$")
_SUBJECT_JOB_SUFFIX_RE = re.compile(r"(?i)^(.+?)(?:\s*的\s*|\s+)[a-z][a-z0-9_.-]{0,63}\s*(?:任务|job)$")
_SUBJECT_DEPLOYMENT_SUFFIX_RE = re.compile(r"(?i)^(.+?)(?:\s*的\s*|\s+)(?:blue|green|蓝|绿色?)\s*(?:部署|deployment)$")
_SUBJECT_COORDINATE_SUFFIX_PATTERNS = (
    _SUBJECT_SERVICE_SUFFIX_RE,
    _SUBJECT_INSTANCE_SUFFIX_RE,
    _SUBJECT_PROCESS_SUFFIX_RE,
    _SUBJECT_JOB_SUFFIX_RE,
    _SUBJECT_DEPLOYMENT_SUFFIX_RE,
)

_EXPERIMENTAL_QUALIFIER_KEYS: dict[str, tuple[str, ...]] = {
    "config.version": ("component", "service", "environment", "deployment", "instance", "platform"),
    "state.service_health": ("service", "environment", "deployment", "instance"),
    "state.process": ("process", "environment", "deployment", "instance"),
    "state.deployment": ("deployment", "environment", "instance"),
    "state.connectivity": ("service", "environment", "deployment", "instance"),
    "state.job": ("job", "environment", "deployment", "instance"),
}


def _normalized_token(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value)).strip().casefold()


def _canonical_subject(value: str) -> str:
    canonical = normalize_entity_id(value)
    for pattern in _SUBJECT_COORDINATE_SUFFIX_PATTERNS:
        match = pattern.fullmatch(canonical)
        if match is not None and match.group(1).strip():
            return normalize_entity_id(match.group(1))
    return canonical


def _subject_has_stable_owner(value: str) -> bool:
    normalized = _normalized_token(value)
    generic_qualifiers = {"api", "web", "worker", "sync", "blue", "green"}
    for pattern in _SUBJECT_COORDINATE_SUFFIX_PATTERNS:
        match = pattern.fullmatch(normalized)
        if match is None:
            continue
        owner = _normalized_token(match.group(1))
        if owner and owner not in generic_qualifiers:
            return True
    return False


def _predicate(raw_claim: Mapping[str, Any], slot: str | None) -> str:
    kind = _normalized_token(raw_claim.get("kind", "fact"))
    predicate = _KIND_PREDICATES.get(kind, "事实")
    if slot is not None and slot.startswith("state."):
        return "状态"
    return predicate


def _state_context(raw_claim: Mapping[str, Any], text: str) -> str:
    if _normalized_token(raw_claim.get("kind")) == "plan" or _NON_ASSERTED_RE.search(text) is not None:
        return "non_asserted"
    if _HISTORICAL_RE.search(text) is not None:
        return "historical"
    return "current"


def _state_slot(text: str) -> str | None:
    for slot, pattern in (
        ("config.version", _VERSION_RE),
        ("state.process", _PROCESS_RE),
        ("state.deployment", _DEPLOYMENT_RE),
        ("state.connectivity", _CONNECTIVITY_RE),
        ("state.job", _JOB_RE),
        ("state.service_health", _SERVICE_HEALTH_RE),
    ):
        if pattern.search(text):
            return slot
    return None


def _environment(text: str) -> str | None:
    if re.search(r"(?i)(?:\bprod(?:uction)?\b|生产环境)", text):
        return "production"
    if re.search(r"(?i)(?:\bstag(?:e|ing)?\b|预发环境)", text):
        return "staging"
    if re.search(r"(?i)(?:\bdev(?:elopment)?\b|开发环境)", text):
        return "development"
    if re.search(r"(?i)(?:\btest(?:ing)?\b|测试环境)", text):
        return "test"
    return None


def _platform(text: str) -> str | None:
    match = re.search(r"(?i)\b(windows|linux|macos|darwin)\b", text)
    return _normalized_token(match.group(1)) if match else None


def _component(text: str) -> str | None:
    match = re.search(r"(?i)\b(server|cli|api|worker|plugin|sdk)\b", text)
    return _normalized_token(match.group(1)) if match else None


def _match_token(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    if match is None:
        return None
    value = next((group for group in match.groups() if group), None)
    return _normalized_token(value) if value else None


def _has_same_owner_service_alias(text: str, canonical_subject: str) -> bool:
    normalized = _normalized_token(text)
    service_matches = list(_SERVICE_RE.finditer(normalized))
    if len(service_matches) != 1:
        return False
    alias = _SUBJECT_SERVICE_SUFFIX_RE.fullmatch(normalized[: service_matches[0].end()])
    return alias is not None and normalize_entity_id(alias.group(1)) == canonical_subject


def _qualifier_candidates(slot: str, raw_claim: Mapping[str, Any], canonical_subject: str) -> dict[str, Any]:
    value = str(raw_claim.get("value") or "")
    subject = str(raw_claim.get("subject") or "")
    text = f"{subject} {value}"
    version_owner_alias = slot == "config.version" and (
        _has_same_owner_service_alias(subject, canonical_subject)
        or _has_same_owner_service_alias(value, canonical_subject)
    )
    qualifiers: dict[str, Any] = {}
    environment = _environment(text)
    if environment:
        qualifiers["environment"] = environment
    instance = _match_token(_INSTANCE_RE, value) or _match_token(_INSTANCE_RE, subject)
    if instance:
        qualifiers["instance"] = instance
    deployment = _match_token(_DEPLOYMENT_NAME_RE, value) or _match_token(_DEPLOYMENT_NAME_RE, subject)
    if deployment:
        qualifiers["deployment"] = deployment
    platform = _platform(text)
    if platform:
        qualifiers["platform"] = platform

    service = None
    if slot == "state.connectivity":
        service = _match_token(_CONNECTIVITY_SERVICE_RE, value) or _match_token(_CONNECTIVITY_SERVICE_RE, subject)
    service = service or _match_token(_SERVICE_RE, value) or _match_token(_SERVICE_RE, subject)
    if service and not version_owner_alias:
        qualifiers["service"] = service
    process = _match_token(_PROCESS_NAME_RE, value) or _match_token(_PROCESS_NAME_RE, subject)
    if process:
        qualifiers["process"] = process
    if slot == "state.service_health" and "service" not in qualifiers:
        qualifiers["service"] = canonical_subject

    if slot == "config.version" and "instance" not in qualifiers and not version_owner_alias:
        component = _component(value)
        if component and component != qualifiers.get("service"):
            qualifiers["component"] = component
    if slot == "state.job":
        job = _match_token(_JOB_NAME_RE, value) or _match_token(_JOB_NAME_RE, subject)
        if job is None:
            match = re.search(r"(?i)(?:任务|job)\s*[:#=-]?\s*([a-z0-9][a-z0-9_.-]{0,63})", value)
            job = _normalized_token(match.group(1)) if match else None
        if job:
            qualifiers["job"] = job
    return qualifiers


def _coordinate_qualifiers(slot: str, candidates: dict[str, Any]) -> dict[str, Any]:
    # Always call the batch-1 projection API first. Experimental dimensions are
    # overlaid locally and never relax the production slot admission policy.
    projected = coordinate_qualifier_key(slot, candidates)
    for key in _EXPERIMENTAL_QUALIFIER_KEYS.get(slot, ()):
        if key in candidates:
            projected[key] = candidates[key]
    return dict(sorted(projected.items()))


def _coordinate_dict(coordinate: StateCoordinate) -> dict[str, Any]:
    return {
        "namespace": coordinate.namespace,
        "canonical_subject": coordinate.canonical_subject,
        "canonical_slot": coordinate.canonical_slot,
        "coordinate_qualifiers": {
            key: json.loads(frozen_json) for key, frozen_json in coordinate.coordinate_qualifiers
        },
    }


def canonicalize_claim(raw_claim: Mapping[str, Any], *, namespace: str = "default") -> dict[str, Any]:
    """Project one compact claim onto a deterministic candidate state coordinate."""

    subject = str(raw_claim.get("subject") or "").strip()
    value = str(raw_claim.get("value") or "").strip()
    if not subject or not value:
        raise ValueError("raw claim requires non-blank subject and value")
    canonical_subject = _canonical_subject(subject)
    context = _state_context(raw_claim, value)
    if context == "non_asserted":
        return {
            "predicate": _predicate(raw_claim, None),
            "canonical_subject": canonical_subject,
            "canonical_slot": None,
            "coordinate_qualifiers": {},
            "coordinate": None,
            "state_context": context,
            "reason_codes": ["non_current_context"],
        }

    slot = _state_slot(value)
    if slot is None and _subject_has_stable_owner(subject):
        slot = _state_slot(f"{subject} {value}")
    if slot is None:
        return {
            "predicate": _predicate(raw_claim, None),
            "canonical_subject": canonical_subject,
            "canonical_slot": None,
            "coordinate_qualifiers": {},
            "coordinate": None,
            "state_context": "non_state",
            "reason_codes": ["not_state"],
        }
    candidates = _qualifier_candidates(slot, raw_claim, canonical_subject)
    qualifiers = _coordinate_qualifiers(slot, candidates)
    coordinate = StateCoordinate(namespace, canonical_subject, slot, qualifiers)
    reason_codes = [f"state_slot:{slot}"]
    if context == "historical":
        reason_codes.append("historical_context")
    return {
        "predicate": _predicate(raw_claim, slot),
        "canonical_subject": canonical_subject,
        "canonical_slot": slot,
        "coordinate_qualifiers": qualifiers,
        "coordinate": _coordinate_dict(coordinate),
        "state_context": context,
        "reason_codes": reason_codes,
    }


def _claim_fragments(raw_claim: Mapping[str, Any]) -> list[str]:
    value = str(raw_claim.get("value") or "").strip()
    return [fragment.strip() for fragment in _CLAUSE_SEPARATOR_RE.split(value) if fragment.strip()]


def _state_assertion_count(raw_claim: Mapping[str, Any], fragments: Sequence[str]) -> int:
    detected = 0
    for fragment in fragments:
        fragment_claim = {**raw_claim, "value": fragment, "evidence_quote": fragment}
        if _state_context(fragment_claim, fragment) != "non_asserted" and _state_slot(fragment) is not None:
            detected += 1
    return detected


def apply_atomicity_gate(
    raw_claim: Mapping[str, Any],
    *,
    strategy: AtomicityStrategy = "split",
) -> dict[str, Any]:
    """Split or reject compact claims containing more than one current-state assertion."""

    if strategy not in {"split", "reject"}:
        raise ValueError(f"unsupported atomicity strategy: {strategy}")
    original = copy.deepcopy(dict(raw_claim))
    fragments = _claim_fragments(original)
    detected = _state_assertion_count(original, fragments)
    if detected <= 1:
        return {"decision": "atomic", "detected_state_assertions": detected, "claims": [original]}
    if strategy == "reject":
        return {"decision": "rejected", "detected_state_assertions": detected, "claims": []}
    evidence_quote = str(original.get("evidence_quote") or "")
    if any(fragment not in evidence_quote for fragment in fragments):
        return {
            "decision": "rejected",
            "detected_state_assertions": detected,
            "claims": [],
            "reason": "ungrounded_split",
        }
    split_claims = [{**original, "value": fragment, "evidence_quote": fragment} for fragment in fragments]
    return {"decision": "split", "detected_state_assertions": detected, "claims": split_claims}


_COMPACT_FIELDS = {
    "subject",
    "value",
    "kind",
    "confidence",
    "notability",
    "evidence_quote",
    "source_event_indices",
}
_COMPACT_KINDS = {"preference", "architecture", "identity", "config", "fact", "plan", "choice"}


def _validate_compact_claim(claim: Mapping[str, Any], label: str) -> None:
    if set(claim) != _COMPACT_FIELDS:
        raise ValueError(f"{label} violates compact seven-field contract")
    if (
        any(
            not isinstance(claim[field], str) or not claim[field].strip()
            for field in ("subject", "value", "evidence_quote")
        )
        or len(claim["subject"]) > 200
    ):
        raise ValueError(f"{label} violates compact seven-field contract")
    kind = claim["kind"]
    notability = claim["notability"]
    if (
        not isinstance(kind, str)
        or kind not in _COMPACT_KINDS
        or not isinstance(notability, str)
        or notability not in {"high", "medium", "low"}
    ):
        raise ValueError(f"{label} violates compact seven-field contract")
    confidence = claim["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0.0 <= confidence <= 1.0:
        raise ValueError(f"{label} violates compact seven-field contract")
    indices = claim["source_event_indices"]
    if not (
        isinstance(indices, list)
        and 1 <= len(indices) <= 32
        and all(isinstance(index, int) and not isinstance(index, bool) and index >= 0 for index in indices)
    ):
        raise ValueError(f"{label} violates compact seven-field contract")


def _validated_response(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} raw_llm_json must be an object")
    if set(value) != {"claims", "should_memorize"} or not isinstance(value.get("should_memorize"), bool):
        raise ValueError(f"{label} raw_llm_json violates compact response contract")
    claims = value.get("claims")
    if not isinstance(claims, list) or not all(isinstance(claim, Mapping) for claim in claims):
        raise ValueError(f"{label} raw_llm_json.claims must be an object array")
    if len(claims) > 20:
        raise ValueError(f"{label} raw_llm_json allows at most 20 claims")
    for claim_index, claim in enumerate(claims):
        _validate_compact_claim(claim, f"{label}.claims[{claim_index}]")
    return copy.deepcopy(dict(value))


def make_arm_sample(
    corpus_bundle: Mapping[str, Any],
    raw_llm_json: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind a corpus bundle to one frozen extractor response for A/B replay.

    The extractor is deliberately outside this offline post-processing module.
    A and B1 must receive the same returned ``raw_llm_json``; ``bundle_id`` is
    the stable sample/assertion-id prefix shared with the separated gold file.
    """

    bundle_id = str(corpus_bundle.get("bundle_id") or "").strip()
    events = corpus_bundle.get("events")
    if (
        not bundle_id
        or not isinstance(events, list)
        or not events
        or not all(isinstance(event, Mapping) for event in events)
    ):
        raise ValueError("corpus bundle requires a non-blank bundle_id and a non-empty events array")
    return {
        "sample_id": bundle_id,
        "raw_llm_json": _validated_response(raw_llm_json, bundle_id),
    }


def _run_a(sample_id: str, response: dict[str, Any]) -> dict[str, Any]:
    claims = copy.deepcopy(response["claims"])
    return {
        "sample_id": sample_id,
        "arm": "A",
        "raw_llm_json": response,
        "input_claim_count": len(claims),
        "output_claim_count": len(claims),
        "claims": claims,
        "rejections": [],
    }


def _run_b1(
    sample_id: str,
    response: dict[str, Any],
    *,
    namespace: str,
    atomicity_strategy: AtomicityStrategy,
    arm: Literal["B1", "B2"],
) -> dict[str, Any]:
    output_claims: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []
    for claim_index, raw_claim in enumerate(response["claims"]):
        atomicity = apply_atomicity_gate(raw_claim, strategy=atomicity_strategy)
        if atomicity["decision"] == "rejected":
            rejections.append(
                {
                    "source_claim_index": claim_index,
                    "reason": "compound_state_claim",
                    "detected_state_assertions": atomicity["detected_state_assertions"],
                }
            )
            continue
        for atomic_index, claim in enumerate(atomicity["claims"]):
            output_claims.append(
                {
                    "assertion_id": f"{sample_id}:c{claim_index}:a{atomic_index}",
                    "source_claim_index": claim_index,
                    "atomic_index": atomic_index,
                    "atomicity": atomicity["decision"],
                    "claim": claim,
                    "projection": canonicalize_claim(claim, namespace=namespace),
                }
            )
    return {
        "sample_id": sample_id,
        "arm": arm,
        "raw_llm_json": response,
        "input_claim_count": len(response["claims"]),
        "output_claim_count": len(output_claims),
        "claims": output_claims,
        "rejections": rejections,
    }


def run_arm(
    samples: Iterable[Mapping[str, Any]],
    *,
    arm: ArmName,
    namespace: str = "default",
    atomicity_strategy: AtomicityStrategy = "split",
    b2_response_provider: B2ResponseProvider | None = None,
) -> list[dict[str, Any]]:
    """Run an offline arm over raw extractor responses without touching a database."""

    if arm not in {"A", "B1", "B2"}:
        raise ValueError(f"unsupported experiment arm: {arm}")
    if arm == "B2" and b2_response_provider is None:
        raise NotImplementedError("B2 response provider is required; the atomic prompt is not part of batch 2")
    results: list[dict[str, Any]] = []
    for sample_index, sample in enumerate(samples):
        sample_id = str(sample.get("sample_id") or "").strip()
        if not sample_id:
            raise ValueError(f"sample[{sample_index}] requires sample_id")
        response_value = (
            b2_response_provider(sample) if arm == "B2" and b2_response_provider else sample.get("raw_llm_json")
        )
        response = _validated_response(response_value, sample_id)
        if arm == "A":
            results.append(_run_a(sample_id, response))
        else:
            results.append(
                _run_b1(
                    sample_id,
                    response,
                    namespace=namespace,
                    atomicity_strategy=atomicity_strategy,
                    arm=arm,
                )
            )
    return results


def run_arm_file(
    input_path: str | Path,
    output_path: str | Path,
    *,
    arm: ArmName,
    namespace: str = "default",
    atomicity_strategy: AtomicityStrategy = "split",
) -> dict[str, Any]:
    """Run one arm from JSONL to an explicitly supplied experiment output path."""

    source = Path(input_path).resolve()
    target = Path(output_path).resolve()
    samples = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines() if line.strip()]
    results = run_arm(
        samples,
        arm=arm,
        namespace=namespace,
        atomicity_strategy=atomicity_strategy,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        for result in results:
            stream.write(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            stream.write("\n")
    os.replace(temporary, target)
    return {"arm": arm, "samples": len(results), "output": str(target)}
