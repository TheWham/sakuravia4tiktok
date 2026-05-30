"""Audio download and slicing helpers."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen
from uuid import uuid4

from ..config import AppConfig
from ..models import VideoMetadata
from .douyin_browser import DouyinBrowserResolver
from .process_runner import ProcessRunner


AUDIO_DOWNLOAD_TIMEOUT_SECONDS = 3600
VIDEO_DOWNLOAD_TIMEOUT_SECONDS = 3600
AUDIO_SPLIT_TIMEOUT_SECONDS = 1200
AUDIO_NORMALIZE_TIMEOUT_SECONDS = 1200
VIDEO_NORMALIZE_TIMEOUT_SECONDS = 3600
MIMO_AUDIO_SUFFIXES = {".mp3", ".flac", ".m4a", ".wav", ".ogg"}


class SubtitleOrAudioService:
    """Download Douyin media via Playwright-extracted URLs and ffmpeg processing."""

    def __init__(self, config: AppConfig, process_runner: ProcessRunner, douyin_resolver: DouyinBrowserResolver) -> None:
        self._config = config
        self._process_runner = process_runner
        self._douyin_resolver = douyin_resolver

    def download_audio(self, metadata: VideoMetadata) -> Path:
        """Download video via Playwright, then extract audio with ffmpeg."""
        video_dir = self._config.audio_dir / metadata.video_id
        run_dir = video_dir / f"run_{datetime.utcnow():%Y%m%d_%H%M%S}_{uuid4().hex[:8]}"
        run_dir.mkdir(parents=True, exist_ok=True)

        # Clean up stale partial files from previous runs.
        for stale_file in video_dir.glob(f"{metadata.video_id}.*.part"):
            try:
                stale_file.unlink(missing_ok=True)
            except OSError:
                continue

        # Resolve download URL and download video.
        result = self._douyin_resolver.resolve(metadata.video_id)
        video_path = run_dir / f"{metadata.video_id}.mp4"
        self._download_file(result.download_url or result.video_url, str(video_path), AUDIO_DOWNLOAD_TIMEOUT_SECONDS)

        # Extract audio with ffmpeg.
        audio_path = run_dir / f"{metadata.video_id}.mp3"
        self._process_runner.run(
            [
                self._config.ffmpeg_bin,
                "-y", "-i", str(video_path),
                "-vn", "-acodec", "libmp3lame", "-q:a", "4",
                str(audio_path),
            ],
            timeout_seconds=AUDIO_NORMALIZE_TIMEOUT_SECONDS,
        )

        # Clean up the video file after audio extraction.
        try:
            video_path.unlink(missing_ok=True)
        except OSError:
            pass

        if not audio_path.exists() or audio_path.stat().st_size <= 0:
            raise FileNotFoundError("ffmpeg 音频提取完成后未找到 mp3 文件。")

        # Check if we need format normalization.
        if audio_path.suffix.lower() in MIMO_AUDIO_SUFFIXES:
            return audio_path
        return self._normalize_audio_for_mimo(audio_path)

    def download_video(self, metadata: VideoMetadata) -> Path:
        """Download a single playable video file for Mimo video understanding."""
        video_dir = self._config.audio_dir / metadata.video_id
        run_dir = video_dir / f"video_{datetime.utcnow():%Y%m%d_%H%M%S}_{uuid4().hex[:8]}"
        run_dir.mkdir(parents=True, exist_ok=True)

        # Resolve download URL and download video.
        result = self._douyin_resolver.resolve(metadata.video_id)
        raw_video_path = run_dir / f"{metadata.video_id}.mp4"
        self._download_file(result.download_url or result.video_url, str(raw_video_path), VIDEO_DOWNLOAD_TIMEOUT_SECONDS)

        return self._normalize_video_for_mimo(raw_video_path)

    def split_audio(self, audio_path: Path, chunk_seconds: int = 600) -> list[Path]:
        """Split large audio files into fixed-size chunks accepted by the ASR provider."""
        target_dir = audio_path.parent / f"{audio_path.stem}_chunks"
        target_dir.mkdir(parents=True, exist_ok=True)
        output_pattern = target_dir / "chunk_%03d.m4a"

        self._process_runner.run(
            [
                self._config.ffmpeg_bin,
                "-i",
                str(audio_path),
                "-f",
                "segment",
                "-segment_time",
                str(chunk_seconds),
                "-c",
                "copy",
                str(output_pattern),
            ],
            timeout_seconds=AUDIO_SPLIT_TIMEOUT_SECONDS,
        )

        chunks = sorted(target_dir.glob("chunk_*.m4a"))
        if not chunks:
            raise FileNotFoundError("ffmpeg 切片完成后未生成任何音频分片。")
        return chunks

    def _download_file(self, url: str, dest: str, timeout: int) -> None:
        """Download a file with timeout."""
        req = Request(url, method="GET")
        req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36")
        req.add_header("Referer", "https://www.douyin.com/")
        with urlopen(req, timeout=timeout) as resp:
            data = resp.read()
        Path(dest).write_bytes(data)

    def _normalize_audio_for_mimo(self, audio_path: Path) -> Path:
        """Convert non-standard audio formats into a conservative MP3 for Mimo."""
        target_path = audio_path.with_suffix(".mp3")
        if target_path == audio_path:
            return audio_path
        self._process_runner.run(
            [
                self._config.ffmpeg_bin,
                "-y",
                "-i",
                str(audio_path),
                "-vn",
                "-codec:a",
                "libmp3lame",
                "-b:a",
                "128k",
                str(target_path),
            ],
            timeout_seconds=AUDIO_NORMALIZE_TIMEOUT_SECONDS,
        )
        if not target_path.exists() or target_path.stat().st_size <= 0:
            raise FileNotFoundError("ffmpeg 音频格式转换完成后未生成 mp3 文件。")
        return target_path

    def _normalize_video_for_mimo(self, video_path: Path) -> Path:
        """Transcode video variants into a conservative Mimo-friendly MP4."""
        target_path = video_path.with_name(f"{video_path.stem}_mimo.mp4")
        if target_path.exists() and target_path.stat().st_size > 0:
            return target_path
        self._process_runner.run(
            [
                self._config.ffmpeg_bin,
                "-y",
                "-i",
                str(video_path),
                "-vf",
                "scale='min(1280,iw)':-2",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-movflags",
                "+faststart",
                str(target_path),
            ],
            timeout_seconds=VIDEO_NORMALIZE_TIMEOUT_SECONDS,
        )
        if not target_path.exists() or target_path.stat().st_size <= 0:
            raise FileNotFoundError("ffmpeg 视频格式转换完成后未生成 Mimo 兼容 mp4 文件。")
        return target_path
