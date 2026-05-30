"""End-to-end task orchestration and state transitions."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..models import MailStatus, SummaryResult, TaskRecord, TaskStatus, TranscriptResult, VideoMetadata
from ..storage import TaskRepository
from .artifact import ArtifactService
from .audio import SubtitleOrAudioService
from .douyin import DouyinResolverService
from .mail import MailService
from .media import AudioProbeService, InvalidAudioError
from .mimo import MimoNoValidAudioError, MimoSummaryService
from .summary import SummaryService
from .transcription import TranscriptionService


LOGGER = logging.getLogger(__name__)
AUDIO_SUFFIXES = {".m4a", ".mp3", ".wav", ".flac", ".ogg"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".avi", ".wmv"}


class TaskService:
    """Coordinate the whole pipeline while keeping each service focused."""

    def __init__(
        self,
        repository: TaskRepository,
        Douyin_service: DouyinResolverService,
        subtitle_audio_service: SubtitleOrAudioService,
        transcription_service: TranscriptionService,
        summary_service: SummaryService,
        artifact_service: ArtifactService,
        mail_service: MailService,
        keep_audio_after_success: bool = False,
        mimo_summary_service: MimoSummaryService | None = None,
        audio_probe_service: AudioProbeService | None = None,
        mimo_media_mode: str = "auto",
    ) -> None:
        self._repository = repository
        self._douyin_service = Douyin_service
        self._subtitle_audio_service = subtitle_audio_service
        self._transcription_service = transcription_service
        self._summary_service = summary_service
        self._artifact_service = artifact_service
        self._mail_service = mail_service
        self._keep_audio_after_success = keep_audio_after_success
        self._mimo_summary_service = mimo_summary_service
        self._audio_probe_service = audio_probe_service
        self._mimo_media_mode = mimo_media_mode.strip().lower() or "auto"
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="summary-worker")

    def submit_task(self, source_input: str, reuse_success: bool = False, send_mail: bool = True) -> TaskRecord:
        """Create or reuse a task and make sure new work is scheduled exactly once.

        页面手动提交只复用运行中任务；抖音 @ 触发会额外复用历史成功任务，
        避免多人重复 @ 同一个视频时反复消耗外部 API 额度。抖音触发的新任务
        不发送默认邮箱，完成后由事件记录按请求用户绑定邮箱单独投递。
        """
        video_id = self._douyin_service.normalize_source(source_input)
        active = self._repository.find_reusable_task_by_video_id(
            video_id,
            source_input.strip(),
            include_success=reuse_success,
        )
        if active is not None:
            return active

        task = self._repository.create_task(source_input=source_input.strip(), video_id=video_id, send_mail=send_mail)
        self._executor.submit(self._process_task, task.id)
        return task

    def retry_task(self, task_id: int) -> TaskRecord:
        """Reset one failed task and enqueue it again.

        The retry keeps existing artifacts such as downloaded audio or Markdown.
        `_process_task` will inspect those checkpoints and continue from the
        cheapest safe stage instead of blindly starting from scratch.
        """
        current = self._repository.get_task(task_id)
        if current.status is TaskStatus.SUCCESS:
            return current
        if self._is_active_status(current.status):
            return current
        task = self._repository.reset_task_for_retry(task_id)
        self._repository.reopen_douyin_events_for_task(task.id)
        self._executor.submit(self._process_task, task.id)
        return self._repository.get_task(task.id)

    def recover_interrupted_tasks(self) -> list[TaskRecord]:
        """Schedule tasks left active by the previous process instance."""
        tasks = self._repository.prepare_interrupted_tasks_for_retry()
        for task in tasks:
            self._executor.submit(self._process_task, task.id)
        return tasks

    def list_tasks(self) -> list[TaskRecord]:
        """Return latest tasks for page polling and API access."""
        return self._repository.list_tasks()

    def get_task(self, task_id: int) -> TaskRecord:
        """Load one task for API detail responses."""
        return self._repository.get_task(task_id)

    def _process_task(self, task_id: int) -> None:
        """Run the configured pipeline and persist every visible state change."""
        try:
            task = self._repository.get_task(task_id)
            existing_markdown = self._load_existing_markdown(task)
            if existing_markdown is not None:
                markdown_content, markdown_path = existing_markdown
                title = task.video_title or task.video_id
                self._repository.update_task_fields(
                    task_id,
                    markdown_content=markdown_content,
                    markdown_file_path=str(markdown_path),
                    last_checkpoint="markdown_ready",
                )
                self._complete_mail_step(task_id, title, markdown_content, markdown_path)
                self._cleanup_success_audio(task_id, None)
                self._set_status(task_id, TaskStatus.SUCCESS)
                return

            self._set_status(task_id, TaskStatus.RESOLVING_VIDEO)
            metadata = self._douyin_service.fetch_metadata_for_source(task.video_id, task.source_input)
            self._repository.update_task_fields(
                task_id,
                video_title=metadata.title,
                last_checkpoint="metadata",
            )

            self._set_status(task_id, TaskStatus.FETCHING_SUBTITLE)
            try:
                transcript = self._douyin_service.fetch_subtitles(metadata)
            except Exception as exc:
                LOGGER.warning("Subtitle fetch failed, skipping to media fallback: %s", exc)
                transcript = None
            subtitle_source = "official_subtitle"
            audio_path: Path | None = None

            if transcript is not None:
                self._repository.update_task_fields(
                    task_id,
                    subtitle_source=subtitle_source,
                    last_checkpoint="transcript",
                )
                self._set_status(task_id, TaskStatus.SUMMARIZING)
                summary = self._summarize_transcript(metadata, transcript)
            elif self._mimo_summary_service is not None:
                summary, subtitle_source, audio_path = self._summarize_media_with_mimo(task_id, task, metadata)
            else:
                subtitle_source = "asr_audio"
                audio_path = self._load_existing_audio(task)
                if audio_path is None:
                    self._set_status(task_id, TaskStatus.DOWNLOADING_AUDIO)
                    audio_path = self._subtitle_audio_service.download_audio(metadata)
                    self._repository.update_task_fields(
                        task_id,
                        audio_file_path=str(audio_path),
                        last_checkpoint="audio",
                    )

                self._set_status(task_id, TaskStatus.TRANSCRIBING)
                transcript = self._transcription_service.transcribe_audio(audio_path)
                self._repository.update_task_fields(
                    task_id,
                    subtitle_source=subtitle_source,
                    last_checkpoint="transcript",
                )
                self._set_status(task_id, TaskStatus.SUMMARIZING)
                summary = self._summary_service.summarize(metadata, transcript)
            self._repository.update_task_fields(task_id, markdown_content=summary.markdown, last_checkpoint="summary")

            self._set_status(task_id, TaskStatus.WRITING_MARKDOWN)
            markdown_path = self._artifact_service.write_markdown(metadata.video_id, metadata.title, summary.markdown)
            self._repository.update_task_fields(
                task_id,
                markdown_file_path=str(markdown_path),
                markdown_content=summary.markdown,
                last_checkpoint="markdown_ready",
            )

            self._complete_mail_step(task_id, metadata.title, summary.markdown, markdown_path)
            self._cleanup_success_audio(task_id, audio_path)
            self._set_status(task_id, TaskStatus.SUCCESS)
        except Exception as exc:  # noqa: BLE001 - the page needs the original message.
            self._repository.update_task_fields(
                task_id,
                status=TaskStatus.FAILED.value,
                mail_status=MailStatus.FAILED.value,
                error_message=str(exc),
            )

    def _set_status(self, task_id: int, status: TaskStatus) -> None:
        """Centralize status updates so the pipeline stays readable."""
        self._repository.update_task_fields(task_id, status=status.value)

    def _is_active_status(self, status: TaskStatus) -> bool:
        """Return whether a task is already owned by the in-process worker."""
        return status in {
            TaskStatus.PENDING,
            TaskStatus.RESOLVING_VIDEO,
            TaskStatus.FETCHING_SUBTITLE,
            TaskStatus.DOWNLOADING_AUDIO,
            TaskStatus.TRANSCRIBING,
            TaskStatus.SUMMARIZING,
            TaskStatus.WRITING_MARKDOWN,
            TaskStatus.SENDING_MAIL,
        }

    def _summarize_transcript(self, metadata: VideoMetadata, transcript: TranscriptResult) -> SummaryResult:
        """Summarize official subtitles through the configured model provider."""
        if self._mimo_summary_service is not None:
            return self._mimo_summary_service.summarize_text(metadata, transcript)
        return self._summary_service.summarize(metadata, transcript)

    def _summarize_media_with_mimo(
        self,
        task_id: int,
        task: TaskRecord,
        metadata: VideoMetadata,
    ) -> tuple[SummaryResult, str, Path | None]:
        """Use Mimo audio/video understanding when no official subtitle exists.

        V4 的默认策略是 auto：先走音频，只有本地检测或 Mimo 返回明确说明音频
        无效时，才下载视频并改走视频理解。这样保留速度和成本优势，同时给屏幕
        录制、Prompt 展示、图表讲解这类强画面视频留出兜底路径。
        """
        if self._mimo_summary_service is None:
            raise RuntimeError("Mimo 总结服务未初始化。")
        if self._mimo_media_mode not in {"auto", "audio", "video"}:
            raise RuntimeError(f"不支持的 MIMO_MEDIA_MODE：{self._mimo_media_mode}")

        if self._mimo_media_mode == "video":
            summary, video_path = self._summarize_video_with_mimo(task_id, task, metadata)
            return summary, "mimo_video", video_path

        audio_path = self._load_existing_audio(task)
        if audio_path is None:
            self._set_status(task_id, TaskStatus.DOWNLOADING_AUDIO)
            audio_path = self._subtitle_audio_service.download_audio(metadata)
            self._repository.update_task_fields(
                task_id,
                audio_file_path=str(audio_path),
                last_checkpoint="audio",
            )

        try:
            self._ensure_valid_audio(audio_path)
            self._set_status(task_id, TaskStatus.TRANSCRIBING)
            summary = self._mimo_summary_service.summarize_audio(metadata, audio_path)
            self._repository.update_task_fields(
                task_id,
                subtitle_source="mimo_audio",
                last_checkpoint="mimo_audio",
            )
            return summary, "mimo_audio", audio_path
        except (InvalidAudioError, MimoNoValidAudioError) as exc:
            if self._mimo_media_mode == "audio":
                raise RuntimeError(f"Mimo 音频理解不可用：{exc}") from exc
            LOGGER.warning("Mimo audio path is not usable, falling back to video: %s", exc)
            summary, video_path = self._summarize_video_with_mimo(task_id, task, metadata)
            self._delete_success_media(audio_path)
            return summary, "mimo_video", video_path

    def _summarize_video_with_mimo(
        self,
        task_id: int,
        task: TaskRecord,
        metadata: VideoMetadata,
    ) -> tuple[SummaryResult, Path]:
        """Download video, validate Mimo limits, and ask Mimo for final Markdown."""
        if self._mimo_summary_service is None:
            raise RuntimeError("Mimo 总结服务未初始化。")
        video_path = self._load_existing_video(task)
        if video_path is None:
            self._set_status(task_id, TaskStatus.DOWNLOADING_AUDIO)
            video_path = self._subtitle_audio_service.download_video(metadata)
            self._repository.update_task_fields(task_id, last_checkpoint="video", audio_file_path=str(video_path))
        self._ensure_valid_video(video_path)
        self._set_status(task_id, TaskStatus.TRANSCRIBING)
        summary = self._mimo_summary_service.summarize_video(metadata, video_path)
        self._repository.update_task_fields(
            task_id,
            subtitle_source="mimo_video",
            last_checkpoint="mimo_video",
        )
        return summary, video_path

    def _ensure_valid_audio(self, audio_path: Path) -> None:
        """Run the optional audio health check before paying for Mimo audio understanding."""
        if self._audio_probe_service is not None:
            self._audio_probe_service.ensure_valid_audio(audio_path)

    def _ensure_valid_video(self, video_path: Path) -> None:
        """Run the optional video size check before sending a URL to Mimo."""
        if self._audio_probe_service is not None:
            self._audio_probe_service.ensure_valid_video(video_path)

    def _load_existing_audio(self, task: TaskRecord) -> Path | None:
        """Return a completed audio artifact when retry can safely skip download."""
        if not task.audio_file_path:
            return None
        audio_path = Path(task.audio_file_path)
        if (
            audio_path.suffix.lower() in AUDIO_SUFFIXES
            and audio_path.exists()
            and audio_path.is_file()
            and audio_path.stat().st_size > 0
        ):
            self._repository.update_task_fields(task.id, last_checkpoint="audio")
            return audio_path
        return None

    def _load_existing_video(self, task: TaskRecord) -> Path | None:
        """Return a completed video artifact when retry can skip video download."""
        if not task.audio_file_path:
            return None
        video_path = Path(task.audio_file_path)
        if (
            video_path.suffix.lower() in VIDEO_SUFFIXES
            and video_path.exists()
            and video_path.is_file()
            and video_path.stat().st_size > 0
        ):
            self._repository.update_task_fields(task.id, last_checkpoint="video")
            return video_path
        return None

    def _delete_success_media(self, media_path: Path | None) -> None:
        """Delete a local media file after a successful fallback path no longer needs it."""
        if self._keep_audio_after_success or media_path is None:
            return
        try:
            if media_path.exists() and media_path.is_file():
                media_path.unlink()
        except OSError as exc:
            LOGGER.warning("删除本地临时媒体失败：%s", exc)

    def _cleanup_success_audio(self, task_id: int, audio_path: Path | None) -> None:
        """Remove local ASR audio after a fully successful task when configured.

        成功后删除音频可以控制 2G/40G 云服务器的长期磁盘占用；失败任务不进
        这个方法，所以仍会保留音频，便于重试时跳过下载或排查供应商错误。
        """
        if self._keep_audio_after_success:
            return

        task = self._repository.get_task(task_id)
        if not task.audio_file_path:
            return

        stored_audio_path = Path(task.audio_file_path)
        target_path = audio_path if audio_path is not None else stored_audio_path
        if target_path != stored_audio_path:
            target_path = stored_audio_path

        try:
            if target_path.exists() and target_path.is_file():
                target_path.unlink()
            self._repository.update_task_fields(task_id, audio_file_path="")
        except OSError as exc:
            LOGGER.warning("删除本地成功任务音频失败：%s", exc)

    def _load_existing_markdown(self, task: TaskRecord) -> tuple[str, Path] | None:
        """Return a completed Markdown artifact so retry can jump to mail delivery."""
        if not task.markdown_file_path:
            return None
        markdown_path = Path(task.markdown_file_path)
        if not markdown_path.exists() or not markdown_path.is_file():
            return None
        markdown_content = task.markdown_content
        if not markdown_content:
            markdown_content = markdown_path.read_text(encoding="utf-8")
        if not markdown_content.strip():
            return None
        return markdown_content, markdown_path

    def _complete_mail_step(self, task_id: int, title: str, markdown: str, markdown_path: Path) -> None:
        """Send or skip default mail according to the task's stored delivery mode."""
        task = self._repository.get_task(task_id)
        if task.send_mail:
            if task.mail_status is not MailStatus.SENT:
                self._set_status(task_id, TaskStatus.SENDING_MAIL)
                self._mail_service.send_markdown(title, markdown, markdown_path)
                self._repository.update_task_fields(
                    task_id,
                    mail_status=MailStatus.SENT.value,
                    last_checkpoint="mail_sent",
                )
            return

        if task.mail_status is not MailStatus.SENT:
            self._repository.update_task_fields(
                task_id,
                mail_status=MailStatus.SKIPPED.value,
                last_checkpoint="mail_skipped",
            )

