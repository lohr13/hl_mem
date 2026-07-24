"""兼容仓储入口；新代码应从具体仓储模块导入。"""

import warnings

from hl_mem.storage.claims import ClaimRepository, SupersedeResult
from hl_mem.storage.events import EventRepository
from hl_mem.storage.evidence import DerivationRepository, EvidenceRepository
from hl_mem.storage.experience import ExperienceRepository
from hl_mem.storage.jobs import JobRepository

warnings.warn(
    "hl_mem.storage.repository is deprecated; import from the focused storage modules",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = [
    "ClaimRepository",
    "DerivationRepository",
    "EventRepository",
    "EvidenceRepository",
    "ExperienceRepository",
    "JobRepository",
    "SupersedeResult",
]
