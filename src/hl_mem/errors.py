"""HL-Mem 应用异常层级。"""


class HlMemError(Exception):
    """HL-Mem 应用异常基类。"""


class NotFoundError(HlMemError, ValueError):
    """资源不存在。"""


class ValidationError(HlMemError, ValueError):
    """输入验证失败。"""


class ConflictError(HlMemError):
    """状态冲突，例如非法状态转换。"""


class ActiveClaimInvariantError(ConflictError):
    """一次 mutation 会在互斥 conflict 组中产生多个 active claim。"""


class ConflictResolutionError(ConflictError, ValueError):
    """人工冲突裁决不满足组级业务约束。"""


class ConfigurationError(HlMemError, RuntimeError):
    """应用配置错误。"""


class ProviderPluginError(ConfigurationError):
    """Provider 插件配置或加载失败。"""


class PluginManifestError(ProviderPluginError, ValueError):
    """Provider 插件清单无效。"""


class PluginCompatibilityError(ProviderPluginError):
    """Provider 插件与宿主版本不兼容。"""


class PluginConflictError(ProviderPluginError):
    """多个 Provider 插件声明了相同能力。"""


class ProviderNotFoundError(ProviderPluginError):
    """配置引用了未注册的 Provider。"""


class UsageGovernanceError(ConfigurationError):
    """Provider 用量治理失败。"""


class UsageLimitExceededError(UsageGovernanceError):
    """一次用量预留将超过已配置限制。"""


class UsageReservationError(UsageGovernanceError):
    """用量预留不存在、状态无效或发生矛盾终结。"""


class OpsReportError(UsageGovernanceError):
    """A read-only usage-ledger report could not be produced safely."""


class ExternalServiceError(HlMemError, RuntimeError):
    """外部服务调用失败。"""


class ProviderCallError(ExternalServiceError):
    """经宿主治理的 Provider 调用失败。"""

    def __init__(
        self,
        category: str,
        message: str,
        *,
        attempts: int,
        sent: bool,
        http_status: int | None = None,
        provider_code: str | None = None,
        request_id: str | None = None,
        response_body: object | None = None,
    ) -> None:
        if attempts < 1:
            raise ValueError("provider call attempts must be at least 1")
        super().__init__(message[:512])
        self.category = category
        self.attempts = attempts
        self.sent = sent
        self.http_status = http_status
        self.provider_code = provider_code
        self.request_id = request_id
        self.response_body = response_body


class LLMOutputTruncatedError(ExternalServiceError):
    """LLM 响应因 token 限制而截断。"""


class LLMSchemaValidationError(ExternalServiceError, ValueError):
    """LLM 输出在内容级重试后仍不符合 schema。"""


class LLMStructuredOutputUnsupportedError(ExternalServiceError):
    """LLM provider 不支持请求的结构化输出模式。"""
