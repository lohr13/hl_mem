"""Compatibility imports for deterministic extraction repair.

The canonical implementation lives in :mod:`hl_mem.ingest.extraction.repair`.
"""

from .extraction.repair import (
    ENUM_MAPPINGS,
    SENSITIVITY_ZH_TO_EN,
    TOPIC_TAG_ZH_TO_EN,
    repair_extraction_json,
)

__all__ = [
    "ENUM_MAPPINGS",
    "SENSITIVITY_ZH_TO_EN",
    "TOPIC_TAG_ZH_TO_EN",
    "repair_extraction_json",
]
