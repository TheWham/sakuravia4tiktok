"""Markdown file writing helpers."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ..config import AppConfig
from ..utils import sanitize_filename


class ArtifactService:
    """Persist generated Markdown under a deterministic naming scheme."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config

    def write_markdown(self, video_id: str, title: str, markdown: str) -> Path:
        """Store one Markdown artifact and return its final path."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_title = sanitize_filename(title)
        file_path = self._config.output_dir / f"{timestamp}_{video_id}_{safe_title}.md"
        file_path.write_text(markdown, encoding="utf-8")
        return file_path

