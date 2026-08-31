from __future__ import annotations

import base64
import socket
from pathlib import Path

import httpx
import pytest

from hl_mem.domain.content import ImagePart
from hl_mem.security.image_input import ImageInputError, ImageInputGuard

PNG = b"\x89PNG\r\n\x1a\n" + b"safe-image-bytes"


def _public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )


def test_base64_is_strictly_decoded_bounded_and_hashed() -> None:
    guard = ImageInputGuard(64, False, ())
    image = ImagePart(None, base64.b64encode(PNG).decode(), "image/png")

    validated = guard.materialize(image)

    assert validated.data == PNG
    assert validated.media_type == "image/png"
    assert len(validated.sha256) == 64
    with pytest.raises(ImageInputError, match="base64"):
        guard.materialize(ImagePart(None, "not base64!", "image/png"))
    with pytest.raises(ImageInputError, match="maximum"):
        ImageInputGuard(4, False, ()).materialize(image)


def test_declared_hash_must_match_materialized_bytes() -> None:
    image = ImagePart(None, base64.b64encode(PNG).decode(), "image/png", sha256="0" * 64)
    with pytest.raises(ImageInputError, match="sha256"):
        ImageInputGuard(64, False, ()).materialize(image)


def test_file_uri_is_confined_and_mime_checked(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    image_path = allowed / "evidence.png"
    image_path.write_bytes(PNG)
    guard = ImageInputGuard(64, True, (allowed,))

    assert guard.materialize(ImagePart(image_path.as_uri(), None, "image/png")).data == PNG

    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG)
    with pytest.raises(ImageInputError, match="allow roots"):
        guard.materialize(ImagePart(outside.as_uri(), None, "image/png"))

    wrong_extension = allowed / "evidence.jpg"
    wrong_extension.write_bytes(PNG)
    with pytest.raises(ImageInputError, match="extension"):
        guard.materialize(ImagePart(wrong_extension.as_uri(), None, "image/png"))


def test_file_symlink_cannot_escape_allow_root(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG)
    link = allowed / "link.png"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable on this Windows host")

    with pytest.raises(ImageInputError, match="allow roots"):
        ImageInputGuard(64, True, (allowed,)).materialize(ImagePart(link.as_uri(), None, "image/png"))


@pytest.mark.parametrize(
    "uri",
    [
        "https://127.0.0.1/a.png",
        "https://[::1]/a.png",
        "https://169.254.169.254/latest/meta-data",
    ],
)
def test_private_literal_is_rejected_before_fetch(uri: str) -> None:
    called = False

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, request=request, content=PNG)

    client = httpx.Client(transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(ImageInputError, match="public"):
            ImageInputGuard(64, False, (), client=client).materialize(ImagePart(uri, None, "image/png"))
        assert not called
    finally:
        client.close()


def test_mixed_public_private_dns_answer_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", 443)),
        ],
    )
    with pytest.raises(ImageInputError, match="public"):
        ImageInputGuard(64, False, ()).materialize(ImagePart("https://public.test/a.png", None, "image/png"))


def test_redirect_target_is_validated_before_second_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    _public_dns(monkeypatch)
    requested: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(302, request=request, headers={"Location": "https://127.0.0.1/private.png"})

    client = httpx.Client(transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(ImageInputError, match="public"):
            ImageInputGuard(64, False, (), client=client).materialize(
                ImagePart("https://public.test/a.png", None, "image/png")
            )
        assert requested == ["https://public.test/a.png"]
    finally:
        client.close()


def test_redirect_limit_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    _public_dns(monkeypatch)

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, request=request, headers={"Location": "/again.png"})

    client = httpx.Client(transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(ImageInputError, match="redirect"):
            ImageInputGuard(64, False, (), client=client, max_redirects=1).materialize(
                ImagePart("https://public.test/a.png", None, "image/png")
            )
    finally:
        client.close()


def test_https_stream_size_content_type_and_magic_are_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    _public_dns(monkeypatch)
    payloads = [
        (b"x" * 65, "image/png", "maximum"),
        (PNG, "image/jpeg", "Content-Type"),
        (b"not-an-image", "image/png", "magic"),
    ]
    for content, content_type, message in payloads:
        client = httpx.Client(
            transport=httpx.MockTransport(
                lambda request, body=content, mime=content_type: httpx.Response(
                    200,
                    request=request,
                    headers={"Content-Type": mime},
                    content=body,
                )
            )
        )
        try:
            with pytest.raises(ImageInputError, match=message):
                ImageInputGuard(64, False, (), client=client).materialize(
                    ImagePart("https://public.test/a.png", None, "image/png")
                )
        finally:
            client.close()


def test_https_success_returns_bytes_not_locator(monkeypatch: pytest.MonkeyPatch) -> None:
    _public_dns(monkeypatch)
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                request=request,
                headers={"Content-Type": "image/png; charset=binary"},
                content=PNG,
            )
        )
    )
    try:
        validated = ImageInputGuard(64, False, (), client=client).materialize(
            ImagePart("https://public.test/a.png", None, "image/png")
        )
        assert validated.data == PNG
        assert not hasattr(validated, "uri")
    finally:
        client.close()
