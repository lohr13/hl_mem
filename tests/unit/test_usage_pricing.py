from __future__ import annotations

import importlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from hl_mem.errors import ConfigurationError
from hl_mem.observability.usage import UsageAmount, UsageIdentity
from hl_mem.plugins.contracts import ProviderCapability


def _pricing_module() -> Any:
    try:
        return importlib.import_module("hl_mem.observability.pricing")
    except ModuleNotFoundError as error:
        raise AssertionError("host-owned usage pricing is not implemented") from error


def _rates(**overrides: int) -> dict[str, int]:
    rates = {
        "request": 0,
        "million_input_tokens": 0,
        "million_output_tokens": 0,
        "embedding_item": 0,
        "rerank_document": 0,
        "image": 0,
    }
    rates.update(overrides)
    return rates


def _rule(
    *,
    capability: str = "llm",
    model: str = "qwen",
    provider: str | None = None,
    rates: dict[str, int] | None = None,
) -> dict[str, object]:
    rule: dict[str, object] = {
        "capability": capability,
        "model": model,
        "rates_microunits": rates or _rates(),
    }
    if provider is not None:
        rule["provider"] = provider
    return rule


def _document(*rules: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "currency": "CNY",
        "effective_date": "2026-09-01",
        "source_urls": ["https://pricing.example.test/provider"],
        "rules": list(rules) or [_rule()],
    }


def _write_book(path: Path, document: dict[str, object]) -> Path:
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _load(path: Path) -> Any:
    return _pricing_module().UsagePriceBook.load(path)


def _identity(*, provider: str = "dashscope", model: str = "qwen") -> UsageIdentity:
    return UsageIdentity(ProviderCapability.LLM, "extract", "hl-mem.builtin", provider, model)


def test_price_book_prices_each_positive_unit_upward_without_float_rounding(tmp_path: Path) -> None:
    book = _load(
        _write_book(
            tmp_path / "pricing.json",
            _document(
                _rule(
                    rates=_rates(
                        request=11,
                        million_input_tokens=1_500_001,
                        million_output_tokens=2_500_001,
                    )
                )
            ),
        )
    )
    amount = UsageAmount(requests=1, input_tokens=1_000, output_tokens=2_000, cost_microunits=999)

    priced = book.price(_identity(), amount, phase="reserve")

    assert priced == UsageAmount(requests=1, input_tokens=1_000, output_tokens=2_000, cost_microunits=6_513)


def test_price_book_ceilings_do_not_cancel_between_billable_units(tmp_path: Path) -> None:
    book = _load(
        _write_book(
            tmp_path / "pricing.json",
            _document(
                _rule(
                    rates=_rates(
                        request=1,
                        million_input_tokens=1,
                        million_output_tokens=1,
                        embedding_item=1,
                        rerank_document=1,
                        image=1,
                    )
                )
            ),
        )
    )

    priced = book.price(
        _identity(),
        UsageAmount(
            requests=1,
            input_tokens=1,
            output_tokens=1,
            embedding_items=1,
            rerank_documents=1,
            images=1,
        ),
        phase="settle",
    )

    assert priced.cost_microunits == 6


def test_exact_provider_rule_precedes_generic_and_matching_is_exact(tmp_path: Path) -> None:
    book = _load(
        _write_book(
            tmp_path / "pricing.json",
            _document(
                _rule(rates=_rates(request=10)),
                _rule(provider="dashscope", rates=_rates(request=20)),
            ),
        )
    )

    assert book.price(_identity(), UsageAmount(requests=1), phase="reserve").cost_microunits == 20
    assert (
        book.price(_identity(provider="other"), UsageAmount(requests=1), phase="reserve").cost_microunits == 10
    )
    assert book.price(_identity(model="QWEN"), UsageAmount(requests=1), phase="reserve").cost_microunits is None


def test_unmatched_rule_replaces_only_cost_with_unknown(tmp_path: Path) -> None:
    book = _load(_write_book(tmp_path / "pricing.json", _document(_rule(model="other"))))
    amount = UsageAmount(requests=2, input_tokens=3, output_tokens=4, cost_microunits=55)

    assert book.price(_identity(), amount, phase="settle") == UsageAmount(
        requests=2,
        input_tokens=3,
        output_tokens=4,
        cost_microunits=None,
    )


@pytest.mark.parametrize(
    ("unit", "rate"),
    (
        ("requests", "request"),
        ("input_tokens", "million_input_tokens"),
        ("output_tokens", "million_output_tokens"),
        ("embedding_items", "embedding_item"),
        ("rerank_documents", "rerank_document"),
        ("images", "image"),
    ),
)
def test_positive_rate_for_an_unknown_billable_unit_keeps_cost_unknown(
    tmp_path: Path,
    unit: str,
    rate: str,
) -> None:
    book = _load(
        _write_book(
            tmp_path / f"unknown-{unit}.json",
            _document(_rule(rates=_rates(**{rate: 1}))),
        )
    )
    amount = UsageAmount(**{unit: 7}, unknown_units=frozenset({unit}))

    priced = book.price(_identity(), amount, phase="settle")

    assert priced.cost_microunits is None
    assert priced.unknown_units == frozenset({unit})


def test_zero_rate_for_an_unknown_unit_does_not_hide_known_cost(tmp_path: Path) -> None:
    book = _load(
        _write_book(
            tmp_path / "known-request.json",
            _document(_rule(rates=_rates(request=9))),
        )
    )
    amount = UsageAmount(requests=1, input_tokens=0, unknown_units=frozenset({"input_tokens"}))

    assert book.price(_identity(), amount, phase="reserve").cost_microunits == 9


def test_scaling_unknown_usage_to_zero_returns_exact_additive_identity() -> None:
    amount = UsageAmount(
        requests=1,
        input_tokens=7,
        cost_microunits=None,
        unknown_units=frozenset({"input_tokens", "output_tokens"}),
    )

    assert amount.scale(0) == UsageAmount(cost_microunits=0)


def test_duplicate_exact_rule_identity_is_rejected(tmp_path: Path) -> None:
    path = _write_book(
        tmp_path / "duplicate.json",
        _document(_rule(provider="dashscope"), _rule(provider="dashscope", rates=_rates(request=1))),
    )

    with pytest.raises(ConfigurationError, match=r"duplicate.*llm.*qwen.*dashscope"):
        _load(path)


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (lambda value: value.update(schema_version=2), "schema_version"),
        (lambda value: value.update(currency="USD"), "currency"),
        (lambda value: value.update(effective_date="2026-02-30"), "effective_date"),
        (lambda value: value.update(extra=True), "additional"),
        (lambda value: value.update(rules=[]), "rules"),
        (lambda value: value["rules"][0].update(capability="image"), "capability"),
        (lambda value: value["rules"][0].update(provider="DashScope"), "provider"),
        (lambda value: value["rules"][0].update(model="bad model"), "model"),
        (
            lambda value: value["rules"][0]["rates_microunits"].update(request=-1),
            "request",
        ),
        (
            lambda value: value["rules"][0]["rates_microunits"].update(request=True),
            "request",
        ),
        (
            lambda value: value["rules"][0]["rates_microunits"].pop("image"),
            "image",
        ),
        (lambda value: value.update(source_urls=["http://pricing.example.test"]), "source_urls"),
        (lambda value: value.update(source_urls=["https://"]), "source_urls"),
        (lambda value: value.update(source_urls=["https:///path"]), "source_urls"),
        (lambda value: value.update(source_urls=["https://?q=x"]), "source_urls"),
    ),
)
def test_invalid_price_book_contract_fails_closed(
    tmp_path: Path,
    mutation: Any,
    message: str,
) -> None:
    document = _document(_rule())
    mutation(document)
    path = _write_book(tmp_path / "invalid.json", document)

    with pytest.raises(ConfigurationError, match=message):
        _load(path)


def test_fingerprint_is_canonical_and_repr_does_not_disclose_sources(tmp_path: Path) -> None:
    document = _document(_rule(rates=_rates(request=7)))
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text(json.dumps(document, separators=(",", ":")), encoding="utf-8")
    second.write_text(json.dumps(document, indent=4, sort_keys=True), encoding="utf-8")

    first_book = _load(first)
    second_book = _load(second)

    assert first_book.fingerprint == second_book.fingerprint
    assert len(first_book.fingerprint) == 64
    assert str(first) not in repr(first_book)
    assert "pricing.example.test" not in repr(first_book)


def test_fingerprint_covers_effective_date_and_source_metadata(tmp_path: Path) -> None:
    first = _document(_rule())
    second = deepcopy(first)
    second["source_urls"] = ["https://pricing.example.test/revised"]

    assert _load(_write_book(tmp_path / "first.json", first)).fingerprint != _load(
        _write_book(tmp_path / "second.json", second)
    ).fingerprint


def test_missing_or_malformed_price_book_fails_with_config_context(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match=r"price book.*does not exist"):
        _load(tmp_path / "missing.json")

    malformed = tmp_path / "malformed.json"
    malformed.write_text("{", encoding="utf-8")
    with pytest.raises(ConfigurationError, match=r"price book.*invalid JSON"):
        _load(malformed)


def test_published_json_schema_accepts_the_canonical_book_and_rejects_extra_fields() -> None:
    schema_path = Path(__file__).resolve().parents[2] / "docs" / "usage-pricing.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    document = _document(_rule())

    assert list(validator.iter_errors(document)) == []
    document["rules"][0]["rates_microunits"]["unexpected"] = 1
    assert any(error.validator == "additionalProperties" for error in validator.iter_errors(document))


def test_published_schema_is_generated_from_the_frozen_runtime_v1_contract() -> None:
    try:
        from scripts.check_usage_pricing_schema import rendered_schema
    except ModuleNotFoundError as error:
        raise AssertionError("usage pricing schema has no single generated source") from error

    schema_path = Path(__file__).resolve().parents[2] / "docs" / "usage-pricing.schema.json"
    schema = json.loads(rendered_schema())

    assert schema_path.read_text(encoding="utf-8") == rendered_schema()
    assert schema["properties"]["rules"]["items"]["properties"]["capability"]["enum"] == [
        "llm",
        "embedding",
        "reranker",
        "image_describer",
    ]
