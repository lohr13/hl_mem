# 原生图片证据入口实现方案

## 目标与边界

让 event 原生接收 image URI 或 base64，经可插拔视觉 provider 生成 caption/OCR，再沿用文本 Claim 提取管线。Claim 仍只保存结构事实；原图定位符、描述结果和模型元数据保存在 event content 中并通过既有 `evidence_links` 回指。

默认关闭，不新增 Python 硬依赖，不把图片二进制复制到 Claim、SQLite 新表或 embedding 中。默认视觉配置使用百炼 Coding Plan 的 **qwen3.7-plus**（多模态模型，Vision Arena 全球前五），走 Coding Plan AK + `coding.dashscope.aliyuncs.com/v1` 端点；也可切换为智谱 Coding Plan 的 GLM-5T。provider 可插拔，未来换其他视觉模型只需加一个 provider 实现。

## 现状与集成点

- `src/hl_mem/domain/content.py::ContentPart/parse_content()` 当前实现 `TextPart`、`FileTextPart`，是图片扩展边界。
- `src/hl_mem/application/ingest.py::IngestService.ingest_event()` 将 content 原样持久化并排 `extract_event` job。
- `src/hl_mem/workers/worker.py::_handle_extract()` 读取 event 后选择 extractor；图片描述应发生在文本 extractor 前。
- `src/hl_mem/ingest/llm_extractor.py::LLMExtractor.extract()` 消费结构化 content 的文本化结果。
- `src/hl_mem/llm/providers.py`、`http_utils.py` 已有 compatible provider、httpx、retry/timeout 和具体错误处理模式。
- `storage/events.py` 保存 `content_json` 与 `source_uri`；`evidence_links` 已把 Claim 指回 event。

## 协议与类型

在 `src/hl_mem/protocols.py` 增加：

```python
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ImageLocator:
    """图片在原始证据中的稳定定位信息。"""

    uri: str | None
    media_type: str
    sha256: str | None
    page: int | None = None
    region: tuple[float, float, float, float] | None = None


@dataclass(frozen=True)
class ImageDescription:
    """视觉模型返回的可审计派生文本。"""

    caption: str
    ocr_text: str
    model: str
    confidence: float | None
    locator: ImageLocator


class ImageDescriberProtocol(Protocol):
    """把图片证据转成 caption/OCR，不直接生成 Claim。"""

    def describe(
        self,
        image: "ImagePart",
        *,
        timeout_seconds: float,
    ) -> ImageDescription: ...
```

在 `src/hl_mem/domain/content.py` 增加：

```python
@dataclass(frozen=True)
class ImagePart:
    """URI 或 base64 图片内容；两种来源必须且只能提供一种。"""

    uri: str | None
    base64_data: str | None
    mime_type: str
    sha256: str | None = None
    page: int | None = None
    region: tuple[float, float, float, float] | None = None

    def source_uri(self) -> str | None: ...
    def to_text(self) -> str:
        return ""  # 描述前不伪装成文本
```

`parse_content()` 支持：

```json
{
  "images": [{
    "uri": "file:///.../receipt.png",
    "mime_type": "image/png",
    "sha256": "...",
    "page": 1,
    "region": [0.1, 0.2, 0.8, 0.9]
  }]
}
```

或 `base64_data`。解析时拒绝同时提供/同时缺少 URI 与 base64、非 `image/*` MIME、非法 region、超过配置字节上限的 base64。

## Provider

新建 `src/hl_mem/ingest/image_describer.py::DashScopeImageDescriber`。使用现有 httpx/retry helper 调用：

```text
POST {HL_MEM_IMAGE_DESCRIBER_BASE_URL}/chat/completions
Authorization: Bearer {IMAGE_API_KEY}
model: {HL_MEM_IMAGE_DESCRIBER_MODEL}
```

默认环境配置：

```text
HL_MEM_IMAGE_DESCRIBER_MODE=off
HL_MEM_IMAGE_DESCRIBER_PROVIDER=dashscope
HL_MEM_IMAGE_DESCRIBER_BASE_URL=https://coding.dashscope.aliyuncs.com/v1
HL_MEM_IMAGE_DESCRIBER_MODEL=qwen3.7-plus
IMAGE_API_KEY=<百炼 Coding Plan AK（复用 LLM_API_KEY）>
HL_MEM_IMAGE_DESCRIBER_TIMEOUT_SECONDS=20
HL_MEM_IMAGE_MAX_BYTES=10485760
HL_MEM_IMAGE_MAX_PARTS=4
```

模型、端点、timeout 和大小均从 Settings 注入，源码不硬编码运行时选择。默认使用百炼 Coding Plan 的 qwen3.7-plus（多模态），复用 `LLM_API_KEY`（Coding Plan AK）；也可配置为智谱 GLM-5T（走智谱 Coding Plan 端点）。工厂在 mode=on 时校验 API key、HTTPS base URL 和视觉 model 非空。

请求 content part 使用 provider 支持的 `image_url`：`https:` URI 原样传递；base64 组装 `data:<mime>;base64,...`；允许的 `file:` URI 在完成 real-path allow-root、大小和 MIME/magic 校验后由本地读取并转换成 data URL，绝不把 `file:` 地址发给 DashScope。system prompt 要求 JSON：

```json
{"caption":"客观描述","ocr_text":"逐行 OCR 文本","confidence":0.0}
```

confidence 若 provider 未给出可靠标定则写 `null`，禁止从措辞猜数值。响应解析限制 caption/OCR 字符数，记录 HTTP status、request id、model、tokens 和 latency；外部调用使用既有 retry + timeout，不吞异常。

## 摄入数据流

1. `ingest_event()` 验证 ImagePart 结构，但只持久化原 event 并快速返回。
2. `_handle_extract()` 读取 event；mode=off 且含图片时忽略图片，只处理已有 text。
3. mode=on 时逐图调用 describer，生成 `ImageDescription`。
4. 为每张结果创建不可变 `event_type='image_description'` 派生 event，content 中保存原 event ID、图片 index、locator 和描述；幂等键防止并发重复。
5. 构造给 extractor 的派生文本：

```text
<image_evidence index="0" uri_hash="...">
[caption]
...
[ocr]
...
</image_evidence>
```

6. `LLMExtractor` 正常生成 Claim；`store_extracted()` 在同一 Claim 上写两类 evidence link：原图片 event 为主要 `derived_from` 证据，image_description event 为带模型元数据的 `supports` 证据。

派生描述采用新 event，而不 UPDATE 原 event，保持事件溯源的物理不可变性。description event 的 `occurred_at` 继承原 event，`recorded_at` 使用模型完成时间，因而双时间语义明确。

幂等键为 `image-describe:<event_id>:<image_sha_or_uri_hash>:<model>`。若无 sha256，读取 base64 后计算；远程 URI 不下载计算 hash，以规范化 URI hash 标识。重复 worker 使用已存在且 model 相同的描述，不再次计费。

## 安全与失败策略

- 默认只允许 `https:`、`data:` 和明确配置允许的 `file:`；禁止 `http:`、loopback、link-local 和云 metadata 地址，防止 SSRF。
- `file:` 必须位于配置的 allow roots，解析 real path 后检查；API 服务默认不允许 file URI。
- base64 解码后验证 magic/MIME 与大小，日志和 trace 永不写 base64。
- 某张图失败：记录 job error detail；若 event 还有文本，可继续文本提取并标注 partial；纯图片 event 则 job retry，耗尽后 failed，不能生成空 Claim。
- caption/OCR 视作不可信证据文本，prompt 中明确包裹，防止图片内 prompt injection 成为系统指令。

## 持久化与 migration

不新增 migration。原 event content 约定：

```json
{
  "images": [{"uri": "...", "mime_type": "image/png", "sha256": "..."}]
}
```

派生 description event content 约定：

```json
{
  "source_event_id": "...",
  "image_index": 0,
  "caption": "...",
  "ocr_text": "...",
  "model": "qwen-vl-max",
  "confidence": null,
  "locator": {"uri": "...", "media_type": "image/png", "sha256": "..."}
}
```

原图 bytes 不进入 SQLite；base64 是调用方显式提交时原 event content 的一部分，API 层按大小上限接收。证据定位为 Claim → 原 event/image index，并可沿 Claim 的 supports evidence link 读取 description event。

## 文件变更

- 修改 `domain/content.py`、`protocols.py`。
- 新建 `ingest/image_describer.py`。
- 修改 `settings.py`、`components.py`、`workers/worker.py::_handle_extract`。
- 修改 `storage/events.py`，增加幂等插入 image_description event 的 repository 方法。
- 修改 API/MCP schema，使 images 字段透传并校验。

## 测试计划

- ImagePart：URI/base64 互斥、MIME、region、大小、hash、parse_content 混合顺序。
- provider：http payload 与默认 endpoint/model、JSON 解析、null confidence、retry/timeout、401/429/5xx 错误信息。
- 安全：SSRF 地址、越界 file URI、MIME 欺骗、日志不含 base64、OCR prompt injection 被当作数据。
- worker：off 零视觉调用；on 描述后提取；混合内容 partial；纯图失败 retry；幂等不重复计费。
- evidence：Claim 仅有结构事实，evidence link 指向原 event，locator 能定位到对应图片和描述。
- 回归：纯文本和 file text 事件行为不变，无真实网络调用。

## 验收标准

- 默认关闭且不需要视觉 API key。
- 默认 on 配置明确使用百炼通用 AK + `qwen-vl-max`，provider 可替换。
- 图片内容可形成 Claim，但 Claim 不复制原图或描述元数据，证据链可完整回指。
