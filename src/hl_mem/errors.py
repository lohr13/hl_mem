"""HL-Mem 应用异常层级。"""


class HlMemError(Exception):
    """HL-Mem 应用异常基类。"""


class NotFoundError(HlMemError, ValueError):
    """资源不存在。"""


class ValidationError(HlMemError, ValueError):
    """输入验证失败。"""


class ConflictError(HlMemError):
    """状态冲突，例如非法状态转换。"""


class ConfigurationError(HlMemError, RuntimeError):
    """应用配置错误。"""


class ExternalServiceError(HlMemError, RuntimeError):
    """外部服务调用失败。"""


class LLMOutputTruncatedError(ExternalServiceError):
    """LLM 响应因 token 限制而截断。"""


class LLMSchemaValidationError(ExternalServiceError, ValueError):
    """LLM 输出在内容级重试后仍不符合 schema。"""


class LLMStructuredOutputUnsupportedError(ExternalServiceError):
    """LLM provider 不支持请求的结构化输出模式。"""
