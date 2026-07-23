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
