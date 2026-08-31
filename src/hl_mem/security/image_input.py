"""Host-owned materialization boundary for untrusted image inputs."""

from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import mimetypes
import socket
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit
from urllib.request import url2pathname

import httpx

from hl_mem.domain.content import ImagePart
from hl_mem.plugins.contracts import ValidatedImageInput

_MAGIC_TYPES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),
)
_ALLOWED_MEDIA_TYPES = frozenset(media_type for _prefix, media_type in _MAGIC_TYPES)
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class ImageInputError(ValueError):
    """An image source failed host security validation."""


def _detected_media_type(data: bytes) -> str | None:
    for prefix, media_type in _MAGIC_TYPES:
        if data.startswith(prefix) and (media_type != "image/webp" or data[8:12] == b"WEBP"):
            return media_type
    return None


def _resolved_addresses(hostname: str, port: int) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    try:
        return (ipaddress.ip_address(hostname),)
    except ValueError:
        try:
            records = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        except socket.gaierror as error:
            raise ImageInputError(f"image URI host cannot be resolved: {hostname}") from error
        addresses = tuple({ipaddress.ip_address(record[4][0]) for record in records})
        if not addresses:
            raise ImageInputError(f"image URI host cannot be resolved: {hostname}")
        return addresses


def _validate_public_https_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ImageInputError("remote image URI must use HTTPS and include a host")
    if parsed.username is not None or parsed.password is not None:
        raise ImageInputError("remote image URI must not contain credentials")
    try:
        port = parsed.port or 443
    except ValueError as error:
        raise ImageInputError("remote image URI port is invalid") from error
    addresses = _resolved_addresses(parsed.hostname, port)
    if any(not address.is_global for address in addresses):
        raise ImageInputError("image URI must resolve only to public addresses")


def _validate_materialized(data: bytes, declared_media_type: str, max_bytes: int) -> str:
    normalized = declared_media_type.casefold().strip()
    if normalized not in _ALLOWED_MEDIA_TYPES:
        raise ImageInputError(f"unsupported image MIME type: {declared_media_type}")
    if len(data) > max_bytes:
        raise ImageInputError(f"image exceeds maximum size of {max_bytes} bytes")
    detected = _detected_media_type(data)
    if detected is None or detected != normalized:
        raise ImageInputError(f"image magic does not match declared MIME {declared_media_type}")
    return normalized


def _check_declared_hash(image: ImagePart, data: bytes) -> str:
    computed = hashlib.sha256(data).hexdigest()
    if image.sha256 is not None and image.sha256.casefold() != computed:
        raise ImageInputError("image sha256 does not match materialized bytes")
    return computed


def _bounded_join(chunks: Iterable[bytes], max_bytes: int) -> bytes:
    data = bytearray()
    for chunk in chunks:
        remaining = max_bytes + 1 - len(data)
        if remaining <= 0:
            break
        data.extend(chunk[:remaining])
        if len(data) > max_bytes:
            break
    if len(data) > max_bytes:
        raise ImageInputError(f"image exceeds maximum size of {max_bytes} bytes")
    return bytes(data)


class ImageInputGuard:
    """Materialize ImagePart into bounded bytes before Provider code runs."""

    def __init__(
        self,
        max_bytes: int,
        allow_file_uris: bool,
        file_allow_roots: tuple[Path, ...],
        client: httpx.Client | None = None,
        max_redirects: int = 3,
    ) -> None:
        if max_bytes < 1:
            raise ValueError("image max_bytes must be positive")
        if max_redirects < 0:
            raise ValueError("image max_redirects must be non-negative")
        self.max_bytes = max_bytes
        self.allow_file_uris = allow_file_uris
        self.file_allow_roots = tuple(Path(root).resolve() for root in file_allow_roots)
        self.max_redirects = max_redirects
        self._client = client or httpx.Client(follow_redirects=False)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()
            self._owns_client = False

    def materialize(self, image: ImagePart) -> ValidatedImageInput:
        if image.base64_data is not None:
            data = self._decode_base64(image.base64_data)
        else:
            parsed = urlsplit(image.uri or "")
            if parsed.scheme.lower() == "file":
                data = self._read_file(image, parsed.netloc, parsed.path)
            elif parsed.scheme.lower() == "https":
                data = self._download_https(image)
            else:
                raise ImageInputError("image URI scheme must be HTTPS or an allowed file URI")
        media_type = _validate_materialized(data, image.mime_type, self.max_bytes)
        return ValidatedImageInput(data, media_type, _check_declared_hash(image, data))

    def _decode_base64(self, encoded: str) -> bytes:
        max_encoded = ((self.max_bytes + 2) // 3) * 4
        if len(encoded) > max_encoded:
            raise ImageInputError(f"image exceeds maximum size of {self.max_bytes} bytes")
        try:
            return base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ImageInputError("image base64_data is invalid") from error

    def _read_file(self, image: ImagePart, netloc: str, raw_path: str) -> bytes:
        if not self.allow_file_uris:
            raise ImageInputError("file image URIs are disabled")
        if netloc and netloc.casefold() != "localhost":
            raise ImageInputError("network file image URIs are forbidden")
        try:
            path = Path(url2pathname(unquote(raw_path))).resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise ImageInputError("file image URI cannot be resolved") from error
        if not any(path == root or root in path.parents for root in self.file_allow_roots):
            raise ImageInputError("file image URI is outside configured allow roots")
        guessed = mimetypes.guess_type(path.name)[0]
        if guessed is not None and guessed.casefold() != image.mime_type.casefold():
            raise ImageInputError("file extension MIME does not match declared MIME")
        try:
            with path.open("rb") as stream:
                return _bounded_join(iter(lambda: stream.read(64 * 1024), b""), self.max_bytes)
        except OSError as error:
            raise ImageInputError("file image URI cannot be read") from error

    def _download_https(self, image: ImagePart) -> bytes:
        url = image.uri or ""
        redirects = 0
        while True:
            _validate_public_https_url(url)
            try:
                request = self._client.build_request("GET", url)
                response = self._client.send(request, stream=True, follow_redirects=False)
            except httpx.HTTPError as error:
                raise ImageInputError("image download failed") from error
            try:
                if response.status_code in _REDIRECT_STATUSES:
                    location = response.headers.get("Location")
                    if location is None:
                        raise ImageInputError("image redirect is missing Location")
                    if redirects >= self.max_redirects:
                        raise ImageInputError("image redirect limit exceeded")
                    url = urljoin(url, location)
                    redirects += 1
                    continue
                try:
                    response.raise_for_status()
                except httpx.HTTPError as error:
                    raise ImageInputError("image download failed") from error
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    try:
                        lengths = {int(item.strip()) for item in content_length.split(",")}
                    except ValueError as error:
                        raise ImageInputError("image Content-Length is invalid") from error
                    if len(lengths) != 1 or next(iter(lengths)) > self.max_bytes:
                        raise ImageInputError(f"image exceeds maximum size of {self.max_bytes} bytes")
                content_type = response.headers.get("Content-Type")
                if content_type is not None:
                    actual_type = content_type.split(";", 1)[0].strip().casefold()
                    if actual_type != image.mime_type.casefold():
                        raise ImageInputError("image HTTP Content-Type does not match declared MIME")
                guessed = mimetypes.guess_type(urlsplit(url).path)[0]
                if guessed is not None and guessed.casefold() != image.mime_type.casefold():
                    raise ImageInputError("image URL extension MIME does not match declared MIME")
                return _bounded_join(response.iter_bytes(), self.max_bytes)
            finally:
                response.close()


__all__ = ["ImageInputError", "ImageInputGuard"]
