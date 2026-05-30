"""Resolve Douyin video metadata and download URL via Playwright browser automation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from ..config import AppConfig


_SCRIPT = r'''
import json, sys, os

def load_cookies(ctx, cookies_file):
    """Load Netscape-format cookies into browser context. Returns count loaded."""
    if not cookies_file or not os.path.exists(cookies_file):
        return 0
    cookies = []
    for line in open(cookies_file, encoding="utf-8", errors="ignore"):
        if line.startswith("#") or not line.strip():
            continue
        parts = line.strip().split("\t")
        if len(parts) >= 7:
            cookies.append({"domain": parts[0], "name": parts[5], "value": parts[6], "path": parts[2], "secure": parts[3].upper() == "TRUE"})
    if cookies:
        ctx.add_cookies(cookies)
    return len(cookies)


def try_extract(pw, aweme_id, video_url, cookies_file):
    """Attempt to load Douyin video page and capture API/RENDER_DATA.
    Returns (captured_dict, is_blocked)."""
    captured = {}
    browser = pw.chromium.launch(headless=True, args=["--no-proxy-server"])
    ctx = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        viewport={"width": 1920, "height": 1080},
    )

    if cookies_file:
        load_cookies(ctx, cookies_file)

    page = ctx.new_page()

    def on_resp(response):
        if "aweme/detail" in response.url:
            try:
                captured["api"] = response.json()
            except Exception:
                pass

    page.on("response", on_resp)
    page.goto(video_url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(8000)

    is_blocked = False
    try:
        title = page.title()
        if "验证" in title or "verify" in title.lower() or "captcha" in title.lower():
            is_blocked = True
    except Exception:
        pass

    # Detect "video not found" page.
    if not captured.get("api"):
        try:
            body_text = page.evaluate("() => document.body ? document.body.innerText.substring(0, 500) : ''")
            if "不存在" in body_text or "已失效" in body_text or "已删除" in body_text:
                captured["not_found"] = True
        except Exception:
            pass

    if not captured.get("api") and not captured.get("not_found"):
        try:
            rd = page.evaluate("() => { var el = document.getElementById('RENDER_DATA'); if (el) return decodeURIComponent(el.textContent); return null; }")
            if rd:
                captured["render"] = json.loads(rd)
        except Exception:
            pass

    browser.close()
    return captured, is_blocked


def main():
    from playwright.sync_api import sync_playwright

    aweme_id = sys.argv[1]
    cookies_file = sys.argv[2] if len(sys.argv) > 2 else ""
    video_url = f"https://www.douyin.com/video/{aweme_id}"

    with sync_playwright() as pw:
        # First attempt: with cookies if available
        captured, is_blocked = try_extract(pw, aweme_id, video_url, cookies_file)

        # If cookies caused verification page or no data, retry without cookies
        if not captured.get("api") and not captured.get("render") and cookies_file:
            if is_blocked:
                sys.stderr.write("Cookies triggered verification page, retrying without cookies\n")
            else:
                sys.stderr.write("No data with cookies, retrying without cookies\n")
            captured, _ = try_extract(pw, aweme_id, video_url, "")

    print(json.dumps(captured, ensure_ascii=False))

main()
'''


@dataclass(slots=True)
class DouyinBrowserResult:
    """Result extracted from Douyin video page via browser automation."""

    aweme_id: str
    title: str
    author: str
    video_url: str
    download_url: str
    duration_ms: int = 0
    subtitle_urls: list[str] | None = None


class DouyinBrowserResolver:
    """Use Playwright (via subprocess) to load Douyin video pages.

    Runs the Playwright script in a separate Python process to completely
    isolate it from the Uvicorn/anyio event loop that causes
    ``NotImplementedError`` on Windows.
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._cookies_file = config.douyin_cookies_file

    def resolve(self, aweme_id: str) -> DouyinBrowserResult:
        """Launch a subprocess that runs Playwright and returns video data."""
        cookies_path = str(self._cookies_file) if self._cookies_file else ""
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"} if hasattr(os, "environ") else None
        proc = subprocess.run(
            [sys.executable, "-c", _SCRIPT, aweme_id, cookies_path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            env=env,
        )

        if proc.returncode != 0:
            stderr = proc.stderr.strip()[-500:] if proc.stderr else "no stderr"
            raise RuntimeError(f"Playwright 浏览器解析失败 (code={proc.returncode}): {stderr}")

        try:
            captured = json.loads(proc.stdout)
        except json.JSONDecodeError:
            raise RuntimeError(f"Playwright 返回了无效的 JSON: {proc.stdout[:200]}")

        return self._parse_result(aweme_id, captured)

    def download_video(self, result: DouyinBrowserResult, output_dir: Path | None = None) -> Path:
        """Download the video to a local file and return the path."""
        target_dir = output_dir or self._config.tmp_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / f"{result.aweme_id}.mp4"

        url = result.download_url or result.video_url
        if not url:
            raise RuntimeError(f"抖音视频 {result.aweme_id} 无可用下载链接。")

        _download_with_timeout(url, str(target_path), 300)
        return target_path

    def _parse_result(self, aweme_id: str, data: dict) -> DouyinBrowserResult:
        api_data = data.get("api")
        if isinstance(api_data, dict):
            aweme = api_data.get("aweme_detail")
            if isinstance(aweme, dict) and aweme:
                return _extract_from_aweme(aweme_id, aweme)
            # API responded but aweme_detail is null — video doesn't exist.
            if aweme is None:
                raise RuntimeError(f"视频 {aweme_id} 不存在或已被删除。")

        render = data.get("render")
        if isinstance(render, dict):
            found = _find_aweme_in_render(render)
            if found:
                return _extract_from_aweme(aweme_id, found)

        if data.get("not_found"):
            raise RuntimeError(f"视频 {aweme_id} 不存在或已被删除。")

        raise RuntimeError(f"无法从抖音页面提取视频 {aweme_id} 的信息。请确认视频链接有效且 cookies 未过期。")


def _extract_from_aweme(aweme_id: str, aweme: dict) -> DouyinBrowserResult:
    video = aweme.get("video", {}) or {}
    play_addr = video.get("play_addr", {}) or {}
    download_addr = video.get("download_addr", {}) or {}
    author = aweme.get("author", {}) or {}
    play_urls = play_addr.get("url_list") or []
    download_urls = download_addr.get("url_list") or []
    caption_list = video.get("caption_info_list") or []
    subtitle_urls = [c["url"] for c in caption_list if isinstance(c, dict) and c.get("url")]

    return DouyinBrowserResult(
        aweme_id=aweme_id,
        title=str(aweme.get("desc", "")),
        author=str(author.get("nickname", "")),
        video_url=play_urls[0] if play_urls else "",
        download_url=download_urls[0] if download_urls else (play_urls[0] if play_urls else ""),
        duration_ms=int(video.get("duration", 0)),
        subtitle_urls=subtitle_urls or None,
    )


def _find_aweme_in_render(obj, depth=0):
    if depth > 7:
        return None
    if isinstance(obj, dict):
        if "desc" in obj and "video" in obj:
            return obj
        for v in list(obj.values())[:20]:
            found = _find_aweme_in_render(v, depth + 1)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj[:10]:
            found = _find_aweme_in_render(item, depth + 1)
            if found:
                return found
    return None


def _download_with_timeout(url: str, dest: str, timeout: int) -> None:
    from urllib.request import Request, urlopen
    req = Request(url, method="GET")
    req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36")
    req.add_header("Referer", "https://www.douyin.com/")
    with urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    Path(dest).write_bytes(data)
