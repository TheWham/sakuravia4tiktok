"""Thin HTTP helpers for JSON and multipart requests."""

from __future__ import annotations

import json
import mimetypes
import uuid
from pathlib import Path
from urllib import request
from urllib.error import HTTPError, URLError


class HttpRequestError(RuntimeError):
    """Raised when a remote API returns an error response."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class SimpleHttpClient:
    """Minimal HTTP client based on urllib to avoid extra runtime dependencies."""

    _USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"

    def post_json(self, url: str, headers: dict[str, str], payload: dict[str, object]) -> dict[str, object]:
        """Send a JSON request and parse the JSON response."""
        encoded = json.dumps(payload).encode("utf-8")
        req = request.Request(url, data=encoded, method="POST")
        req.add_header("Content-Type", "application/json")
        self._add_default_headers(req)
        for key, value in headers.items():
            req.add_header(key, value)
        return self._read_json(req)

    def get_json(self, url: str, headers: dict[str, str] | None = None) -> dict[str, object]:
        """Download a JSON document from a provider-owned result URL."""
        req = request.Request(url, method="GET")
        self._add_default_headers(req)
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        return self._read_json(req)

    def post_multipart(
        self,
        url: str,
        headers: dict[str, str],
        fields: dict[str, str],
        file_field: str,
        file_path: Path,
    ) -> str:
        """Send one local file plus text fields and return the raw response body."""
        boundary = f"----Boundary{uuid.uuid4().hex}"
        body = bytearray()
        for key, value in fields.items():
            body.extend(f"--{boundary}\r\n".encode("utf-8"))
            body.extend(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8"))
            body.extend(value.encode("utf-8"))
            body.extend(b"\r\n")

        mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(
            f'Content-Disposition: form-data; name="{file_field}"; filename="{file_path.name}"\r\n'.encode("utf-8")
        )
        body.extend(f"Content-Type: {mime_type}\r\n\r\n".encode("utf-8"))
        body.extend(file_path.read_bytes())
        body.extend(b"\r\n")
        body.extend(f"--{boundary}--\r\n".encode("utf-8"))

        req = request.Request(url, data=bytes(body), method="POST")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        self._add_default_headers(req)
        for key, value in headers.items():
            req.add_header(key, value)

        try:
            with request.urlopen(req) as response:
                return response.read().decode("utf-8")
        except HTTPError as exc:
            message = exc.read().decode("utf-8", errors="ignore")
            raise HttpRequestError(message or str(exc), status_code=exc.code) from exc
        except URLError as exc:
            raise HttpRequestError(f"网络请求失败：{exc.reason}") from exc

    def _add_default_headers(self, req: request.Request) -> None:
        """Use a normal client fingerprint so API gateways do not reject urllib's default UA."""
        req.add_header("User-Agent", self._USER_AGENT)
        req.add_header("Accept", "application/json")

    def _read_json(self, req: request.Request) -> dict[str, object]:
        """Read and decode a JSON response, including the error body when available."""
        try:
            with request.urlopen(req) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            message = exc.read().decode("utf-8", errors="ignore")
            raise HttpRequestError(message or str(exc), status_code=exc.code) from exc
        except URLError as exc:
            raise HttpRequestError(f"网络请求失败：{exc.reason}") from exc

