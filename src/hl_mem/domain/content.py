"""多模态事件内容的文本化协议与实现。"""

from __future__ import annotations

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


def parse_content(content: dict[str, Any] | str) -> list[TextPart | FileTextPart]:
    """从事件 content 中解析可供提取器消费的内容部分。"""
    if isinstance(content, str):
        return [TextPart(content)]
    parts: list[TextPart | FileTextPart] = []
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
    return parts or [TextPart(str(content))]
