"""Compatibility imports for extraction response schemas.

The canonical implementation lives in :mod:`hl_mem.ingest.extraction.schema`.
"""

from .extraction.schema import (
    CanonicalSlot,
    CompactExtractedClaimSchema,
    CompactExtractionResponseSchema,
    ExtractedClaimSchema,
    ExtractionResponseSchema,
    TopicTag,
    extraction_response_json_schema,
    legacy_extraction_response_json_schema,
    source_bounded_rao_extraction_response_json_schema,
    temporal_gate_extraction_response_json_schema,
)

__all__ = [
    "CanonicalSlot",
    "CompactExtractedClaimSchema",
    "CompactExtractionResponseSchema",
    "ExtractedClaimSchema",
    "ExtractionResponseSchema",
    "TopicTag",
    "extraction_response_json_schema",
    "legacy_extraction_response_json_schema",
    "source_bounded_rao_extraction_response_json_schema",
    "temporal_gate_extraction_response_json_schema",
]
