"""Small utility helpers shared by multiple modules."""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


AWEME_ID_PATTERN = re.compile(r"(\d{19})")
DOUYIN_VIDEO_URL_PATTERN = re.compile(r"https?://(?:www\.)?douyin\.com/video/(\d{19})(?:[^\s，。；、]*)?")
SHORT_LINK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][\w\-]{4,30}$")
SHORT_LINK_REDIRECT_TIMEOUT = 10


class ValidationError(ValueError):
    """Raised when the user input cannot be turned into a valid douyin video target."""


def _resolve_short_link(url: str) -> str:
    """Follow redirects on a short link and return the final URL."""
    req = Request(url, method="GET")
    req.add_header(
        "User-Agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    )
    try:
        with urlopen(req, timeout=SHORT_LINK_REDIRECT_TIMEOUT) as response:
            return response.url or url
    except (HTTPError, URLError, TimeoutError):
        return url


def normalize_douyin_source(source: str) -> str:
    """Accept an aweme id, douyin video URL, or short link and return the canonical aweme id."""
    text = source.strip()
    if not text:
        raise ValidationError("请输入视频链接或 awemeId。")

    match = AWEME_ID_PATTERN.search(text)
    if match:
        return match.group(1)

    # Bare short link ID (e.g. "OssFSFXz-cc") — construct v.douyin.com URL.
    if SHORT_LINK_ID_PATTERN.match(text) and not text.isdigit():
        resolved = _resolve_short_link(f"https://v.douyin.com/{text}/")
        resolved_match = AWEME_ID_PATTERN.search(resolved)
        if resolved_match:
            return resolved_match.group(1)
        raise ValidationError("短链接无效或已过期，无法解析为抖音视频。")

    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"}:
        raise ValidationError("仅支持 awemeId 或标准抖音视频链接。")

    # Resolve short links (v.douyin.com) by following redirects.
    if "v.douyin.com" in parsed.netloc:
        resolved = _resolve_short_link(text)
        resolved_match = AWEME_ID_PATTERN.search(resolved)
        if resolved_match:
            return resolved_match.group(1)
        raise ValidationError("短链接无效或已过期，无法解析为抖音视频。")

    if "douyin.com" not in parsed.netloc:
        raise ValidationError("链接不是有效的抖音视频地址。")

    # Only accept douyin.com/video/... URLs, not the homepage or other pages.
    if "/video/" not in parsed.path:
        raise ValidationError("链接不是有效的抖音视频地址，请使用 /video/ 开头的视频链接。")

    return text


def extract_douyin_video_source(text: str) -> tuple[str, str] | None:
    """Extract the first douyin video URL or aweme id from free-form text."""
    url_match = DOUYIN_VIDEO_URL_PATTERN.search(text)
    if url_match:
        return url_match.group(0), url_match.group(1)

    aweme_match = AWEME_ID_PATTERN.search(text)
    if aweme_match:
        video_id = aweme_match.group(1)
        return video_id, video_id
    return None


def sanitize_filename(title: str, limit: int = 80) -> str:
    """Remove Windows-illegal characters and keep filenames short enough for daily use."""
    cleaned = re.sub(r"[\\/:*?\"<>|]+", "_", title).strip().strip(".")
    compact = re.sub(r"\s+", "_", cleaned)
    if not compact:
        compact = "untitled"
    return compact[:limit]


def read_json_file(file_path: Path) -> dict[str, object]:
    """Load a UTF-8 json file created by subprocess helpers."""
    return json.loads(file_path.read_text(encoding="utf-8"))

