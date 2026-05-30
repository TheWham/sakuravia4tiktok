"""Configuration loading for the local assistant."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_ENV_PATH = BASE_DIR / ".env"


def _parse_env_file(env_path: Path) -> dict[str, str]:
    """Read a simple .env file without pulling an extra dependency."""
    values: dict[str, str] = {}
    if not env_path.exists():
        return values

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _get_setting(name: str, file_values: dict[str, str], default: str = "") -> str:
    """Environment variables override the values defined in the local .env file."""
    return os.getenv(name, file_values.get(name, default))


def _normalize_douyin_poll_range(file_values: dict[str, str]) -> tuple[int, int]:
    """Return the listener poll range while keeping old fixed-interval config usable."""
    legacy_interval = _get_setting("DOUYIN_POLL_INTERVAL_SECONDS", file_values).strip()
    min_text = _get_setting("DOUYIN_POLL_MIN_SECONDS", file_values).strip()
    max_text = _get_setting("DOUYIN_POLL_MAX_SECONDS", file_values).strip()

    if not min_text and not max_text and legacy_interval:
        min_seconds = max_seconds = int(legacy_interval)
    else:
        min_seconds = int(min_text or "180")
        max_seconds = int(max_text or "480")

    min_seconds = max(60, min_seconds)
    max_seconds = max(min_seconds, max_seconds)
    return min_seconds, max_seconds


@dataclass(slots=True)
class AppConfig:
    """Runtime settings used across the whole application."""

    app_host: str
    app_port: int
    sqlite_path: Path
    output_dir: Path
    audio_dir: Path
    tmp_dir: Path
    ffmpeg_bin: str
    groq_api_key: str
    groq_asr_model: str
    deepseek_base_url: str
    deepseek_api_key: str
    deepseek_model: str
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    smtp_use_ssl: bool
    mail_from: str
    mail_to: str
    douyin_cookies_file: Path | None = None
    keep_audio_after_success: bool = False
    summary_provider: str = "mimo"
    mimo_api_key: str = ""
    mimo_base_url: str = "https://api.xiaomimimo.com/v1"
    mimo_model: str = "mimo-v2.5"
    mimo_media_mode: str = "auto"
    mimo_max_completion_tokens: int = 4096
    mimo_video_fps: float = 1.0
    mimo_video_resolution: str = "default"
    asr_provider: str = "aliyun_paraformer"
    aliyun_dashscope_api_key: str = ""
    aliyun_asr_model: str = "paraformer-v2"
    aliyun_oss_access_key_id: str = ""
    aliyun_oss_access_key_secret: str = ""
    aliyun_oss_endpoint: str = ""
    aliyun_oss_bucket: str = ""
    aliyun_oss_public_base_url: str = ""
    aliyun_oss_signed_url_expires_seconds: int = 3600
    aliyun_asr_poll_interval_seconds: int = 5
    aliyun_asr_timeout_seconds: int = 1800
    douyin_enable_listener: bool = False
    douyin_cookie: str = ""
    douyin_self_mid: str = ""
    douyin_poll_interval_seconds: int = 60
    douyin_poll_min_seconds: int = 180
    douyin_poll_max_seconds: int = 480
    douyin_request_timeout_seconds: int = 15

    @classmethod
    def load(cls, env_path: Path | None = None) -> "AppConfig":
        """Load configuration and resolve all file-system locations under the workspace."""
        target_env = env_path or DEFAULT_ENV_PATH
        file_values = _parse_env_file(target_env)

        sqlite_path = BASE_DIR / _get_setting("SQLITE_PATH", file_values, "data/tasks.db")
        output_dir = BASE_DIR / _get_setting("OUTPUT_DIR", file_values, "data/output")
        audio_dir = BASE_DIR / "data/audio"
        tmp_dir = BASE_DIR / "data/tmp"
        douyin_cookies_file_text = _get_setting("DOUYIN_COOKIES_FILE", file_values).strip()
        if not douyin_cookies_file_text:
            douyin_cookies_file_text = _get_setting("YT_DLP_COOKIES_FILE", file_values).strip()
        douyin_cookies_file = BASE_DIR / douyin_cookies_file_text if douyin_cookies_file_text else None
        douyin_poll_min_seconds, douyin_poll_max_seconds = _normalize_douyin_poll_range(file_values)

        return cls(
            app_host=_get_setting("APP_HOST", file_values, "127.0.0.1"),
            app_port=int(_get_setting("APP_PORT", file_values, "8000")),
            sqlite_path=sqlite_path,
            output_dir=output_dir,
            audio_dir=audio_dir,
            keep_audio_after_success=_get_setting("KEEP_AUDIO_AFTER_SUCCESS", file_values, "false").lower() == "true",
            tmp_dir=tmp_dir,
            douyin_cookies_file=douyin_cookies_file,
            ffmpeg_bin=_get_setting("FFMPEG_BIN", file_values, "ffmpeg"),
            groq_api_key=_get_setting("GROQ_API_KEY", file_values),
            groq_asr_model=_get_setting("GROQ_ASR_MODEL", file_values, "whisper-large-v3-turbo"),
            deepseek_base_url=_get_setting("DEEPSEEK_BASE_URL", file_values, "https://api.deepseek.com"),
            deepseek_api_key=_get_setting("DEEPSEEK_API_KEY", file_values),
            deepseek_model=_get_setting("DEEPSEEK_MODEL", file_values, "deepseek-chat"),
            summary_provider=_get_setting("SUMMARY_PROVIDER", file_values, "mimo"),
            mimo_api_key=_get_setting("MIMO_API_KEY", file_values),
            mimo_base_url=_get_setting("MIMO_BASE_URL", file_values, "https://api.xiaomimimo.com/v1"),
            mimo_model=_get_setting("MIMO_MODEL", file_values, "mimo-v2.5"),
            mimo_media_mode=_get_setting("MIMO_MEDIA_MODE", file_values, "auto"),
            mimo_max_completion_tokens=int(_get_setting("MIMO_MAX_COMPLETION_TOKENS", file_values, "4096")),
            mimo_video_fps=float(_get_setting("MIMO_VIDEO_FPS", file_values, "1")),
            mimo_video_resolution=_get_setting("MIMO_VIDEO_RESOLUTION", file_values, "default"),
            smtp_host=_get_setting("SMTP_HOST", file_values),
            smtp_port=int(_get_setting("SMTP_PORT", file_values, "465")),
            smtp_username=_get_setting("SMTP_USERNAME", file_values),
            smtp_password=_get_setting("SMTP_PASSWORD", file_values),
            smtp_use_ssl=_get_setting("SMTP_USE_SSL", file_values, "true").lower() == "true",
            mail_from=_get_setting("MAIL_FROM", file_values),
            mail_to=_get_setting("MAIL_TO", file_values),
            asr_provider=_get_setting("ASR_PROVIDER", file_values, "aliyun_paraformer"),
            aliyun_dashscope_api_key=_get_setting("ALIYUN_DASHSCOPE_API_KEY", file_values),
            aliyun_asr_model=_get_setting("ALIYUN_ASR_MODEL", file_values, "paraformer-v2"),
            aliyun_oss_access_key_id=_get_setting("ALIYUN_OSS_ACCESS_KEY_ID", file_values),
            aliyun_oss_access_key_secret=_get_setting("ALIYUN_OSS_ACCESS_KEY_SECRET", file_values),
            aliyun_oss_endpoint=_get_setting("ALIYUN_OSS_ENDPOINT", file_values),
            aliyun_oss_bucket=_get_setting("ALIYUN_OSS_BUCKET", file_values),
            aliyun_oss_public_base_url=_get_setting("ALIYUN_OSS_PUBLIC_BASE_URL", file_values),
            aliyun_oss_signed_url_expires_seconds=int(
                _get_setting("ALIYUN_OSS_SIGNED_URL_EXPIRES_SECONDS", file_values, "3600")
            ),
            aliyun_asr_poll_interval_seconds=int(
                _get_setting("ALIYUN_ASR_POLL_INTERVAL_SECONDS", file_values, "5")
            ),
            aliyun_asr_timeout_seconds=int(_get_setting("ALIYUN_ASR_TIMEOUT_SECONDS", file_values, "1800")),
            douyin_enable_listener=_get_setting("DOUYIN_ENABLE_LISTENER", file_values, "false").lower() == "true",
            douyin_cookie=_get_setting("DOUYIN_COOKIE", file_values),
            douyin_self_mid=_get_setting("DOUYIN_SELF_MID", file_values),
            douyin_poll_interval_seconds=douyin_poll_min_seconds,
            douyin_poll_min_seconds=douyin_poll_min_seconds,
            douyin_poll_max_seconds=douyin_poll_max_seconds,
            douyin_request_timeout_seconds=int(_get_setting("DOUYIN_REQUEST_TIMEOUT_SECONDS", file_values, "15")),
        )

    def ensure_directories(self) -> None:
        """Create required folders before the first task is persisted or written."""
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

