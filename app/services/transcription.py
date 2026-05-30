"""Audio transcription providers used when a 抖音 video has no subtitles."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Protocol

from ..config import AppConfig
from ..models import TranscriptResult
from .audio import SubtitleOrAudioService
from .http_client import HttpRequestError, SimpleHttpClient
from .media import OssMediaStorage, UploadedMedia


LOGGER = logging.getLogger(__name__)


class AudioTranscriptionProvider(Protocol):
    """Common contract for ASR providers hidden behind TranscriptionService."""

    def transcribe_file(self, file_path: Path) -> str:
        """Return plain transcript text for one local audio file."""


UploadedAudio = UploadedMedia


class PublicAudioStorage(Protocol):
    """Upload audio to a short-lived URL that Paraformer can fetch."""

    def upload_audio(self, file_path: Path) -> UploadedAudio:
        """Upload one audio file and return the URL passed to DashScope."""

    def delete_audio(self, object_key: str) -> None:
        """Remove the temporary audio object after recognition finishes."""


class OssAudioStorage(OssMediaStorage):
    """Store temporary ASR audio in OSS and expose a short-lived read URL.

    Paraformer only accepts reachable URLs. The safer default is a private
    bucket plus a signed GET URL; `ALIYUN_OSS_PUBLIC_BASE_URL` is kept only for
    compatiDouyinty when the user intentionally uses a public-read bucket.
    """

    def upload_audio(self, file_path: Path) -> UploadedAudio:
        """Upload one local audio file to OSS and build the URL given to ASR."""
        return self.upload_file(file_path, "asr")

    def delete_audio(self, object_key: str) -> None:
        """Delete one temporary OSS object if it was uploaded for ASR."""
        self.delete_file(object_key)


class GroqWhisperProvider:
    """Call Groq Whisper and keep its retry behavior isolated from other ASR providers."""

    def __init__(self, config: AppConfig, http_client: SimpleHttpClient) -> None:
        self._config = config
        self._http_client = http_client

    def transcribe_file(self, file_path: Path) -> str:
        """Send one audio file to Groq and return plain text."""
        url = "https://api.groq.com/openai/v1/audio/transcriptions"
        headers = {
            "Authorization": f"Bearer {self._config.groq_api_key}",
        }
        fields = {
            "model": self._config.groq_asr_model,
            "language": "zh",
            "response_format": "text",
            "temperature": "0",
        }
        last_error: HttpRequestError | None = None
        for _ in range(2):
            try:
                return self._http_client.post_multipart(url, headers, fields, "file", file_path).strip()
            except HttpRequestError as exc:
                last_error = exc
                continue
        detail = str(last_error) if last_error is not None else "未知错误"
        raise HttpRequestError(f"Groq Whisper 转写连续两次失败：{detail}") from last_error


class AliyunParaformerProvider:
    """Use Alibaba Cloud Model Studio Paraformer recorded-file ASR."""

    _SUBMIT_URL = "https://dashscope.aliyuncs.com/api/v1/services/audio/asr/transcription"
    _TASK_URL = "https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}"
    _RUNNING_STATUSES = {"PENDING", "RUNNING"}

    def __init__(
        self,
        config: AppConfig,
        http_client: SimpleHttpClient,
        audio_storage: PublicAudioStorage,
    ) -> None:
        self._config = config
        self._http_client = http_client
        self._audio_storage = audio_storage

    def transcribe_file(self, file_path: Path) -> str:
        """Upload audio to OSS, submit a Paraformer task, and return persisted text.

        DashScope requires an API Key from the 中国内地（北京）地域 for this model.
        The result URL is valid for a short window, so the transcript is fetched
        immediately instead of storing the provider URL for later use.
        """
        self._validate_config()
        uploaded: UploadedAudio | None = None
        try:
            uploaded = self._audio_storage.upload_audio(file_path)
            task_id = self._submit_task(uploaded.file_url)
            task_output = self._wait_for_task(task_id)
            transcription_url = self._extract_transcription_url(task_output)
            text = self._download_transcript(transcription_url)
            if not text.strip():
                raise HttpRequestError("阿里百炼 Paraformer 返回空转写结果。")
            return text.strip()
        finally:
            if uploaded is not None:
                self._delete_uploaded_audio(uploaded.object_key)

    def _validate_config(self) -> None:
        """Validate DashScope fields before uploading audio to OSS."""
        missing = []
        if not self._config.aliyun_dashscope_api_key:
            missing.append("ALIYUN_DASHSCOPE_API_KEY（必须是中国内地（北京）地域的百炼 API Key）")
        if not self._config.aliyun_asr_model:
            missing.append("ALIYUN_ASR_MODEL")
        if missing:
            raise RuntimeError(f"阿里百炼 Paraformer 缺少配置：{', '.join(missing)}")

    def _headers(self) -> dict[str, str]:
        """Build DashScope headers required for async recorded-file ASR."""
        return {
            "Authorization": f"Bearer {self._config.aliyun_dashscope_api_key}",
            "X-DashScope-Async": "enable",
        }

    def _submit_task(self, audio_url: str) -> str:
        """Submit one OSS audio URL to Paraformer and return task_id."""
        payload = {
            "model": self._config.aliyun_asr_model,
            "input": {
                "file_urls": [audio_url],
            },
        }
        response = self._http_client.post_json(self._SUBMIT_URL, self._headers(), payload)
        output = self._get_output(response)
        task_id = str(output.get("task_id") or "").strip()
        if not task_id:
            raise HttpRequestError("阿里百炼 Paraformer 未返回 task_id。")
        return task_id

    def _wait_for_task(self, task_id: str) -> dict[str, object]:
        """Poll DashScope until the async task finishes or times out."""
        deadline = time.monotonic() + max(0, self._config.aliyun_asr_timeout_seconds)
        task_url = self._TASK_URL.format(task_id=task_id)
        while True:
            response = self._http_client.post_json(task_url, self._headers(), {})
            output = self._get_output(response)
            status = str(output.get("task_status") or "").upper()
            if status == "SUCCEEDED":
                return output
            if status not in self._RUNNING_STATUSES:
                message = str(output.get("message") or output.get("code") or status or "未知错误")
                raise HttpRequestError(f"阿里百炼 Paraformer 任务失败：{message}")
            if time.monotonic() >= deadline:
                break
            time.sleep(max(0, self._config.aliyun_asr_poll_interval_seconds))
        raise HttpRequestError(f"阿里百炼 Paraformer 任务超时：{task_id}")

    def _extract_transcription_url(self, task_output: dict[str, object]) -> str:
        """Pick the succeeded subtask URL from DashScope's task output."""
        results = task_output.get("results")
        if not isinstance(results, list):
            raise HttpRequestError("阿里百炼 Paraformer 未返回识别结果列表。")

        failed_messages: list[str] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            status = str(item.get("subtask_status") or "").upper()
            transcription_url = str(item.get("transcription_url") or "").strip()
            if status == "SUCCEEDED" and transcription_url:
                return transcription_url
            if status == "FAILED":
                failed_messages.append(str(item.get("message") or item.get("code") or "子任务失败"))

        detail = "；".join(failed_messages) if failed_messages else "没有成功的子任务。"
        raise HttpRequestError(f"阿里百炼 Paraformer 未生成可下载的转写结果：{detail}")

    def _download_transcript(self, transcription_url: str) -> str:
        """Download DashScope's JSON result and join paragraph-level text."""
        result = self._http_client.get_json(transcription_url)
        transcripts = result.get("transcripts")
        if not isinstance(transcripts, list):
            raise HttpRequestError("阿里百炼 Paraformer 结果 JSON 缺少 transcripts。")

        parts: list[str] = []
        for transcript in transcripts:
            if not isinstance(transcript, dict):
                continue
            text = str(transcript.get("text") or "").strip()
            if text:
                parts.append(text)
                continue

            sentences = transcript.get("sentences")
            if isinstance(sentences, list):
                sentence_text = "".join(
                    str(sentence.get("text") or "")
                    for sentence in sentences
                    if isinstance(sentence, dict)
                ).strip()
                if sentence_text:
                    parts.append(sentence_text)
        return "\n".join(parts)

    def _delete_uploaded_audio(self, object_key: str) -> None:
        """Best-effort OSS cleanup; cleanup failure must not hide ASR errors."""
        try:
            self._audio_storage.delete_audio(object_key)
        except Exception as exc:  # noqa: BLE001 - cleanup must never mask the main task result.
            LOGGER.warning("删除 OSS 临时音频失败：%s", exc)

    def _get_output(self, response: dict[str, object]) -> dict[str, object]:
        """DashScope wraps normal payloads in output; keep parsing tolerant for tests."""
        output = response.get("output", response)
        if not isinstance(output, dict):
            raise HttpRequestError("阿里百炼 Paraformer 返回格式异常。")
        return output


class TranscriptionService:
    """Select the configured ASR provider and return transcript results."""

    _RETRYABLE_UPLOAD_STATUS_CODES = {400, 413}

    def __init__(
        self,
        config: AppConfig,
        http_client: SimpleHttpClient,
        audio_service: SubtitleOrAudioService,
        audio_storage: PublicAudioStorage | None = None,
    ) -> None:
        self._config = config
        self._audio_service = audio_service
        self._provider = self._build_provider(config, http_client, audio_storage)

    def transcribe_audio(self, audio_path: Path) -> TranscriptResult:
        """Use direct ASR first, then split audio only when the provider asks for it.

        目前只有 Groq 会因为上传大小触发分片重试。Paraformer 支持长录音文件，
        走 OSS URL 模式时没有必要在本地切片，避免后续摘要阶段再合并多段噪声。
        """
        try:
            text = self._provider.transcribe_file(audio_path)
            return TranscriptResult(source="asr_audio", full_text=text)
        except HttpRequestError as exc:
            if not self._should_retry_with_chunks(exc):
                raise
            chunks = self._audio_service.split_audio(audio_path)
            parts = [self._provider.transcribe_file(chunk) for chunk in chunks]
            return TranscriptResult(
                source="asr_audio",
                full_text="\n".join(part.strip() for part in parts if part.strip()),
            )

    def _build_provider(
        self,
        config: AppConfig,
        http_client: SimpleHttpClient,
        audio_storage: PublicAudioStorage | None,
    ) -> AudioTranscriptionProvider:
        """Create the ASR provider selected by ASR_PROVIDER."""
        provider = config.asr_provider.strip().lower()
        if provider == "groq":
            return GroqWhisperProvider(config, http_client)
        if provider == "aliyun_paraformer":
            return AliyunParaformerProvider(
                config,
                http_client,
                audio_storage or OssAudioStorage(config),
            )
        raise RuntimeError(f"不支持的 ASR_PROVIDER：{config.asr_provider}")

    def _should_retry_with_chunks(self, exc: HttpRequestError) -> bool:
        """Only split audio when the provider hints that the upload itself is the problem."""
        if self._config.asr_provider.strip().lower() != "groq":
            return False
        if exc.status_code in self._RETRYABLE_UPLOAD_STATUS_CODES:
            return True
        message = str(exc).lower()
        return "file" in message and ("large" in message or "size" in message or "format" in message)

