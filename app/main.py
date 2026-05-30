"""FastAPI entrypoint for the local Douyin AI assistant."""

from __future__ import annotations

import json
from html import escape

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .config import AppConfig
from .models import TaskStatus
from .storage import TaskRepository
from .utils import ValidationError
from .services.artifact import ArtifactService
from .services.audio import SubtitleOrAudioService
from .services.douyin import DouyinResolverService
from .services.douyin_browser import DouyinBrowserResolver
from .services.douyin_listener import DouyinAuthService, DouyinEventService, DouyinHttpClient, DouyinMentionApi, DouyinMentionItem, DouyinMentionPoller
from .services.http_client import SimpleHttpClient
from .services.mail import MailService
from .services.media import AudioProbeService, OssMediaStorage
from .services.mimo import MimoSummaryService
from .services.process_runner import ProcessExecutionError, ProcessRunner
from .services.summary import SummaryService
from .services.task_service import TaskService
from .services.transcription import TranscriptionService


class TaskCreateRequest(BaseModel):
    """Incoming JSON payload for task creation."""

    source: str


class VideoOptionsRequest(BaseModel):
    """Incoming JSON payload for checking selectable video parts."""

    source: str


class DouyinUserEmailRequest(BaseModel):
    """Incoming JSON payload for a local 抖音用户邮箱 binding."""

    uid: str
    username: str
    email: str


class DouyinEventImportRequest(BaseModel):
    """Manual import of a douyin @ mention event when polling API is unavailable."""

    comment_id: str = ""
    sender_mid: str
    sender_name: str = ""
    content: str = ""
    video_id: str = ""


def build_app() -> FastAPI:
    """Create the application and wire all services once at import time."""
    config = AppConfig.load()
    config.ensure_directories()

    repository = TaskRepository(config.sqlite_path)
    repository.init_db()

    process_runner = ProcessRunner()
    http_client = SimpleHttpClient()
    douyin_browser_resolver = DouyinBrowserResolver(config)
    Douyin_service = DouyinResolverService(config, douyin_browser_resolver)
    subtitle_audio_service = SubtitleOrAudioService(config, process_runner, douyin_browser_resolver)
    transcription_service = TranscriptionService(config, http_client, subtitle_audio_service)
    summary_service = SummaryService(config, http_client)
    mimo_summary_service = None
    if config.summary_provider.strip().lower() == "mimo":
        mimo_summary_service = MimoSummaryService(config, http_client, OssMediaStorage(config))
    elif config.summary_provider.strip().lower() != "deepseek":
        raise RuntimeError(f"不支持的 SUMMARY_PROVIDER：{config.summary_provider}")
    audio_probe_service = AudioProbeService(config, process_runner)
    artifact_service = ArtifactService(config)
    mail_service = MailService(config)
    task_service = TaskService(
        repository=repository,
        Douyin_service=Douyin_service,
        subtitle_audio_service=subtitle_audio_service,
        transcription_service=transcription_service,
        summary_service=summary_service,
        artifact_service=artifact_service,
        mail_service=mail_service,
        keep_audio_after_success=config.keep_audio_after_success,
        mimo_summary_service=mimo_summary_service,
        audio_probe_service=audio_probe_service,
        mimo_media_mode=config.mimo_media_mode,
    )
    task_service.recover_interrupted_tasks()
    Douyin_http_client = DouyinHttpClient(config)
    Douyin_auth_service = DouyinAuthService(Douyin_http_client)
    Douyin_mention_api = DouyinMentionApi(Douyin_http_client, config.douyin_self_mid)
    Douyin_event_service = DouyinEventService(repository, task_service, mail_service)
    Douyin_poller = DouyinMentionPoller(config, Douyin_auth_service, Douyin_mention_api, Douyin_event_service)

    app = FastAPI(title="个人版 抖音 AI 助手", version="1.0.0")
    app.state.task_service = task_service
    app.state.repository = repository
    app.state.douyin_poller = Douyin_poller
    app.state.douyin_event_service = Douyin_event_service
    app.state.config = config

    @app.on_event("startup")
    def start_Douyin_listener() -> None:
        """Start the optional 抖音 listener after FastAPI finishes booting."""
        Douyin_poller.start()

    @app.on_event("shutdown")
    def stop_Douyin_listener() -> None:
        """Stop the 抖音 listener so the process exits cleanly."""
        Douyin_poller.stop()

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        """Render the single-page local UI."""
        tasks = [task.to_dict() for task in task_service.list_tasks()]
        selected = tasks[0] if tasks else None
        listener_state = Douyin_poller.snapshot().to_dict()
        return HTMLResponse(_render_index_html(tasks, selected, listener_state))

    @app.get("/v2/users", response_class=HTMLResponse)
    def v2_users() -> HTMLResponse:
        """Render the V2 抖音用户分析 page."""
        users = _build_Douyin_user_payloads(repository)
        selected_uid = _select_default_Douyin_uid(users)
        listener_state = Douyin_poller.snapshot().to_dict()
        return HTMLResponse(_render_v2_users_html(listener_state, users, selected_uid))

    @app.post("/api/tasks")
    def create_task(payload: TaskCreateRequest) -> dict[str, object]:
        """Create a task or return the running duplicate for the same BV id."""
        try:
            task = task_service.submit_task(payload.source)
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"task_id": task.id, "task": task.to_dict()}

    @app.post("/api/video-options")
    def inspect_video_options(payload: VideoOptionsRequest) -> dict[str, object]:
        """Return selectable entries before a task is created."""
        try:
            video_id, title, parts = Douyin_service.inspect_parts(payload.source)
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ProcessExecutionError as exc:
            raise HTTPException(status_code=502, detail=f"抖音视频解析失败：{exc}") from exc
        return {"video_id": video_id, "title": title, "parts": [part.to_dict() for part in parts]}

    @app.get("/api/tasks")
    def list_tasks() -> dict[str, object]:
        """Return the latest tasks for page polling."""
        return {"tasks": [task.to_dict() for task in task_service.list_tasks()]}

    @app.post("/api/tasks/{task_id}/retry")
    def retry_task(task_id: int) -> dict[str, object]:
        """Retry one failed task while keeping reusable local artifacts."""
        try:
            task = task_service.retry_task(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="任务不存在。") from exc
        return {"task_id": task.id, "task": task.to_dict()}

    @app.get("/api/Douyin-listener")
    def get_Douyin_listener() -> dict[str, object]:
        """Return 抖音 listener state and recent mention events."""
        Douyin_event_service.sync_task_statuses()
        return {
            "state": Douyin_poller.snapshot().to_dict(),
            "events": [event.to_dict() for event in repository.list_douyin_events()],
        }

    @app.post("/api/douyin/events/import")
    def import_douyin_event(payload: DouyinEventImportRequest) -> dict[str, object]:
        """Manually import a douyin @ mention event when the polling API is unavailable."""
        notification_id = f"manual:{payload.sender_mid}:{payload.comment_id or 'direct'}"
        mention = DouyinMentionItem(
            notification_id=notification_id,
            comment_id=payload.comment_id,
            sender_mid=payload.sender_mid,
            sender_name=payload.sender_name,
            content=payload.content,
            mentions_self=True,
            timestamp_seconds=0,
        )
        Douyin_event_service.process_mentions([mention])
        event = repository.get_douyin_event_by_notification_id(notification_id)
        return {"event": event.to_dict()}

    @app.get("/api/Douyin-users")
    def list_Douyin_users() -> dict[str, object]:
        """Return local 抖音用户邮箱 bindings with lightweight stats."""
        return {"users": _build_Douyin_user_payloads(repository)}

    @app.post("/api/Douyin-users")
    def upsert_Douyin_user(payload: DouyinUserEmailRequest) -> dict[str, object]:
        """Create or update one local 抖音用户邮箱 binding."""
        uid = payload.uid.strip()
        email = payload.email.strip()
        if not uid:
            raise HTTPException(status_code=400, detail="请输入 抖音用户 UID。")
        if not email or "@" not in email:
            raise HTTPException(status_code=400, detail="请输入有效邮箱。")
        user = repository.upsert_douyin_user_email(uid, payload.username, email)
        stats = repository.get_douyin_user_stats()
        return {"user": _build_Douyin_user_payload(user.to_dict(), stats)}

    @app.delete("/api/Douyin-users/{uid}")
    def delete_Douyin_user(uid: str) -> dict[str, object]:
        """Delete one local 抖音用户邮箱 binding."""
        repository.delete_douyin_user_email(uid)
        return {"ok": True}

    @app.get("/api/Douyin-users/{uid}/events")
    def list_Douyin_user_events(uid: str) -> dict[str, object]:
        """Return recent @ events and task snapshots for one 抖音用户 UID."""
        Douyin_event_service.sync_task_statuses()
        events = repository.list_douyin_events_by_sender_mid(uid)
        task_ids = [event.task_id for event in events if event.task_id is not None]
        task_map = repository.get_task_summary_map(task_ids)
        payloads: list[dict[str, object]] = []
        for event in events:
            item = event.to_dict()
            item["task"] = task_map.get(event.task_id) if event.task_id is not None else None
            payloads.append(item)
        return {"uid": uid.strip(), "events": payloads}

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: int) -> dict[str, object]:
        """Return one task with the full Markdown content and error state."""
        try:
            task = task_service.get_task(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="任务不存在。") from exc
        return {"task": task.to_dict()}

    return app


def _render_index_html(
    tasks: list[dict[str, object]],
    selected: dict[str, object] | None,
    listener_state: dict[str, object],
) -> str:
    """Render a lightweight HTML page without adding a template dependency."""
    selected_markdown = escape(str((selected or {}).get("markdown_content", "")))
    selected_path = escape(str((selected or {}).get("markdown_file_path", "")))
    selected_error = escape(str((selected or {}).get("error_message", "")))
    selected_title = escape(str((selected or {}).get("video_title", "")))
    selected_status = escape(str((selected or {}).get("status", "")))
    selected_mail_status = escape(str((selected or {}).get("mail_status", "")))
    retry_display = "" if selected_status == TaskStatus.FAILED.value else "display:none;"

    items = []
    for task in tasks:
        task_title = escape(str(task.get("video_title") or task.get("video_id") or f"任务 {task.get('id')}"))
        items.append(
            "\n".join(
                [
                    f'<button class="task-item" data-task-id="{task["id"]}">',
                    f'  <span class="task-title">{task_title}</span>',
                    f'  <span class="task-meta">#{task["id"]} {escape(str(task["status"]))}</span>',
                    "</button>",
                ]
            )
        )
    task_html = "\n".join(items) or '<div class="empty">还没有任务，先提交一个视频。</div>'
    listener_status = escape(str(listener_state.get("login_status", "UNKNOWN")))
    listener_running = "运行中" if listener_state.get("running") else "未运行"
    listener_error = escape(str(listener_state.get("last_error", "")))
    listener_next_poll = escape(str(listener_state.get("next_poll_at") or "暂无"))
    listener_interval = escape(str(listener_state.get("last_interval_seconds") or "暂无"))
    listener_paused = escape(str(listener_state.get("paused_reason") or "无"))

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>个人版 抖音 AI 助手</title>
  <style>
    :root {{
      --bg: #f3f5f8;
      --panel: #ffffff;
      --line: #d9e0e8;
      --text: #16202a;
      --muted: #5e6b78;
      --accent: #0086ff;
      --accent-soft: #e9f3ff;
      --danger: #cb334d;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      background: linear-gradient(180deg, #eef4fb 0%, #f7f9fc 100%);
      color: var(--text);
    }}
    .shell {{
      max-width: 1400px;
      margin: 0 auto;
      padding: 24px;
      display: grid;
      grid-template-columns: 360px 1fr;
      gap: 20px;
      min-height: 100vh;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: 0 10px 30px rgba(17, 31, 44, 0.06);
    }}
    .left {{
      padding: 20px;
      display: flex;
      flex-direction: column;
      gap: 18px;
    }}
    .right {{
      padding: 20px;
      display: flex;
      flex-direction: column;
      gap: 16px;
    }}
    h1 {{
      margin: 0;
      font-size: 24px;
      font-weight: 700;
    }}
    .sub {{
      color: var(--muted);
      line-height: 1.6;
      font-size: 14px;
    }}
    form {{
      display: flex;
      flex-direction: column;
      gap: 12px;
    }}
    input {{
      width: 100%;
      padding: 12px 14px;
      border: 1px solid var(--line);
      border-radius: 8px;
      font-size: 14px;
    }}
    button.submit {{
      border: 0;
      border-radius: 8px;
      padding: 12px 14px;
      background: var(--accent);
      color: #fff;
      font-size: 14px;
      cursor: pointer;
    }}
    button.secondary {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px 12px;
      background: #fff;
      color: var(--text);
      font-size: 14px;
      cursor: pointer;
    }}
    .part-panel {{
      display: none;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfcfe;
      padding: 12px;
      gap: 8px;
      flex-direction: column;
    }}
    .part-panel.visible {{
      display: flex;
    }}
    .part-list {{
      display: flex;
      flex-direction: column;
      gap: 8px;
      max-height: 260px;
      overflow: auto;
    }}
    .part-item {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 10px;
      text-align: left;
      cursor: pointer;
    }}
    .part-item:hover {{
      border-color: var(--accent);
      background: var(--accent-soft);
    }}
    .part-title {{
      font-size: 14px;
      font-weight: 600;
      line-height: 1.5;
    }}
    .tasks {{
      display: flex;
      flex-direction: column;
      gap: 8px;
      max-height: 58vh;
      overflow: auto;
    }}
    .listener {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfcfe;
      padding: 12px;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }}
    .event-list {{
      display: flex;
      flex-direction: column;
      gap: 8px;
      max-height: 240px;
      overflow: auto;
    }}
    .event-item {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 10px;
      text-align: left;
      cursor: pointer;
    }}
    .event-item.active {{
      border-color: var(--accent);
      background: var(--accent-soft);
    }}
    .user-list {{
      display: flex;
      flex-direction: column;
      gap: 8px;
      max-height: 220px;
      overflow: auto;
    }}
    .user-item {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 10px;
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 8px;
      align-items: center;
    }}
    .delete-user {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      color: var(--danger);
      padding: 8px 10px;
      cursor: pointer;
    }}
    .task-item {{
      width: 100%;
      text-align: left;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 12px;
      cursor: pointer;
      display: flex;
      flex-direction: column;
      gap: 4px;
    }}
    .task-item.active {{
      border-color: var(--accent);
      background: var(--accent-soft);
    }}
    .task-title {{
      font-size: 14px;
      font-weight: 600;
      line-height: 1.5;
    }}
    .task-meta, .meta-line {{
      color: var(--muted);
      font-size: 13px;
    }}
    .empty {{
      color: var(--muted);
      font-size: 14px;
      padding: 12px;
      border: 1px dashed var(--line);
      border-radius: 8px;
    }}
    .status-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }}
    .status-box {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      background: #fbfcfe;
    }}
    pre {{
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      font-size: 14px;
      line-height: 1.7;
      min-height: 420px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
      background: #fafcff;
    }}
    .danger {{
      color: var(--danger);
    }}
    .manage-link {{
      display: inline-flex;
      width: fit-content;
      align-items: center;
      padding: 8px 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      color: var(--accent);
      text-decoration: none;
      background: #fff;
      font-size: 13px;
    }}
    @media (max-width: 980px) {{
      .shell {{
        grid-template-columns: 1fr;
      }}
    }}
  </style>
</head>
<body>
  <div class="shell">
    <section class="panel left">
      <div>
        <h1>个人版 抖音 AI 助手</h1>
        <div class="sub">输入视频链接或 awemeId，系统会优先抓字幕，没有字幕再下载音频转写，最后生成 Markdown 并发邮件。</div>
      </div>
      <form id="task-form">
        <input id="source-input" name="source" placeholder="例如：https://www.douyin.com/video/1234567890123456789" />
        <button class="submit" type="submit">解析并创建任务</button>
        <div id="form-message" class="meta-line"></div>
      </form>
      <div id="part-panel" class="part-panel">
        <div class="meta-line" id="part-panel-title">选择要处理的视频条目</div>
        <div id="part-list" class="part-list"></div>
      </div>
      <div class="listener">
        <div class="sub">抖音监听</div>
        <div class="meta-line">状态：<span id="Douyin-login-status">{listener_status}</span> / <span id="Douyin-running-status">{listener_running}</span></div>
        <div class="meta-line">账号：<span id="Douyin-account">暂无</span></div>
        <div class="meta-line">最近轮询：<span id="Douyin-last-poll">暂无</span></div>
        <div class="meta-line">下次轮询：<span id="Douyin-next-poll">{listener_next_poll}</span></div>
        <div class="meta-line">本次间隔：<span id="Douyin-last-interval">{listener_interval}</span> 秒</div>
        <div class="meta-line">暂停原因：<span id="Douyin-paused-reason">{listener_paused}</span></div>
        <div id="Douyin-error" class="danger">{listener_error or "无"}</div>
        <a class="manage-link" href="/v2/users">查看用户状态</a>
      </div>
      <div class="sub">最近任务</div>
      <div id="task-list" class="tasks">{task_html}</div>
    </section>
    <section class="panel right">
      <div class="status-grid">
        <div class="status-box">
          <div class="meta-line">视频标题</div>
          <div id="task-title">{selected_title or "暂无"}</div>
        </div>
        <div class="status-box">
          <div class="meta-line">任务状态</div>
          <div id="task-status">{selected_status or "暂无"}</div>
        </div>
        <div class="status-box">
          <div class="meta-line">邮件状态</div>
          <div id="mail-status">{selected_mail_status or "暂无"}</div>
        </div>
        <div class="status-box">
          <div class="meta-line">文件路径</div>
          <div id="task-path">{selected_path or "暂无"}</div>
        </div>
      </div>
      <div>
        <div class="meta-line">错误信息</div>
        <div id="task-error" class="danger">{selected_error or "无"}</div>
        <button id="retry-task-button" class="secondary" type="button" style="margin-top:10px;{retry_display}">重试当前任务</button>
      </div>
      <div>
        <div class="meta-line">Markdown 预览</div>
        <pre id="markdown-preview">{selected_markdown or "暂无结果"}</pre>
      </div>
    </section>
  </div>
  <script>
    let selectedTaskId = {selected["id"] if selected else "null"};

    async function fetchTasks() {{
      const response = await fetch('/api/tasks');
      const payload = await response.json();
      renderTaskList(payload.tasks || []);
      if (selectedTaskId) {{
        await fetchTaskDetail(selectedTaskId);
      }} else if ((payload.tasks || []).length > 0) {{
        selectedTaskId = payload.tasks[0].id;
        await fetchTaskDetail(selectedTaskId);
      }}
    }}

    async function fetchDouyinListener() {{
      const response = await fetch('/api/Douyin-listener');
      if (!response.ok) {{
        return;
      }}
      const payload = await response.json();
      renderDouyinListener(payload.state || {{}}, payload.events || []);
    }}

    function renderTaskList(tasks) {{
      const container = document.getElementById('task-list');
      if (!tasks.length) {{
        container.innerHTML = '<div class="empty">还没有任务，先提交一个视频。</div>';
        return;
      }}
      container.innerHTML = tasks.map((task) => {{
        const title = task.video_title || task.video_id || ('任务 ' + task.id);
        const active = task.id === selectedTaskId ? 'active' : '';
        return `
          <button class="task-item ${{active}}" data-task-id="${{task.id}}">
            <span class="task-title">${{escapeHtml(title)}}</span>
            <span class="task-meta">#${{task.id}} ${{escapeHtml(task.status)}}</span>
          </button>
        `;
      }}).join('');
      container.querySelectorAll('[data-task-id]').forEach((button) => {{
        button.addEventListener('click', async () => {{
          selectedTaskId = Number(button.dataset.taskId);
          await fetchTaskDetail(selectedTaskId);
          renderTaskList(tasks);
        }});
      }});
    }}

    function renderDouyinListener(state) {{
      document.getElementById('Douyin-login-status').textContent = state.login_status || 'UNKNOWN';
      document.getElementById('Douyin-running-status').textContent = state.running ? '运行中' : '未运行';
      document.getElementById('Douyin-account').textContent = state.account_name
        ? `${{state.account_name}} (${{state.account_mid || 'unknown'}})`
        : '暂无';
      document.getElementById('Douyin-last-poll').textContent = state.last_poll_at || '暂无';
      document.getElementById('Douyin-next-poll').textContent = state.next_poll_at || '暂无';
      document.getElementById('Douyin-last-interval').textContent = state.last_interval_seconds || '暂无';
      document.getElementById('Douyin-paused-reason').textContent = state.paused_reason || '无';
      document.getElementById('Douyin-error').textContent = state.last_error || '无';
    }}

    async function fetchTaskDetail(taskId) {{
      const response = await fetch(`/api/tasks/${{taskId}}`);
      if (!response.ok) {{
        return;
      }}
      const payload = await response.json();
      const task = payload.task;
      document.getElementById('task-title').textContent = task.video_title || task.video_id || '暂无';
      document.getElementById('task-status').textContent = task.status || '暂无';
      document.getElementById('mail-status').textContent = task.mail_status || '暂无';
      document.getElementById('task-path').textContent = task.markdown_file_path || '暂无';
      document.getElementById('task-error').textContent = task.error_message || '无';
      document.getElementById('markdown-preview').textContent = task.markdown_content || '暂无结果';
      document.getElementById('retry-task-button').style.display = task.status === 'FAILED' ? 'inline-block' : 'none';
    }}

    function escapeHtml(value) {{
      return String(value)
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
    }}

    async function createTaskFromSource(source) {{
      const message = document.getElementById('form-message');
      message.textContent = '任务已提交，正在创建。';
      const response = await fetch('/api/tasks', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ source }})
      }});
      const payload = await response.json();
      if (!response.ok) {{
        message.textContent = payload.detail || '提交失败。';
        return;
      }}
      selectedTaskId = payload.task_id;
      document.getElementById('source-input').value = '';
      hidePartPanel();
      message.textContent = `任务 #${{payload.task_id}} 已创建。`;
      await fetchTasks();
    }}

    function renderPartOptions(title, parts) {{
      const panel = document.getElementById('part-panel');
      const panelTitle = document.getElementById('part-panel-title');
      const list = document.getElementById('part-list');
      panelTitle.textContent = `${{title}}：请选择要处理的视频条目`;
      list.innerHTML = parts.map((part) => `
        <button class="part-item" type="button" data-url="${{escapeHtml(part.url)}}">
          <div class="part-title">P${{part.index}} ${{escapeHtml(part.title)}}</div>
          <div class="task-meta">${{formatDuration(part.duration)}} · 点击后创建这个条目的总结任务</div>
        </button>
      `).join('');
      list.querySelectorAll('[data-url]').forEach((button) => {{
        button.addEventListener('click', async () => {{
          await createTaskFromSource(button.dataset.url);
        }});
      }});
      panel.classList.add('visible');
    }}

    function hidePartPanel() {{
      document.getElementById('part-panel').classList.remove('visible');
      document.getElementById('part-list').innerHTML = '';
    }}

    function formatDuration(seconds) {{
      const total = Number(seconds || 0);
      if (!total) {{
        return '时长未知';
      }}
      const minutes = Math.floor(total / 60);
      const rest = total % 60;
      return `${{minutes}}:${{String(rest).padStart(2, '0')}}`;
    }}

    document.getElementById('task-form').addEventListener('submit', async (event) => {{
      event.preventDefault();
      const source = document.getElementById('source-input').value.trim();
      const message = document.getElementById('form-message');
      if (!source) {{
        message.textContent = '请输入视频链接或 awemeId。';
        return;
      }}

      hidePartPanel();
      message.textContent = '正在解析视频条目。';
      const response = await fetch('/api/video-options', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ source }})
      }});
      const payload = await response.json();
      if (!response.ok) {{
        message.textContent = payload.detail || '解析失败。';
        return;
      }}
      const parts = payload.parts || [];
      if (parts.length > 1) {{
        message.textContent = `检测到 ${{parts.length}} 个条目，请选择其中一个。`;
        renderPartOptions(payload.title || payload.video_id || '视频合集', parts);
        return;
      }}
      await createTaskFromSource(parts[0]?.url || source);
    }});

    document.getElementById('retry-task-button').addEventListener('click', async () => {{
      if (!selectedTaskId) {{
        return;
      }}
      const response = await fetch(`/api/tasks/${{selectedTaskId}}/retry`, {{ method: 'POST' }});
      if (response.ok) {{
        await fetchTaskDetail(selectedTaskId);
        await fetchTasks();
      }}
    }});

    fetchTasks();
    fetchDouyinListener();
    setInterval(async () => {{
      await fetchTasks();
      await fetchDouyinListener();
    }}, 4000);
  </script>
</body>
</html>"""


def _render_Douyin_event_html(events: list[dict[str, object]]) -> str:
    """Render recent 抖音 events for the first page load."""
    if not events:
        return '<div class="empty">暂无 抖音 @ 事件。</div>'

    items = []
    for event in events[:10]:
        sender = escape(str(event.get("sender_name") or event.get("sender_mid") or "未知用户"))
        content = escape(str(event.get("content") or ""))
        status = escape(str(event.get("status") or ""))
        delivery_status = escape(str(event.get("delivery_status") or ""))
        sender_mid = escape(str(event.get("sender_mid") or ""))
        recipient_email = escape(str(event.get("recipient_email") or "未绑定邮箱"))
        task_id = escape(str(event.get("task_id") or ""))
        data_task = f' data-task-id="{task_id}"' if task_id else ""
        items.append(
            "\n".join(
                [
                    f'<button class="event-item"{data_task} type="button">',
                    f'  <div class="task-title">{sender}</div>',
                    f'  <div class="task-meta">{status} / {delivery_status}</div>',
                    f'  <div class="task-meta">UID {sender_mid} · {recipient_email}</div>',
                    f'  <div class="task-meta">{content}</div>',
                    "</button>",
                ]
            )
        )
    return "\n".join(items)


def _render_Douyin_user_html(users: list[dict[str, object]]) -> str:
    """Render the local UID to email bindings for the first page load."""
    if not users:
        return '<div class="empty">暂无用户邮箱绑定。</div>'

    items = []
    for user in users[:20]:
        uid = escape(str(user.get("uid") or ""))
        username = escape(str(user.get("username") or "未命名用户"))
        email = escape(str(user.get("email") or ""))
        items.append(
            "\n".join(
                [
                    '<div class="user-item">',
                    "  <div>",
                    f'    <div class="task-title">{username}</div>',
                    f'    <div class="task-meta">UID {uid} · {email}</div>',
                    "  </div>",
                    f'  <button class="delete-user" type="button" data-uid="{uid}">删除</button>',
                    "</div>",
                ]
            )
        )
    return "\n".join(items)


def _empty_Douyin_user_stats() -> dict[str, object]:
    """Return the default V2 stats shape for users without @ events."""
    return {
        "event_count": 0,
        "success_count": 0,
        "failed_count": 0,
        "last_event_at": "",
        "last_error": "",
    }


def _build_Douyin_user_payload(user: dict[str, object], stats_by_uid: dict[str, dict[str, object]]) -> dict[str, object]:
    """Merge one UID binding with its local event statistics."""
    payload = dict(user)
    payload["stats"] = stats_by_uid.get(str(user.get("uid") or ""), _empty_Douyin_user_stats())
    return payload


def _build_Douyin_user_payloads(repository: TaskRepository) -> list[dict[str, object]]:
    """Build the `/api/Douyin-users` response without changing the old user fields."""
    stats_by_uid = repository.get_douyin_user_stats()
    return [
        _build_Douyin_user_payload(user.to_dict(), stats_by_uid)
        for user in repository.list_douyin_user_emails()
    ]


def _select_default_Douyin_uid(users: list[dict[str, object]]) -> str:
    """Prefer the user with recent activity, then fall back to the first binding."""
    if not users:
        return ""
    users_with_events = [
        user
        for user in users
        if str((user.get("stats") or {}).get("last_event_at") or "")
    ]
    if users_with_events:
        selected = max(
            users_with_events,
            key=lambda user: str((user.get("stats") or {}).get("last_event_at") or ""),
        )
        return str(selected.get("uid") or "")
    return str(users[0].get("uid") or "")


def _render_v2_user_list_html(users: list[dict[str, object]], selected_uid: str) -> str:
    """Render the first V2 user list before browser polling takes over."""
    if not users:
        return '<div class="empty">暂无用户邮箱绑定。</div>'

    items = []
    for user in users:
        stats = user.get("stats") or {}
        uid = escape(str(user.get("uid") or ""))
        username = escape(str(user.get("username") or "未命名用户"))
        email = escape(str(user.get("email") or ""))
        event_count = escape(str(stats.get("event_count") or 0))
        failed_count = escape(str(stats.get("failed_count") or 0))
        active = " active" if uid == selected_uid else ""
        items.append(
            "\n".join(
                [
                    f'<button class="user-row{active}" type="button" data-uid="{uid}">',
                    "  <span>",
                    f'    <span class="row-title">{username}</span>',
                    f'    <span class="row-meta">UID {uid} · {email}</span>',
                    "  </span>",
                    f'  <span class="row-badge">{event_count} 次 / 失败 {failed_count}</span>',
                    "</button>",
                ]
            )
        )
    return "\n".join(items)


def _render_v2_users_html(
    listener_state: dict[str, object],
    users: list[dict[str, object]],
    selected_uid: str,
) -> str:
    """Render the V2 用户分析 page with plain HTML, CSS, and JS."""
    listener_status = escape(str(listener_state.get("login_status", "UNKNOWN")))
    listener_running = "运行中" if listener_state.get("running") else "未运行"
    listener_error = escape(str(listener_state.get("last_error") or "无"))
    listener_next_poll = escape(str(listener_state.get("next_poll_at") or "暂无"))
    listener_interval = escape(str(listener_state.get("last_interval_seconds") or "暂无"))
    listener_paused = escape(str(listener_state.get("paused_reason") or "无"))
    user_list_html = _render_v2_user_list_html(users, selected_uid)
    selected_uid_json = json.dumps(selected_uid, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>V2 用户状态 - 抖音 AI 助手</title>
  <style>
    :root {{
      --bg: #f4f6f8;
      --panel: #ffffff;
      --line: #d9e1ea;
      --text: #18222d;
      --muted: #5c6875;
      --accent: #0077d9;
      --accent-soft: #eaf4ff;
      --success: #168251;
      --danger: #c7344f;
      --warn: #a66a00;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      background: #f4f6f8;
      color: var(--text);
    }}
    .topbar {{
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }}
    .topbar-inner {{
      max-width: 1440px;
      margin: 0 auto;
      padding: 16px 24px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
    }}
    .brand {{
      display: flex;
      flex-direction: column;
      gap: 4px;
    }}
    h1 {{
      margin: 0;
      font-size: 22px;
      line-height: 1.3;
    }}
    .meta {{
      color: var(--muted);
      font-size: 13px;
      line-height: 1.5;
    }}
    a {{
      color: var(--accent);
      text-decoration: none;
    }}
    .shell {{
      max-width: 1440px;
      margin: 0 auto;
      padding: 20px 24px 28px;
      display: grid;
      grid-template-columns: 390px 1fr;
      gap: 18px;
      min-height: calc(100vh - 72px);
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    .left,
    .right {{
      padding: 18px;
      display: flex;
      flex-direction: column;
      gap: 16px;
    }}
    .section-title {{
      margin: 0;
      font-size: 15px;
      font-weight: 700;
    }}
    .listener {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      background: #fbfcfe;
      display: grid;
      gap: 6px;
    }}
    form {{
      display: grid;
      gap: 10px;
    }}
    input {{
      width: 100%;
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      font-size: 14px;
      background: #fff;
    }}
    button {{
      font-family: inherit;
    }}
    .primary {{
      border: 0;
      border-radius: 8px;
      padding: 10px 12px;
      background: var(--accent);
      color: #fff;
      font-size: 14px;
      cursor: pointer;
    }}
    .danger-button {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 9px 12px;
      background: #fff;
      color: var(--danger);
      cursor: pointer;
    }}
    .user-list,
    .event-list {{
      display: flex;
      flex-direction: column;
      gap: 8px;
      overflow: auto;
    }}
    .user-list {{
      max-height: 48vh;
    }}
    .event-list {{
      max-height: 52vh;
    }}
    .user-row {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 11px;
      background: #fff;
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 10px;
      text-align: left;
      cursor: pointer;
      align-items: center;
    }}
    .user-row.active {{
      border-color: var(--accent);
      background: var(--accent-soft);
    }}
    .row-title {{
      display: block;
      font-size: 14px;
      font-weight: 700;
      line-height: 1.4;
    }}
    .row-meta {{
      display: block;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.6;
      word-break: break-all;
    }}
    .row-badge,
    .tag {{
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 4px 8px;
      background: #fff;
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
    }}
    .tag.ok {{
      color: var(--success);
      border-color: #bde5d2;
      background: #f0fbf5;
    }}
    .tag.fail {{
      color: var(--danger);
      border-color: #f0bfcb;
      background: #fff4f6;
    }}
    .detail-head {{
      display: grid;
      gap: 12px;
    }}
    .detail-actions {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
    }}
    .stats-grid {{
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 10px;
    }}
    .stat {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
      background: #fbfcfe;
      min-width: 0;
    }}
    .stat-value {{
      font-size: 18px;
      font-weight: 700;
      line-height: 1.4;
      word-break: break-word;
    }}
    .event-card {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      background: #fff;
      display: grid;
      gap: 8px;
    }}
    .event-top {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      flex-wrap: wrap;
    }}
    .content {{
      white-space: pre-wrap;
      word-break: break-word;
      line-height: 1.6;
      font-size: 13px;
    }}
    .task-strip {{
      border-top: 1px solid var(--line);
      padding-top: 8px;
      display: grid;
      gap: 4px;
    }}
    .empty {{
      border: 1px dashed var(--line);
      border-radius: 8px;
      padding: 14px;
      color: var(--muted);
      font-size: 14px;
      background: #fbfcfe;
    }}
    .danger {{
      color: var(--danger);
    }}
    @media (max-width: 1020px) {{
      .shell {{
        grid-template-columns: 1fr;
      }}
      .stats-grid {{
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }}
    }}
  </style>
</head>
<body>
  <header class="topbar">
    <div class="topbar-inner">
      <div class="brand">
        <h1>V2 用户状态</h1>
        <div class="meta">用户邮箱簿、@ 请求记录、任务和邮件状态</div>
      </div>
      <a href="/">返回首页</a>
    </div>
  </header>
  <main class="shell">
    <section class="panel left">
      <div class="listener">
        <div class="section-title">抖音监听</div>
        <div class="meta">状态：<span id="v2-login-status">{listener_status}</span> / <span id="v2-running-status">{listener_running}</span></div>
        <div class="meta">账号：<span id="v2-account">暂无</span></div>
        <div class="meta">最近轮询：<span id="v2-last-poll">暂无</span></div>
        <div class="meta">下次轮询：<span id="v2-next-poll">{listener_next_poll}</span></div>
        <div class="meta">本次间隔：<span id="v2-last-interval">{listener_interval}</span> 秒</div>
        <div class="meta">暂停原因：<span id="v2-paused-reason">{listener_paused}</span></div>
        <div id="v2-error" class="danger">{listener_error}</div>
      </div>
      <div>
        <div class="section-title">新增/更新绑定</div>
        <form id="v2-user-form" style="margin-top:10px;">
          <input id="v2-user-uid" name="uid" placeholder="抖音用户 UID，例如 123456" />
          <input id="v2-user-name" name="username" placeholder="用户名，例如 测试用户" />
          <input id="v2-user-email" name="email" placeholder="邮箱，例如 user@example.com" />
          <button class="primary" type="submit">保存绑定</button>
          <div id="v2-form-message" class="meta"></div>
        </form>
      </div>
      <div>
        <div class="section-title">用户邮箱簿</div>
        <div id="v2-user-list" class="user-list" style="margin-top:10px;">{user_list_html}</div>
      </div>
    </section>
    <section class="panel right">
      <div id="v2-user-detail" class="detail-head">
        <div class="empty">正在加载用户状态。</div>
      </div>
      <div>
        <div class="section-title">最近 @ 事件</div>
        <div id="v2-event-list" class="event-list" style="margin-top:10px;">
          <div class="empty">请选择一个用户。</div>
        </div>
      </div>
    </section>
  </main>
  <script>
    let selectedUid = {selected_uid_json};
    let currentUsers = [];

    async function fetchListener() {{
      const response = await fetch('/api/Douyin-listener');
      if (!response.ok) {{
        return;
      }}
      const payload = await response.json();
      renderListener(payload.state || {{}});
    }}

    async function fetchUsers(preferredUid = null) {{
      const response = await fetch('/api/Douyin-users');
      if (!response.ok) {{
        return;
      }}
      const payload = await response.json();
      currentUsers = payload.users || [];
      if (preferredUid) {{
        selectedUid = preferredUid;
      }}
      if (!selectedUid || !currentUsers.some((user) => user.uid === selectedUid)) {{
        selectedUid = pickDefaultUid(currentUsers);
      }}
      renderUsers();
      if (selectedUid) {{
        await fetchUserEvents(selectedUid);
      }} else {{
        renderEmptyDetail();
      }}
    }}

    async function fetchUserEvents(uid) {{
      const response = await fetch(`/api/Douyin-users/${{encodeURIComponent(uid)}}/events`);
      if (!response.ok) {{
        return;
      }}
      const payload = await response.json();
      const user = currentUsers.find((item) => item.uid === uid);
      renderUserDetail(user, payload.events || []);
      renderEvents(payload.events || []);
    }}

    function pickDefaultUid(users) {{
      if (!users.length) {{
        return '';
      }}
      const withEvents = users
        .filter((user) => user.stats && user.stats.last_event_at)
        .sort((left, right) => String(right.stats.last_event_at).localeCompare(String(left.stats.last_event_at)));
      return (withEvents[0] || users[0]).uid || '';
    }}

    function renderListener(state) {{
      document.getElementById('v2-login-status').textContent = state.login_status || 'UNKNOWN';
      document.getElementById('v2-running-status').textContent = state.running ? '运行中' : '未运行';
      document.getElementById('v2-account').textContent = state.account_name
        ? `${{state.account_name}} (${{state.account_mid || 'unknown'}})`
        : '暂无';
      document.getElementById('v2-last-poll').textContent = state.last_poll_at || '暂无';
      document.getElementById('v2-next-poll').textContent = state.next_poll_at || '暂无';
      document.getElementById('v2-last-interval').textContent = state.last_interval_seconds || '暂无';
      document.getElementById('v2-paused-reason').textContent = state.paused_reason || '无';
      document.getElementById('v2-error').textContent = state.last_error || '无';
    }}

    function renderUsers() {{
      const container = document.getElementById('v2-user-list');
      if (!currentUsers.length) {{
        container.innerHTML = '<div class="empty">暂无用户邮箱绑定。</div>';
        return;
      }}
      container.innerHTML = currentUsers.map((user) => {{
        const stats = user.stats || {{}};
        const active = user.uid === selectedUid ? ' active' : '';
        return `
          <button class="user-row${{active}}" type="button" data-uid="${{escapeHtml(user.uid)}}">
            <span>
              <span class="row-title">${{escapeHtml(user.username || '未命名用户')}}</span>
              <span class="row-meta">UID ${{escapeHtml(user.uid)}} · ${{escapeHtml(user.email)}}</span>
            </span>
            <span class="row-badge">${{Number(stats.event_count || 0)}} 次 / 失败 ${{Number(stats.failed_count || 0)}}</span>
          </button>
        `;
      }}).join('');
      container.querySelectorAll('[data-uid]').forEach((button) => {{
        button.addEventListener('click', async () => {{
          selectedUid = button.dataset.uid || '';
          renderUsers();
          await fetchUserEvents(selectedUid);
        }});
      }});
    }}

    function renderUserDetail(user, events) {{
      const container = document.getElementById('v2-user-detail');
      if (!user) {{
        renderEmptyDetail();
        return;
      }}
      const stats = user.stats || {{}};
      const lastError = stats.last_error || '无';
      container.innerHTML = `
        <div class="detail-actions">
          <div>
            <h1>${{escapeHtml(user.username || '未命名用户')}}</h1>
            <div class="meta">UID ${{escapeHtml(user.uid)}} · ${{escapeHtml(user.email)}}</div>
          </div>
          <button class="danger-button" type="button" id="delete-selected-user">删除绑定</button>
        </div>
        <div class="stats-grid">
          <div class="stat"><div class="meta">绑定邮箱</div><div class="stat-value">${{user.email ? '已绑定' : '未绑定'}}</div></div>
          <div class="stat"><div class="meta">事件数</div><div class="stat-value">${{Number(stats.event_count || 0)}}</div></div>
          <div class="stat"><div class="meta">成功数</div><div class="stat-value">${{Number(stats.success_count || 0)}}</div></div>
          <div class="stat"><div class="meta">失败数</div><div class="stat-value">${{Number(stats.failed_count || 0)}}</div></div>
          <div class="stat"><div class="meta">最近请求</div><div class="stat-value">${{escapeHtml(stats.last_event_at || '暂无')}}</div></div>
        </div>
        <div class="listener">
          <div class="meta">最近错误</div>
          <div class="${{lastError === '无' ? '' : 'danger'}}">${{escapeHtml(lastError)}}</div>
        </div>
      `;
      document.getElementById('delete-selected-user').addEventListener('click', async () => {{
        await fetch(`/api/Douyin-users/${{encodeURIComponent(user.uid)}}`, {{ method: 'DELETE' }});
        selectedUid = '';
        await fetchUsers();
      }});
    }}

    function renderEvents(events) {{
      const container = document.getElementById('v2-event-list');
      if (!events.length) {{
        container.innerHTML = '<div class="empty">该用户暂无 @ 请求记录。</div>';
        return;
      }}
      container.innerHTML = events.map((event) => {{
        const task = event.task || null;
        const statusClass = event.delivery_status === 'SENT'
          ? 'ok'
          : (event.delivery_status === 'FAILED' || event.status === 'FAILED' || event.status === 'TASK_FAILED' ? 'fail' : '');
        const taskHtml = task ? `
          <div class="task-strip">
            <div class="meta">关联任务 #${{task.id}} · ${{escapeHtml(task.status || '')}} / ${{escapeHtml(task.mail_status || '')}}</div>
            <div class="content">${{escapeHtml(task.video_title || task.video_id || '未命名任务')}}</div>
            ${{task.error_message ? `<div class="danger">${{escapeHtml(task.error_message)}}</div>` : ''}}
          </div>
        ` : '<div class="task-strip"><div class="meta">暂无关联任务</div></div>';
        return `
          <div class="event-card">
            <div class="event-top">
              <div class="row-title">${{escapeHtml(event.video_id || '未解析 BV')}}</div>
              <span class="tag ${{statusClass}}">${{escapeHtml(event.status || '')}} / ${{escapeHtml(event.delivery_status || '')}}</span>
            </div>
            <div class="meta">${{escapeHtml(event.created_at || '')}} · 收件：${{escapeHtml(event.recipient_email || '未绑定邮箱')}}</div>
            <div class="content">${{escapeHtml(event.content || '')}}</div>
            ${{event.error_message ? `<div class="danger">${{escapeHtml(event.error_message)}}</div>` : ''}}
            ${{taskHtml}}
          </div>
        `;
      }}).join('');
    }}

    function renderEmptyDetail() {{
      document.getElementById('v2-user-detail').innerHTML = '<div class="empty">暂无用户。请先添加 UID 与邮箱绑定。</div>';
      document.getElementById('v2-event-list').innerHTML = '<div class="empty">暂无可展示的 @ 请求记录。</div>';
    }}

    function escapeHtml(value) {{
      return String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
    }}

    document.getElementById('v2-user-form').addEventListener('submit', async (event) => {{
      event.preventDefault();
      const message = document.getElementById('v2-form-message');
      const uid = document.getElementById('v2-user-uid').value.trim();
      const username = document.getElementById('v2-user-name').value.trim();
      const email = document.getElementById('v2-user-email').value.trim();
      if (!uid || !email) {{
        message.textContent = '请输入 UID 和邮箱。';
        return;
      }}
      const response = await fetch('/api/Douyin-users', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ uid, username, email }})
      }});
      const payload = await response.json();
      if (!response.ok) {{
        message.textContent = payload.detail || '保存失败。';
        return;
      }}
      document.getElementById('v2-user-uid').value = '';
      document.getElementById('v2-user-name').value = '';
      document.getElementById('v2-user-email').value = '';
      message.textContent = '绑定已保存。';
      await fetchUsers(payload.user.uid);
    }});

    fetchListener();
    fetchUsers(selectedUid);
    setInterval(async () => {{
      await fetchListener();
      await fetchUsers(selectedUid);
    }}, 4000);
  </script>
</body>
</html>"""


app = build_app()

