"""Resolve user input into Douyin metadata and subtitles."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..config import AppConfig
from ..models import TranscriptResult, TranscriptSegment, VideoMetadata, VideoPart
from ..utils import normalize_douyin_source
from .douyin_browser import DouyinBrowserResolver


LOGGER = logging.getLogger(__name__)

SUBTITLE_DOWNLOAD_TIMEOUT = 30


class DouyinResolverService:
    """Resolve Douyin video metadata, subtitles via Playwright browser automation."""

    def __init__(self, config: AppConfig, browser_resolver: DouyinBrowserResolver | None = None) -> None:
        self._config = config
        self._browser_resolver = browser_resolver or DouyinBrowserResolver(config)

    def normalize_source(self, source: str) -> str:
        """Normalize form input early so duplicate tasks can be detected reliably."""
        return normalize_douyin_source(source)

    def fetch_metadata(self, video_id: str) -> VideoMetadata:
        """Fetch video metadata via Playwright."""
        return self.fetch_metadata_for_source(video_id, f"https://www.douyin.com/video/{video_id}")

    def fetch_metadata_for_source(self, video_id: str, source_input: str) -> VideoMetadata:
        """Fetch video metadata via Playwright browser extraction."""
        LOGGER.info("Resolving Douyin video %s via Playwright", video_id)
        result = self._browser_resolver.resolve(video_id)

        subtitle_candidates: list[dict[str, str]] = []
        if result.subtitle_urls:
            for url in result.subtitle_urls:
                subtitle_candidates.append({
                    "lang": "zh",
                    "source": "browser_extracted",
                    "url": url,
                    "ext": "json3",
                })

        duration_seconds = result.duration_ms // 1000 if result.duration_ms else 0

        return VideoMetadata(
            video_id=video_id,
            title=result.title or video_id,
            uploader=result.author,
            duration=duration_seconds,
            webpage_url=f"https://www.douyin.com/video/{video_id}",
            description=result.title,
            tags=[],
            subtitle_candidates=subtitle_candidates,
        )

    def inspect_parts(self, source: str) -> tuple[str, str, list[VideoPart]]:
        """Return selectable entries before creating a task.

        普通单视频只会返回一个条目；多 P 或合集会返回多个条目，前端据此让用户
        手动选择要处理哪一集，避免后台误把整个列表交给下载器。
        """
        video_id = self.normalize_source(source)
        LOGGER.info("Inspecting Douyin video %s via Playwright", video_id)
        result = self._browser_resolver.resolve(video_id)
        title = result.title or video_id
        parts = [VideoPart(index=1, title=title, duration=result.duration_ms // 1000, url=f"https://www.douyin.com/video/{video_id}")]
        return video_id, title, parts

    def fetch_subtitles(self, metadata: VideoMetadata) -> TranscriptResult | None:
        """Download the most useful Chinese subtitle track if one exists."""
        candidates = [item for item in metadata.subtitle_candidates if item.get("lang", "").startswith("zh")]
        if not candidates:
            return None

        target_dir = self._config.tmp_dir / f"subtitle_{metadata.video_id}"
        target_dir.mkdir(parents=True, exist_ok=True)

        for candidate in candidates:
            url = candidate.get("url", "")
            if not url:
                continue
            ext = candidate.get("ext", "json3")
            subtitle_path = target_dir / f"{metadata.video_id}.{ext}"
            try:
                self._download_file(url, str(subtitle_path))
            except Exception as exc:
                LOGGER.warning("Subtitle download failed for %s: %s", url, exc)
                continue
            if not subtitle_path.exists() or subtitle_path.stat().st_size == 0:
                continue
            try:
                transcript = self._read_subtitle_file(subtitle_path)
                if transcript.full_text.strip():
                    return transcript
            except Exception as exc:
                LOGGER.warning("Subtitle parse failed for %s: %s", subtitle_path, exc)
                continue

        return None

    def _download_file(self, url: str, dest: str) -> None:
        """Download a file with timeout."""
        req = Request(url, method="GET")
        req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36")
        req.add_header("Referer", "https://www.douyin.com/")
        with urlopen(req, timeout=SUBTITLE_DOWNLOAD_TIMEOUT) as resp:
            data = resp.read()
        Path(dest).write_bytes(data)

    def _read_subtitle_file(self, file_path: Path) -> TranscriptResult:
        """Parse subtitle files (json3 or vtt) downloaded from Douyin."""
        if file_path.suffix == ".json3":
            payload = json.loads(file_path.read_text(encoding="utf-8"))
            segments: list[TranscriptSegment] = []
            texts: list[str] = []
            for event in payload.get("events", []):
                parts = event.get("segs") or []
                text = "".join(part.get("utf8", "") for part in parts).strip()
                if not text:
                    continue
                start = (event.get("tStartMs") or 0) / 1000
                duration = (event.get("dDurationMs") or 0) / 1000
                segments.append(TranscriptSegment(start=start, end=start + duration, text=text))
                texts.append(text)
            return TranscriptResult(source="official_subtitle", full_text="\n".join(texts), segments=segments)

        lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        segments = []
        texts = []
        pending_text: list[str] = []
        start = None
        end = None
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped == "WEBVTT":
                continue
            if "-->" in stripped:
                if pending_text:
                    text = " ".join(pending_text).strip()
                    segments.append(TranscriptSegment(start=start, end=end, text=text))
                    texts.append(text)
                    pending_text = []
                start_text, end_text = [part.strip() for part in stripped.split("-->", 1)]
                start = self._parse_vtt_time(start_text)
                end = self._parse_vtt_time(end_text)
                continue
            if stripped.isdigit():
                continue
            pending_text.append(stripped)
        if pending_text:
            text = " ".join(pending_text).strip()
            segments.append(TranscriptSegment(start=start, end=end, text=text))
            texts.append(text)
        return TranscriptResult(source="official_subtitle", full_text="\n".join(texts), segments=segments)

    def _parse_vtt_time(self, text: str) -> float:
        """Convert a VTT timestamp into floating-point seconds."""
        normalized = text.replace(",", ".")
        hour_text, minute_text, second_text = normalized.split(":")
        return int(hour_text) * 3600 + int(minute_text) * 60 + float(second_text)
