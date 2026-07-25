"""原生图片证据入口的单元测试。"""

from __future__ import annotations

import base64

import pytest

from hl_mem.domain.content import FileTextPart, ImagePart, TextPart, parse_content
from hl_mem.ingest.image_describer import FakeImageDescriber

PNG_1X1 = base64.b64encode(
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
).decode()


def test_image_part_requires_exactly_one_source() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        parse_content({"images": [{"mime_type": "image/png"}]})
    with pytest.raises(ValueError, match="exactly one"):
        parse_content(
            {
                "images": [
                    {
                        "uri": "https://example.test/a.png",
                        "base64_data": PNG_1X1,
                        "mime_type": "image/png",
                    }
                ]
            }
        )


def test_image_validation_rejects_mime_region_and_size() -> None:
    with pytest.raises(ValueError, match="image/"):
        parse_content({"images": [{"base64_data": PNG_1X1, "mime_type": "text/plain"}]})
    with pytest.raises(ValueError, match="normalized"):
        parse_content(
            {"images": [{"base64_data": PNG_1X1, "mime_type": "image/png", "region": [0.8, 0.1, 0.2, 0.9]}]}
        )
    with pytest.raises(ValueError, match="maximum"):
        parse_content(
            {"images": [{"base64_data": PNG_1X1, "mime_type": "image/png"}]},
            image_max_bytes=1,
        )


def test_parse_content_preserves_text_file_image_order() -> None:
    parts = parse_content(
        {
            "text": "hello",
            "files": [{"text": "file body", "filename": "a.txt"}],
            "images": [{"base64_data": PNG_1X1, "mime_type": "image/png"}],
        }
    )
    assert [type(part) for part in parts] == [TextPart, FileTextPart, ImagePart]
    assert parts[-1].to_text() == ""


def test_fake_image_describer_is_deterministic() -> None:
    image = parse_content({"images": [{"base64_data": PNG_1X1, "mime_type": "image/png"}]})[0]
    result = FakeImageDescriber().describe(image, timeout_seconds=1.0)
    assert result.caption == "测试图片描述"
    assert result.ocr_text == "测试 OCR 文本"
