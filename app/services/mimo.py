"""Xiaomi Mimo based Markdown generation."""

from __future__ import annotations

import logging
from pathlib import Path

from ..config import AppConfig
from ..models import SummaryResult, TranscriptResult, VideoMetadata
from .http_client import HttpRequestError, SimpleHttpClient
from .media import PublicMediaStorage, UploadedMedia
from .summary import SUMMARY_TEMPLATE


LOGGER = logging.getLogger(__name__)


MIMO_SYSTEM_PROMPT = "你是一名严谨的中文视频内容整理助手，只输出适合邮件阅读的 Markdown 正文。"


MIMO_MEDIA_TEMPLATE = """请根据提供的视频元信息、前面传入的{media_name}素材和下面的模板要求，输出一份适合直接发到手机邮箱阅读、可以长期收藏的 Markdown 中文笔记。

必须遵守以下要求：
1. 只输出 Markdown 正文，不要输出额外说明。
2. 先判断内容类型，再选择笔记结构：清单/Prompt类、技术实践类、教程步骤类、观点解读类。
3. 不要像转写稿复述，要像人工整理的专题笔记：提炼主题、归类、补充可执行用途。
4. 标题不要机械写“视频标题”，要改写成主题型标题，例如“2026年10个热门AI绘画 Prompt 盘点”。
5. 对视频明确说出的事实要忠实；对“用途/适用场景/建议”可以基于标题、简介、标签、条目名称做合理常识推断，但不要编造具体数据、链接、评论、人物或来源。
6. 如果视频是清单、盘点、TopN、Prompt 合集、工具合集，必须优先输出“核心清单表”，表格要有实用列，例如“序号 / 名称 / 主要用途 / 适用场景”。
7. 如果视频是技术实践或原理讲解，必须提炼背景问题、核心原理、关键数据、最佳实践、反例与坑点、核心原则总结。
8. 如果视频是教程步骤，必须输出步骤表、注意事项、可执行清单。
9. 短视频也不要写得太空：至少给出清单表、场景分组、使用建议。
10. “视频信息”至少包含 抖音博主、视频链接、标签、内容类型、简介核心；未知信息写“未提及”。
11. 不要输出“# 视频标题”“## 视频概览”这种模板痕迹明显的标题。
12. 固定使用这些视觉符号，不要自创其他符号：📌 信息、🧠 原理、📊 数据/表格、✅ 实践/建议、⚠️ 风险/坑点、🧩 原则、🚀 行动、💡 AI理解与延伸、🎯 问题、🧭 场景。
13. “💡 AI 理解与延伸”必须放在事实总结之后，内容可以包含我的理解、实际使用场景、迁移到项目里的建议、可能的误区；必须明确基于已有内容合理推断，不能编造具体事实、链接、评论、价格或官方结论。
14. 如果{media_name}里没有可理解的有效内容，请明确写“无法识别有效内容”，不要硬编。

推荐结构：
清单/Prompt类：
- # 主题型标题
- ## 📌 视频信息
- ## 📊 核心清单表
- ## 🧭 应用场景总结
- ## ✅ 使用建议
- ## 💡 AI 理解与延伸

技术实践类：
- # 主题型标题
- ## 📌 视频信息
- ## 🎯 背景问题
- ## 🧠 核心原理
- ## 📊 关键数据
- ## ✅ 最佳实践
- ## ⚠️ 反例与坑点
- ## 🧩 核心原则总结
- ## 🚀 可执行建议
- ## 💡 AI 理解与延伸

教程步骤类：
- # 主题型标题
- ## 📌 视频信息
- ## 📊 操作步骤表
- ## ⚠️ 注意事项
- ## ✅ 可执行清单
- ## 🚀 延伸建议
- ## 💡 AI 理解与延伸

视频标题：{title}
视频链接：{webpage_url}
抖音博主：{uploader}
时长（秒）：{duration}
标签：{tags}
视频简介：{description}
"""


class MimoNoValidAudioError(RuntimeError):
    """Raised when Mimo says the uploaded audio has no usable content."""


class MimoSummaryService:
    """Generate final Markdown with Xiaomi Mimo text/audio/video understanding."""

    _NO_VALID_AUDIO_MARKERS = (
        "无语音",
        "没有语音",
        "没有有效语音",
        "无法识别有效内容",
        "没有有效音频",
        "音频为空",
        "静音",
        "听不清",
        "无法转写",
        "无法理解音频",
    )

    def __init__(
        self,
        config: AppConfig,
        http_client: SimpleHttpClient,
        media_storage: PublicMediaStorage,
    ) -> None:
        self._config = config
        self._http_client = http_client
        self._media_storage = media_storage

    def summarize_text(self, metadata: VideoMetadata, transcript: TranscriptResult) -> SummaryResult:
        """Send official subtitle text to Mimo and return the final Markdown."""
        prompt = SUMMARY_TEMPLATE.format(
            title=metadata.title,
            webpage_url=metadata.webpage_url,
            uploader=metadata.uploader,
            duration=metadata.duration,
            tags=self._format_tags(metadata.tags),
            description=metadata.description.strip() or "未提及",
            transcript=transcript.full_text,
        )
        markdown = self._chat([{"role": "user", "content": prompt}])
        return self._to_summary(metadata, markdown)

    def summarize_audio(self, metadata: VideoMetadata, audio_path: Path) -> SummaryResult:
        """Upload audio to OSS, let Mimo understand it, and delete the temporary object."""
        uploaded = self._upload_media(audio_path, "mimo/audio")
        try:
            try:
                markdown = self._chat(
                    [
                        {
                            "role": "user",
                            "content": [
                                {"type": "input_audio", "input_audio": {"data": uploaded.file_url}},
                                {"type": "text", "text": self._build_media_prompt(metadata, "音频")},
                            ],
                        }
                    ]
                )
            except HttpRequestError as exc:
                if "内容为空" in str(exc):
                    raise MimoNoValidAudioError("Mimo 音频理解返回空 Markdown。") from exc
                if self._is_invalid_audio_format_error(exc):
                    raise MimoNoValidAudioError(f"Mimo 拒绝当前音频格式：{exc}") from exc
                raise
            if self.is_no_valid_audio_markdown(markdown):
                raise MimoNoValidAudioError("Mimo 音频理解返回无有效语音或无法识别有效内容。")
            return self._to_summary(metadata, markdown)
        finally:
            self._delete_uploaded_media(uploaded)

    def summarize_video(self, metadata: VideoMetadata, video_path: Path) -> SummaryResult:
        """Upload video to OSS and let Mimo generate the final Markdown directly."""
        uploaded = self._upload_media(video_path, "mimo/video")
        try:
            markdown = self._chat(
                [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "video_url",
                                "video_url": {"url": uploaded.file_url},
                                "fps": self._config.mimo_video_fps,
                                "media_resolution": self._config.mimo_video_resolution,
                            },
                            {"type": "text", "text": self._build_media_prompt(metadata, "视频")},
                        ],
                    }
                ]
            )
            return self._to_summary(metadata, markdown)
        finally:
            self._delete_uploaded_media(uploaded)

    def is_no_valid_audio_markdown(self, markdown: str) -> bool:
        """Return whether a Mimo audio response indicates unusable audio."""
        text = markdown.strip().lower()[:800]
        return any(marker.lower() in text for marker in self._NO_VALID_AUDIO_MARKERS)

    def _is_invalid_audio_format_error(self, exc: HttpRequestError) -> bool:
        """Return whether Mimo rejected the uploaded URL as an audio format issue.

        官方文档要求 URL 音频必须是 MP3/WAV/FLAC/M4A/OGG。即使本地已经尽量
        转成 MP3，网关仍可能根据远端对象头、编码细节或代理响应判断失败；
        对 V4 的 auto 模式来说，这类错误应视为“音频路径不可用”，交给任务
        编排层回退视频理解。
        """
        message = str(exc).lower()
        return (
            "invalid audio format" in message
            or "only mp3/flac/m4a/wav/ogg" in message
            or "multimodal data is corrupted" in message
            or "cannot be processed" in message
        )

    def _chat(self, user_messages: list[dict[str, object]]) -> str:
        """Call Mimo's OpenAI-compatible chat API and return assistant content."""
        self._validate_config()
        url = f"{self._config.mimo_base_url.rstrip('/')}/chat/completions"
        headers = {"api-key": self._config.mimo_api_key}
        payload = {
            "model": self._config.mimo_model,
            "messages": [{"role": "system", "content": MIMO_SYSTEM_PROMPT}, *user_messages],
            "max_completion_tokens": self._config.mimo_max_completion_tokens,
            "temperature": 0.3,
            "stream": False,
            "thinking": {"type": "disabled"},
        }
        response = self._http_client.post_json(url, headers, payload)
        choices = response.get("choices") or []
        if not choices:
            raise HttpRequestError("Mimo 未返回可用结果。")
        message = choices[0].get("message") or {}
        if not isinstance(message, dict):
            raise HttpRequestError("Mimo 返回格式异常。")
        content = str(message.get("content") or "").strip()
        if not content:
            raise HttpRequestError("Mimo 返回内容为空。")
        return content

    def _validate_config(self) -> None:
        """Validate required Mimo settings before uploading media or calling API."""
        missing = []
        if not self._config.mimo_api_key:
            missing.append("MIMO_API_KEY")
        if not self._config.mimo_base_url:
            missing.append("MIMO_BASE_URL")
        if not self._config.mimo_model:
            missing.append("MIMO_MODEL")
        if missing:
            raise RuntimeError(f"Mimo 总结缺少配置：{', '.join(missing)}")

    def _upload_media(self, file_path: Path, object_prefix: str) -> UploadedMedia:
        """Upload media through the shared OSS storage."""
        return self._media_storage.upload_file(file_path, object_prefix)

    def _delete_uploaded_media(self, uploaded: UploadedMedia) -> None:
        """Best-effort OSS cleanup; cleanup failure must not hide model errors."""
        try:
            self._media_storage.delete_file(uploaded.object_key)
        except Exception as exc:  # noqa: BLE001 - cleanup must never mask the main task result.
            LOGGER.warning("删除 Mimo OSS 临时媒体失败：%s", exc)

    def _build_media_prompt(self, metadata: VideoMetadata, media_name: str) -> str:
        """Build the prompt paired with an input_audio or video_url message part."""
        return MIMO_MEDIA_TEMPLATE.format(
            media_name=media_name,
            title=metadata.title,
            webpage_url=metadata.webpage_url,
            uploader=metadata.uploader,
            duration=metadata.duration,
            tags=self._format_tags(metadata.tags),
            description=metadata.description.strip() or "未提及",
        )

    def _to_summary(self, metadata: VideoMetadata, markdown: str) -> SummaryResult:
        """Normalize Markdown output into the service result used by the task flow."""
        content = markdown.strip()
        return SummaryResult(markdown=content, title=metadata.title, highlights=self._extract_highlights(content))

    def _extract_highlights(self, markdown: str) -> list[str]:
        """Pull a few bullet lines for the UI list without parsing full Markdown."""
        highlights: list[str] = []
        for line in markdown.splitlines():
            stripped = line.strip()
            if stripped.startswith("- ") or stripped.startswith("* "):
                highlights.append(stripped[2:].strip())
            if len(highlights) >= 5:
                break
        return highlights

    def _format_tags(self, tags: list[str]) -> str:
        """Keep tag context compact so it helps the model without crowding the prompt."""
        if not tags:
            return "未提及"
        return "、".join(tags[:12])

