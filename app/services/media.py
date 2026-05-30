"""Shared media upload and inspection helpers."""

from __future__ import annotations

import json
import mimetypes
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from ..config import AppConfig
from .process_runner import ProcessExecutionError, ProcessRunner


AUDIO_URL_SIZE_LIMIT_BYTES = 100 * 1024 * 1024
VIDEO_URL_SIZE_LIMIT_BYTES = 300 * 1024 * 1024
MIN_AUDIO_SIZE_BYTES = 4 * 1024
MIN_AUDIO_DURATION_SECONDS = 1.0
SILENCE_MEAN_VOLUME_DB = -55.0
MEDIA_PROBE_TIMEOUT_SECONDS = 60


@dataclass(frozen=True, slots=True)
class UploadedMedia:
    """OSS object metadata needed by external providers and cleanup."""

    object_key: str
    file_url: str


class PublicMediaStorage(Protocol):
    """Upload one local media file and expose a short-lived readable URL."""

    def upload_file(self, file_path: Path, object_prefix: str) -> UploadedMedia:
        """Upload one media file and return the URL passed to the model provider."""

    def delete_file(self, object_key: str) -> None:
        """Remove the temporary media object after the provider finishes."""


class OssMediaStorage:
    """Store temporary media in OSS and expose it through a signed GET URL.

    Mimo 和 Paraformer 都只需要一个公网可访问 URL。这里默认使用私有 Bucket
    加短时签名 URL，既能让外部模型拉取文件，又不会把临时音视频长期公开。
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._bucket = None

    def upload_file(self, file_path: Path, object_prefix: str) -> UploadedMedia:
        """Upload one local media file to OSS and build a provider-readable URL."""
        self._validate_config()
        object_key = self._build_object_key(file_path, object_prefix)
        bucket = self._get_bucket()
        bucket.put_object_from_file(object_key, str(file_path), headers=self._build_content_headers(file_path))
        return UploadedMedia(object_key=object_key, file_url=self._build_file_url(bucket, object_key))

    def delete_file(self, object_key: str) -> None:
        """Delete one temporary OSS object if it was uploaded."""
        if object_key:
            self._get_bucket().delete_object(object_key)

    def _get_bucket(self):
        """Create the OSS client lazily so tests do not need the SDK."""
        if self._bucket is not None:
            return self._bucket

        try:
            import oss2
        except ImportError as exc:  # pragma: no cover - exercised only without dependency installed.
            raise RuntimeError("缺少 oss2 依赖，请先安装 requirements.txt。") from exc

        auth = oss2.Auth(self._config.aliyun_oss_access_key_id, self._config.aliyun_oss_access_key_secret)
        self._bucket = oss2.Bucket(auth, self._config.aliyun_oss_endpoint, self._config.aliyun_oss_bucket)
        return self._bucket

    def _validate_config(self) -> None:
        """Fail early when OSS upload or URL generation is not fully configured."""
        missing = []
        for name, value in {
            "ALIYUN_OSS_ACCESS_KEY_ID": self._config.aliyun_oss_access_key_id,
            "ALIYUN_OSS_ACCESS_KEY_SECRET": self._config.aliyun_oss_access_key_secret,
            "ALIYUN_OSS_ENDPOINT": self._config.aliyun_oss_endpoint,
            "ALIYUN_OSS_BUCKET": self._config.aliyun_oss_bucket,
        }.items():
            if not value:
                missing.append(name)
        if missing:
            raise RuntimeError(f"阿里云 OSS 临时媒体上传缺少配置：{', '.join(missing)}")

    def _build_object_key(self, file_path: Path, object_prefix: str) -> str:
        """Keep temporary media grouped by date for lifecycle cleanup."""
        date_part = datetime.now().strftime("%Y%m%d")
        safe_prefix = object_prefix.strip("/").replace("\\", "/") or "media"
        safe_name = file_path.name.replace("\\", "_").replace("/", "_")
        return f"{safe_prefix}/{date_part}/{uuid.uuid4().hex}/{safe_name}"

    def _build_file_url(self, bucket, object_key: str) -> str:
        """Prefer a signed URL so the Bucket can stay private."""
        public_base_url = self._config.aliyun_oss_public_base_url.strip()
        if public_base_url:
            return f"{public_base_url.rstrip('/')}/{quote(object_key, safe='/')}"
        expires = max(60, self._config.aliyun_oss_signed_url_expires_seconds)
        return bucket.sign_url("GET", object_key, expires)

    def _build_content_headers(self, file_path: Path) -> dict[str, str]:
        """Set a concrete media Content-Type for providers that inspect headers.

        OSS 默认可能把临时对象标成 application/octet-stream。Mimo 的音频理解会校验
        格式，给 m4a/mp3/wav 等文件补上准确 Content-Type，可以避免“后缀正确但
        远端仍按未知二进制拒绝”的问题。
        """
        suffix = file_path.suffix.lower()
        content_type = mimetypes.guess_type(file_path.name)[0]
        if suffix == ".m4a":
            content_type = "audio/mp4"
        elif suffix == ".mp3":
            content_type = "audio/mpeg"
        elif suffix == ".wav":
            content_type = "audio/wav"
        elif suffix == ".flac":
            content_type = "audio/flac"
        elif suffix == ".ogg":
            content_type = "audio/ogg"
        elif suffix == ".mp4":
            content_type = "video/mp4"
        return {"Content-Type": content_type or "application/octet-stream"}


class InvalidAudioError(RuntimeError):
    """Raised when a local audio file should not be sent to Mimo."""


class AudioProbeService:
    """Validate whether a downloaded audio file is worth sending to Mimo."""

    _MEAN_VOLUME_PATTERN = re.compile(r"mean_volume:\s*(-?(?:\d+(?:\.\d+)?|inf))\s*dB", re.IGNORECASE)

    def __init__(self, config: AppConfig, process_runner: ProcessRunner) -> None:
        self._config = config
        self._process_runner = process_runner

    def ensure_valid_audio(self, audio_path: Path) -> None:
        """Raise a clear reason when audio is missing, empty, silent, or too large."""
        if not audio_path.exists() or not audio_path.is_file():
            raise InvalidAudioError("音频文件不存在。")
        size = audio_path.stat().st_size
        if size < MIN_AUDIO_SIZE_BYTES:
            raise InvalidAudioError(f"音频文件过小（{size} 字节），可能下载异常或没有有效声音。")
        if size > AUDIO_URL_SIZE_LIMIT_BYTES:
            raise InvalidAudioError("音频超过 Mimo URL 方式 100 MB 限制。")

        duration = self._probe_duration_seconds(audio_path)
        if duration <= MIN_AUDIO_DURATION_SECONDS:
            raise InvalidAudioError(f"音频时长过短（{duration:.2f} 秒），不适合做音频理解。")

        mean_volume = self._probe_mean_volume(audio_path)
        if mean_volume is not None and mean_volume <= SILENCE_MEAN_VOLUME_DB:
            raise InvalidAudioError(f"音频平均音量接近静音（{mean_volume:.1f} dB）。")

    def ensure_valid_video(self, video_path: Path) -> None:
        """Raise a clear reason when video cannot be sent through Mimo URL mode."""
        if not video_path.exists() or not video_path.is_file():
            raise RuntimeError("视频文件不存在。")
        size = video_path.stat().st_size
        if size <= 0:
            raise RuntimeError("视频文件为空。")
        if size > VIDEO_URL_SIZE_LIMIT_BYTES:
            raise RuntimeError("视频超过 Mimo URL 方式 300 MB 限制。")

    def _probe_duration_seconds(self, audio_path: Path) -> float:
        """Read audio stream or format duration from ffprobe JSON output."""
        args = [
            self._ffprobe_bin(),
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,duration:format=duration",
            "-of",
            "json",
            str(audio_path),
        ]
        try:
            result = self._process_runner.run(args, timeout_seconds=MEDIA_PROBE_TIMEOUT_SECONDS)
        except ProcessExecutionError as exc:
            raise InvalidAudioError(f"ffprobe 检测音频失败：{exc}") from exc

        payload = json.loads(result or "{}")
        streams = payload.get("streams")
        if not isinstance(streams, list) or not any(self._is_audio_stream(item) for item in streams):
            raise InvalidAudioError("ffprobe 未检测到音频流。")

        for item in streams:
            if not self._is_audio_stream(item):
                continue
            duration = self._to_float(item.get("duration"))
            if duration > 0:
                return duration

        fmt = payload.get("format")
        if isinstance(fmt, dict):
            duration = self._to_float(fmt.get("duration"))
            if duration > 0:
                return duration
        return 0.0

    def _probe_mean_volume(self, audio_path: Path) -> float | None:
        """Use ffmpeg volumedetect output to catch long silent recordings."""
        null_target = "NUL" if os.name == "nt" else "/dev/null"
        args = [
            self._config.ffmpeg_bin,
            "-hide_banner",
            "-nostats",
            "-i",
            str(audio_path),
            "-af",
            "volumedetect",
            "-f",
            "null",
            null_target,
        ]
        try:
            result = self._process_runner.run_with_result(args, timeout_seconds=MEDIA_PROBE_TIMEOUT_SECONDS)
        except ProcessExecutionError as exc:
            raise InvalidAudioError(f"ffmpeg 音量检测失败：{exc}") from exc

        match = self._MEAN_VOLUME_PATTERN.search(result.stderr or result.stdout)
        if not match:
            return None
        value = match.group(1).lower()
        if value in {"-inf", "inf"}:
            return float("-inf") if value == "-inf" else float("inf")
        return float(value)

    def _ffprobe_bin(self) -> str:
        """Infer ffprobe from the configured ffmpeg path."""
        ffmpeg_path = Path(self._config.ffmpeg_bin)
        if ffmpeg_path.parent == Path("."):
            return "ffprobe"
        suffix = ffmpeg_path.suffix
        return str(ffmpeg_path.with_name(f"ffprobe{suffix}"))

    def _is_audio_stream(self, value: object) -> bool:
        """Return whether an ffprobe stream item is an audio stream."""
        return isinstance(value, dict) and value.get("codec_type") == "audio"

    def _to_float(self, value: object) -> float:
        """Parse ffprobe numeric fields while treating N/A as zero."""
        try:
            return float(str(value))
        except (TypeError, ValueError):
            return 0.0

