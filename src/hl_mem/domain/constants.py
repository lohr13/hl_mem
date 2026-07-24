"""领域常量：分类标签、意图关键词等。"""

# Intent 检测关键词（domain/recall.py 使用）
INTENT_KEYWORDS_PROCEDURAL = ("如何", "怎么", "步骤", "流程", "部署")
INTENT_KEYWORDS_HISTORICAL = ("去年", "以前", "历史", "当时", "曾经")
INTENT_KEYWORDS_RELATIONAL = ("关系", "关联", "依赖", "属于")
INTENT_KEYWORDS_ANALOGICAL = ("类似", "经验", "上次")
INTENT_KEYWORDS_PREFERENCE = ("偏好", "喜欢", "喜好", "习惯")
INTENT_KEYWORDS_AS_OF = ("当时", "以前", "历史", "曾经", "截至")

# Predicate 分类（domain/claims/conflicts.py 使用）
PREDICATE_PREFERENCE = "偏好"
PREDICATE_STATE = "状态"

# 默认 subject（application/ingest.py 使用）
DEFAULT_SUBJECT = "用户"
