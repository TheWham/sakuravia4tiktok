"""DeepSeek-based Markdown summary generation."""

from __future__ import annotations

from ..config import AppConfig
from ..models import SummaryResult, TranscriptResult, VideoMetadata
from .http_client import SimpleHttpClient


SUMMARY_TEMPLATE = """请根据提供的视频元信息和转写内容，输出一份适合直接发到手机邮箱阅读、可以长期收藏的 Markdown 中文笔记。

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
10. 优先使用以下结构，但必须根据内容类型微调二级标题：
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
11. “视频信息”至少包含 抖音博主、视频链接、标签、内容类型、简介核心；未知信息写“未提及”。
12. 不要输出“# 视频标题”“## 视频概览”这种模板痕迹明显的标题。
13. 固定使用这些视觉符号，不要自创其他符号：📌 信息、🧠 原理、📊 数据/表格、✅ 实践/建议、⚠️ 风险/坑点、🧩 原则、🚀 行动、💡 AI理解与延伸、🎯 问题、🧭 场景。
14. “💡 AI 理解与延伸”必须放在事实总结之后，内容可以包含我的理解、实际使用场景、迁移到项目里的建议、可能的误区；必须明确基于已有内容合理推断，不能编造具体事实、链接、评论、价格或官方结论。

视频标题：{title}
视频链接：{webpage_url}
抖音博主：{uploader}
时长（秒）：{duration}
标签：{tags}
视频简介：{description}

转写内容：
{transcript}
"""


MERGE_TEMPLATE = """你会收到同一个视频的多段笔记，请将它们合并成一份最终 Markdown 邮件正文。

要求：
1. 只输出 Markdown。
2. 开头要直接进入主题，不要解释“我将合并”。
3. 去掉重复表达，保留事实；用途、适用场景、建议可以基于标题、简介、标签、条目名称做合理常识推断。
4. 如果分段笔记里出现清单、Prompt、工具、步骤或对比，最终结果必须保留或生成 Markdown 表格。
5. 标题要改写成主题型标题，不要用“视频标题”。
6. 技术实践类最终稿必须包含：📌 视频信息、🎯 背景问题、🧠 核心原理、📊 关键数据、✅ 最佳实践、⚠️ 反例与坑点、🧩 核心原则总结、🚀 可执行建议、💡 AI 理解与延伸。
7. 清单类最终稿必须包含：📌 视频信息、📊 核心清单表、🧭 应用场景总结、✅ 使用建议、💡 AI 理解与延伸。
8. 固定使用这些视觉符号，不要自创其他符号；“💡 AI 理解与延伸”必须和视频事实分开，避免把推断写成原视频事实。

视频标题：{title}
视频链接：{webpage_url}
抖音博主：{uploader}
标签：{tags}
视频简介：{description}

分段摘要片段：
{chunks}
"""


class SummaryService:
    """Generate markdown from transcript text and merge chunked summaries when needed."""

    def __init__(self, config: AppConfig, http_client: SimpleHttpClient) -> None:
        self._config = config
        self._http_client = http_client

    def summarize(self, metadata: VideoMetadata, transcript: TranscriptResult) -> SummaryResult:
        """Chunk long transcript text before asking DeepSeek for the final merged document."""
        chunks = self._split_text(transcript.full_text, max_chars=7000)
        tags = self._format_tags(metadata.tags)
        description = metadata.description.strip() or "未提及"
        if len(chunks) == 1:
            markdown = self._chat(
                SUMMARY_TEMPLATE.format(
                    title=metadata.title,
                    webpage_url=metadata.webpage_url,
                    uploader=metadata.uploader,
                    duration=metadata.duration,
                    tags=tags,
                    description=description,
                    transcript=chunks[0],
                )
            )
        else:
            partials = []
            for index, chunk in enumerate(chunks, start=1):
                partial = self._chat(
                    SUMMARY_TEMPLATE.format(
                        title=f"{metadata.title}（第{index}段素材）",
                        webpage_url=metadata.webpage_url,
                        uploader=metadata.uploader,
                        duration=metadata.duration,
                        tags=tags,
                        description=description,
                        transcript=chunk,
                    )
                )
                partials.append(partial)
            markdown = self._chat(
                MERGE_TEMPLATE.format(
                    title=metadata.title,
                    webpage_url=metadata.webpage_url,
                    uploader=metadata.uploader,
                    tags=tags,
                    description=description,
                    chunks="\n\n".join(partials),
                )
            )
        highlights = self._extract_highlights(markdown)
        return SummaryResult(markdown=markdown.strip(), title=metadata.title, highlights=highlights)

    def _chat(self, prompt: str) -> str:
        """Call DeepSeek's OpenAI-compatible chat API."""
        url = f"{self._config.deepseek_base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._config.deepseek_api_key}",
        }
        payload = {
            "model": self._config.deepseek_model,
            "messages": [
                {
                    "role": "system",
                    "content": "你是一名严谨的中文视频内容整理助手。",
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
            "temperature": 0.3,
        }
        response = self._http_client.post_json(url, headers, payload)
        choices = response.get("choices") or []
        if not choices:
            raise RuntimeError("DeepSeek 未返回可用结果。")
        message = choices[0].get("message") or {}
        content = str(message.get("content", "")).strip()
        if not content:
            raise RuntimeError("DeepSeek 返回内容为空。")
        return content

    def _split_text(self, text: str, max_chars: int) -> list[str]:
        """Split transcript text by paragraph boundaries to preserve context order."""
        paragraphs = [item.strip() for item in text.splitlines() if item.strip()]
        if not paragraphs:
            return [text]

        chunks: list[str] = []
        current: list[str] = []
        size = 0
        for paragraph in paragraphs:
            if current and size + len(paragraph) > max_chars:
                chunks.append("\n".join(current))
                current = []
                size = 0
            current.append(paragraph)
            size += len(paragraph)
        if current:
            chunks.append("\n".join(current))
        return chunks or [text]

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

