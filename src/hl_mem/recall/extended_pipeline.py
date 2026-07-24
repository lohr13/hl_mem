"""兼容的召回辅助函数导出；实现位于正式生产管线。"""

# Deprecated compatibility surface: tests and downstream callers still import
# this module. New code should import from application.recall/recall_pipeline.

from hl_mem.application.recall import budget_pack
from hl_mem.recall.recall_pipeline import reciprocal_rank_fusion

__all__ = ["budget_pack", "reciprocal_rank_fusion"]
