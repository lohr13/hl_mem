"""领域常量：分类标签、意图关键词等。"""

# Intent 检测关键词（domain/recall.py 使用）
INTENT_KEYWORDS_PROCEDURAL = ("如何", "怎么", "步骤", "流程", "部署")
INTENT_KEYWORDS_HISTORICAL = ("去年", "以前", "历史", "当时", "曾经")
INTENT_KEYWORDS_RELATIONAL = ("关系", "关联", "依赖", "属于")
INTENT_KEYWORDS_ANALOGICAL = ("类似", "经验", "上次")
INTENT_KEYWORDS_PREFERENCE = ("偏好", "喜欢", "喜好", "习惯")
INTENT_KEYWORDS_AS_OF = ("当时", "以前", "历史", "曾经", "截至")
TOOL_KEYWORDS = (
    "用什么工具",
    "哪个工具",
    "哪些工具",
    "工具",
    "tool",
    "command",
    "命令",
    "接口",
    "api",
    "插件",
)
PROCEDURE_KEYWORDS = (
    "怎么做",
    "怎么",
    "如何",
    "how to",
    "步骤",
    "流程",
    "部署",
    "安装",
    "配置",
    "排障",
    "上次怎么",
    "照上次",
)
PROCEDURE_ACTION_KEYWORDS = (
    "做",
    "部署",
    "安装",
    "配置",
    "排障",
    "执行",
    "运行",
    "处理",
)

# Predicate 分类（domain/claims/conflicts.py 使用）
PREDICATE_PREFERENCE = "偏好"
PREDICATE_STATE = "状态"

# 默认 subject（application/ingest.py 使用）
DEFAULT_SUBJECT = "用户"
