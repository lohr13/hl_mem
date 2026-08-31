"""多模态事件内容的文本化协议与实现。"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from typing import Any, Protocol


class ContentPart(Protocol):
    """可供提取器统一文本化的内容部分。"""

    mime_type: str

    def to_text(self) -> str: ...

    def source_uri(self) -> str | None: ...


class TextPart:
    """纯文本内容。"""

    def __init__(self, text: str) -> None:
        self.text = text
        self.mime_type = "text/plain"

    def to_text(self) -> str:
        """返回原始文本。"""
        return self.text

    def source_uri(self) -> str | None:
        """纯文本没有来源 URI。"""
        return None


class FileTextPart:
    """从文件提取的文本内容。"""

    def __init__(self, text: str, filename: str, source_uri: str | None = None) -> None:
        self.text = text
        self.filename = filename
        self.mime_type = "text/plain"
        self._source_uri = source_uri

    def to_text(self) -> str:
        """返回包含文件名标记的文本。"""
        return f"[file: {self.filename}]\n{self.text}"

    def source_uri(self) -> str | None:
        """返回文件来源 URI。"""
        return self._source_uri


@dataclass(frozen=True)
class ImagePart:
    """URI 或 base64 图片内容；两种来源必须且只能提供一种。"""

    uri: str | None
    base64_data: str | None
    mime_type: str
    sha256: str | None = None
    page: int | None = None
    region: tuple[float, float, float, float] | None = None

    def __post_init__(self) -> None:
        """校验图片来源、MIME 与定位区域。"""
        if (self.uri is None) == (self.base64_data is None):
            raise ValueError("image must provide exactly one of uri or base64_data")
        if not self.mime_type.startswith("image/"):
            raise ValueError("image mime_type must start with image/")
        if self.page is not None and self.page < 0:
            raise ValueError("image page must be a non-negative integer")
        if self.region is not None:
            if len(self.region) != 4:
                raise ValueError("image region must contain four coordinates")
            x1, y1, x2, y2 = self.region
            if not (0.0 <= x1 < x2 <= 1.0 and 0.0 <= y1 < y2 <= 1.0):
                raise ValueError("image region must be normalized and ordered")

    def source_uri(self) -> str | None:
        """返回图片来源 URI。"""
        return self.uri

    def to_text(self) -> str:
        """描述前不把图片伪装成文本。"""
        return ""


def _parse_image(raw: dict[str, Any], max_bytes: int) -> ImagePart:
    uri = raw.get("uri")
    base64_data = raw.get("base64_data")
    mime_type = str(raw.get("mime_type", ""))
    if base64_data is not None:
        encoded = str(base64_data)
        max_encoded = ((max_bytes + 2) // 3) * 4
        if len(encoded) > max_encoded:
            raise ValueError(f"image exceeds maximum size of {max_bytes} bytes")
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ValueError("image base64_data is invalid") from error
        if len(decoded) > max_bytes:
            raise ValueError(f"image exceeds maximum size of {max_bytes} bytes")
    raw_region = raw.get("region")
    region = None
    if raw_region is not None:
        if not isinstance(raw_region, (list, tuple)) or len(raw_region) != 4:
            raise ValueError("image region must contain four coordinates")
        region = (
            float(raw_region[0]),
            float(raw_region[1]),
            float(raw_region[2]),
            float(raw_region[3]),
        )
        x1, y1, x2, y2 = region
        if not (0.0 <= x1 < x2 <= 1.0 and 0.0 <= y1 < y2 <= 1.0):
            raise ValueError("image region must be normalized and ordered")
    page = raw.get("page")
    if page is not None and (not isinstance(page, int) or page < 0):
        raise ValueError("image page must be a non-negative integer")
    return ImagePart(
        uri=str(uri) if uri is not None else None,
        base64_data=str(base64_data) if base64_data is not None else None,
        mime_type=mime_type,
        sha256=str(raw["sha256"]) if raw.get("sha256") is not None else None,
        page=page,
        region=region,
    )


def parse_content(
    content: dict[str, Any] | str,
    *,
    image_max_bytes: int = 10 * 1024 * 1024,
    image_max_parts: int = 4,
) -> list[TextPart | FileTextPart | ImagePart]:
    """从事件 content 中解析可供提取器消费的内容部分。"""
    if isinstance(content, str):
        return [TextPart(content)]
    parts: list[TextPart | FileTextPart | ImagePart] = []
    if text := content.get("text"):
        parts.append(TextPart(str(text)))
    files = content.get("files")
    if isinstance(files, list):
        for file_part in files:
            if isinstance(file_part, dict) and file_part.get("text"):
                parts.append(
                    FileTextPart(
                        str(file_part["text"]),
                        str(file_part.get("filename", "unknown")),
                        str(file_part["uri"]) if file_part.get("uri") is not None else None,
                    )
                )
    images = content.get("images")
    if isinstance(images, list):
        if len(images) > image_max_parts:
            raise ValueError(f"content contains more than {image_max_parts} images")
        for image in images:
            if not isinstance(image, dict):
                raise ValueError("each image must be an object")
            parts.append(_parse_image(image, image_max_bytes))
    return parts or [TextPart(str(content))]
