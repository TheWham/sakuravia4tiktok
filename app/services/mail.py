"""SMTP mail delivery service."""

from __future__ import annotations

import html
import re
import smtplib
from email.message import EmailMessage
from pathlib import Path

from ..config import AppConfig


class MailService:
    """Build and send the final mail with the Markdown file attached."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config

    def build_message(
        self,
        subject_title: str,
        markdown_content: str,
        attachment_path: Path,
        to_email: str | None = None,
    ) -> EmailMessage:
        """Create a readable mail body while keeping the Markdown file as an attachment."""
        # Strip linefeed/carriage return and all Unicode line separators so the
        # title is safe for email headers (RFC 5322 forbids bare CR/LF).
        safe_title = re.sub(r"[\r\n  \v\f]+", " ", subject_title).strip()
        message = EmailMessage()
        message["From"] = self._config.mail_from
        message["To"] = to_email or self._config.mail_to
        message["Subject"] = f"抖音视频总结 - {safe_title}"
        message.set_content(
            "\n".join(
                [
                    f"视频《{safe_title}》的 Markdown 总结如下，附件中也保留了一份 .md 文件。",
                    "",
                    markdown_content.strip(),
                ]
            )
        )
        message.add_alternative(self._build_html_body(subject_title, markdown_content), subtype="html")
        message.add_attachment(
            attachment_path.read_bytes(),
            maintype="text",
            subtype="markdown",
            filename=attachment_path.name,
        )
        return message

    def send_markdown(
        self,
        subject_title: str,
        markdown_content: str,
        attachment_path: Path,
        to_email: str | None = None,
    ) -> None:
        """Connect to SMTP and send the message using SSL or STARTTLS."""
        message = self.build_message(subject_title, markdown_content, attachment_path, to_email=to_email)
        if self._config.smtp_use_ssl:
            with smtplib.SMTP_SSL(self._config.smtp_host, self._config.smtp_port) as client:
                client.login(self._config.smtp_username, self._config.smtp_password)
                client.send_message(message)
            return

        with smtplib.SMTP(self._config.smtp_host, self._config.smtp_port) as client:
            client.starttls()
            client.login(self._config.smtp_username, self._config.smtp_password)
            client.send_message(message)

    def _build_html_body(self, subject_title: str, markdown_content: str) -> str:
        """Render Markdown into a simple HTML email that mobile mail clients can read directly."""
        return "\n".join(
            [
                "<!doctype html>",
                '<html lang="zh-CN">',
                "<body>",
                '<div style="font-family: -apple-system, BlinkMacSystemFont, Segoe UI, Microsoft YaHei, sans-serif; '
                'font-size: 16px; line-height: 1.75; color: #1f2933; padding: 8px;">',
                f'<p style="margin: 0 0 20px; color: #52606d;">视频《{html.escape(subject_title)}》的总结如下，附件中也保留了一份 .md 文件。</p>',
                self._markdown_to_html(markdown_content),
                "</div>",
                "</body>",
                "</html>",
            ]
        )

    def _markdown_to_html(self, markdown_content: str) -> str:
        """Convert the limited Markdown produced by the summarizer into email-friendly HTML."""
        lines = markdown_content.strip().splitlines()
        blocks: list[str] = []
        index = 0
        while index < len(lines):
            line = lines[index].strip()
            if not line:
                index += 1
                continue

            if self._is_table_start(lines, index):
                table_lines: list[str] = []
                while index < len(lines) and lines[index].strip().startswith("|"):
                    table_lines.append(lines[index].strip())
                    index += 1
                blocks.append(self._render_table(table_lines))
                continue

            heading = re.match(r"^(#{1,3})\s+(.+)$", line)
            if heading:
                level = len(heading.group(1))
                title = heading.group(2).strip()
                blocks.append(self._render_heading(level, title))
                index += 1
                continue

            if re.match(r"^[-*]\s+", line):
                items: list[str] = []
                while index < len(lines) and re.match(r"^[-*]\s+", lines[index].strip()):
                    item = re.sub(r"^[-*]\s+", "", lines[index].strip())
                    items.append(f"<li>{self._render_inline(item)}</li>")
                    index += 1
                blocks.append('<ul style="margin: 8px 0 16px 22px; padding: 0;">' + "".join(items) + "</ul>")
                continue

            if re.match(r"^\d+[.、]\s+", line):
                items = []
                while index < len(lines) and re.match(r"^\d+[.、]\s+", lines[index].strip()):
                    item = re.sub(r"^\d+[.、]\s+", "", lines[index].strip())
                    items.append(f"<li>{self._render_inline(item)}</li>")
                    index += 1
                blocks.append('<ol style="margin: 8px 0 16px 22px; padding: 0;">' + "".join(items) + "</ol>")
                continue

            paragraph_lines = [line]
            index += 1
            while index < len(lines) and lines[index].strip() and not self._starts_new_block(lines, index):
                paragraph_lines.append(lines[index].strip())
                index += 1
            paragraph = " ".join(paragraph_lines)
            blocks.append(self._render_paragraph(paragraph))
        return "\n".join(blocks)

    def _render_heading(self, level: int, title: str) -> str:
        """Render headings with light semantic colors based on their fixed symbol prefix."""
        if level == 1:
            style = "font-size: 26px; margin: 24px 0 18px; font-weight: 800; color: #102a43;"
            return f'<h1 style="{style}">{self._render_inline(title)}</h1>'

        base_style = "font-weight: 700; border-radius: 6px; padding: 8px 10px; border-left: 4px solid {border}; background: {bg}; color: {color};"
        palette = self._heading_palette(title)
        if level == 2:
            style = (
                "font-size: 21px; margin: 24px 0 12px; "
                + base_style.format(**palette)
            )
            return f'<h2 style="{style}">{self._render_inline(title)}</h2>'

        style = (
            "font-size: 18px; margin: 18px 0 8px; "
            + base_style.format(**palette)
        )
        return f'<h3 style="{style}">{self._render_inline(title)}</h3>'

    def _render_paragraph(self, paragraph: str) -> str:
        """Use a subtle callout block for risk, action and insight paragraphs."""
        palette = self._paragraph_palette(paragraph)
        if palette is None:
            return f'<p style="margin: 0 0 14px;">{self._render_inline(paragraph)}</p>'

        style = (
            "margin: 0 0 14px; padding: 10px 12px; border-radius: 6px; "
            "border-left: 4px solid {border}; background: {bg}; color: {color};"
        ).format(**palette)
        return f'<p style="{style}">{self._render_inline(paragraph)}</p>'

    def _heading_palette(self, title: str) -> dict[str, str]:
        """Map fixed section symbols to email-safe colors."""
        if title.startswith("⚠️"):
            return {"border": "#f59e0b", "bg": "#fff7ed", "color": "#7c2d12"}
        if title.startswith(("✅", "🚀")):
            return {"border": "#22c55e", "bg": "#f0fdf4", "color": "#14532d"}
        if title.startswith(("💡", "🧠")):
            return {"border": "#3b82f6", "bg": "#eff6ff", "color": "#1e3a8a"}
        if title.startswith(("📊", "🧩")):
            return {"border": "#8b5cf6", "bg": "#f5f3ff", "color": "#4c1d95"}
        if title.startswith(("📌", "🎯", "🧭")):
            return {"border": "#0ea5e9", "bg": "#f0f9ff", "color": "#0c4a6e"}
        return {"border": "#94a3b8", "bg": "#f8fafc", "color": "#1f2937"}

    def _paragraph_palette(self, paragraph: str) -> dict[str, str] | None:
        """Highlight paragraphs that intentionally start with a semantic symbol."""
        if paragraph.startswith("⚠️"):
            return {"border": "#f59e0b", "bg": "#fff7ed", "color": "#7c2d12"}
        if paragraph.startswith(("✅", "🚀")):
            return {"border": "#22c55e", "bg": "#f0fdf4", "color": "#14532d"}
        if paragraph.startswith(("💡", "🧠")):
            return {"border": "#3b82f6", "bg": "#eff6ff", "color": "#1e3a8a"}
        return None

    def _starts_new_block(self, lines: list[str], index: int) -> bool:
        """Tell the paragraph parser when the next Markdown block begins."""
        line = lines[index].strip()
        return (
            line.startswith("#")
            or line.startswith("|")
            or bool(re.match(r"^[-*]\s+", line))
            or bool(re.match(r"^\d+[.、]\s+", line))
            or self._is_table_start(lines, index)
        )

    def _is_table_start(self, lines: list[str], index: int) -> bool:
        """Detect a Markdown table by checking for a header row plus separator row."""
        if index + 1 >= len(lines):
            return False
        header = lines[index].strip()
        separator = lines[index + 1].strip()
        normalized_separator = separator.replace("|", "").replace(" ", "").strip()
        return header.startswith("|") and separator.startswith("|") and set(normalized_separator) <= {"-", ":"}

    def _render_table(self, table_lines: list[str]) -> str:
        """Render a compact Markdown table with borders that survive most email clients."""
        rows = [
            [cell.strip() for cell in line.strip().strip("|").split("|")]
            for line in table_lines
            if line.strip().startswith("|")
        ]
        if len(rows) < 2:
            return ""
        header = rows[0]
        body_rows = rows[2:]
        th_style = "border: 1px solid #d0d7de; padding: 8px; background: #f6f8fa; text-align: left;"
        td_style = "border: 1px solid #d0d7de; padding: 8px; vertical-align: top;"
        html_rows = [
            "<tr>" + "".join(f'<th style="{th_style}">{self._render_inline(cell)}</th>' for cell in header) + "</tr>"
        ]
        for row in body_rows:
            html_rows.append("<tr>" + "".join(f'<td style="{td_style}">{self._render_inline(cell)}</td>' for cell in row) + "</tr>")
        return '<table style="border-collapse: collapse; width: 100%; margin: 12px 0 20px;">' + "".join(html_rows) + "</table>"

    def _render_inline(self, text: str) -> str:
        """Handle the few inline styles the summarizer commonly emits."""
        escaped = html.escape(text)
        escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
        escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
        return escaped

