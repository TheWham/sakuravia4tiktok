"""Douyin mention listener based on the web notification API."""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib import parse, request
from urllib.error import HTTPError, URLError
from pathlib import Path

from ..config import AppConfig
from ..models import (
    DouyinDeliveryStatus,
    DouyinEventRecord,
    DouyinEventStatus,
    MailStatus,
    TaskRecord,
    TaskStatus,
    utc_now_text,
)
from ..storage import TaskRepository
from ..utils import extract_douyin_video_source
from .mail import MailService
from .task_service import TaskService


class DouyinApiError(RuntimeError):
    """Raised when a Douyin web API call cannot be used safely."""


@dataclass(slots=True)
class DouyinMentionItem:
    """One parsed @ notification item from Douyin."""

    notification_id: str
    comment_id: str
    sender_mid: str
    sender_name: str
    content: str
    mentions_self: bool
    timestamp_seconds: int = 0


@dataclass(slots=True)
class DouyinListenerState:
    """Runtime state exposed to the local page."""

    enabled: bool
    running: bool
    login_status: str
    account_mid: str
    account_name: str
    last_poll_at: str
    next_poll_at: str
    last_interval_seconds: int
    poll_count: int
    consecutive_failures: int
    startup_cutoff_seconds: int
    skipped_old_count: int
    paused_reason: str
    last_error: str

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-friendly snapshot."""
        return asdict(self)


class DouyinHttpClient:
    """Small Cookie-authenticated client for Douyin web APIs."""

    _USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"

    def __init__(self, config: AppConfig) -> None:
        self._cookie = config.douyin_cookie
        self._timeout = config.douyin_request_timeout_seconds
        # 抖音接口面向国内网络，直接访问通常更稳定。这里显式关闭 urllib
        # 对 HTTP_PROXY/HTTPS_PROXY 的自动读取，避免本机代理端口未启动时轮询失败。
        self._opener = request.build_opener(request.ProxyHandler({}))

    def get_json(self, url: str, params: dict[str, object] | None = None) -> dict[str, object]:
        """Send one GET request and validate the standard Douyin JSON envelope."""
        query = parse.urlencode(params or {}, doseq=True)
        target_url = f"{url}?{query}" if query else url
        req = request.Request(target_url, method="GET")
        req.add_header("User-Agent", self._USER_AGENT)
        req.add_header("Accept", "application/json, text/plain, */*")
        req.add_header("Referer", "https://www.douyin.com/")
        if self._cookie:
            req.add_header("Cookie", self._cookie)

        try:
            with self._opener.open(req, timeout=self._timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise DouyinApiError(f"抖音接口请求失败：{exc}") from exc

        code = payload.get("code", 0)
        if code not in (0, "0"):
            message = payload.get("message") or payload.get("msg") or "未知错误"
            raise DouyinApiError(f"抖音接口返回错误：{code} {message}")
        return payload


class DouyinAuthService:
    """Check whether the configured Cookie still belongs to a logged-in Douyin account."""

    _NAV_URL = "https://api.douyin.com/x/web-interface/nav"

    def __init__(self, http_client: DouyinHttpClient) -> None:
        self._http_client = http_client

    def check_login(self) -> dict[str, str]:
        """Return basic account information or raise a clear auth error."""
        payload = self._http_client.get_json(self._NAV_URL)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        if not data.get("isLogin"):
            raise DouyinApiError("抖音 Cookie 未登录或已失效。")
        return {
            "mid": str(data.get("mid", "")),
            "uname": str(data.get("uname", "")),
        }


class DouyinMentionApi:
    """Fetch @ mention notifications from Douyin."""

    _AT_URL = "https://api.douyin.com/x/msgfeed/at"

    def __init__(self, http_client: DouyinHttpClient, self_mid: str) -> None:
        self._http_client = http_client
        self._self_mid = self_mid

    def fetch_mentions(self) -> list[DouyinMentionItem]:
        """Load recent @ notifications and normalize their nested JSON fields."""
        payload = self._http_client.get_json(self._AT_URL, {"platform": "web"})
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        raw_items = data.get("items") if isinstance(data.get("items"), list) else []
        return [self._parse_item(item) for item in raw_items if isinstance(item, dict)]

    def _parse_item(self, item: dict[str, Any]) -> DouyinMentionItem:
        """Extract stable fields from Douyin's loosely documented notification item."""
        nested_item = item.get("item") if isinstance(item.get("item"), dict) else {}
        user = item.get("user") if isinstance(item.get("user"), dict) else {}
        notification_id = str(item.get("id") or nested_item.get("id") or nested_item.get("notify_id") or "")
        comment_id = str(nested_item.get("target_id") or nested_item.get("source_id") or nested_item.get("rpid") or "")
        sender_mid = str(user.get("mid") or nested_item.get("sender_uid") or nested_item.get("uid") or "")
        sender_name = str(user.get("nickname") or user.get("uname") or nested_item.get("sender_uname") or "")
        content = str(
            nested_item.get("source_content")
            or nested_item.get("title")
            or nested_item.get("content")
            or item.get("title")
            or ""
        )
        mentions_self = self._mentions_self(nested_item.get("at_details"))
        if not notification_id:
            notification_id = f"{comment_id}:{sender_mid}:{hash(content)}"
        timestamp_seconds = self._parse_timestamp_seconds(item, nested_item)
        return DouyinMentionItem(
            notification_id=notification_id,
            comment_id=comment_id,
            sender_mid=sender_mid,
            sender_name=sender_name,
            content=content,
            mentions_self=mentions_self,
            timestamp_seconds=timestamp_seconds,
        )

    def _parse_timestamp_seconds(self, item: dict[str, Any], nested_item: dict[str, Any]) -> int:
        """Read the notification timestamp from the loose 抖音 @ feed shape."""
        for source in (item, nested_item):
            for key in ("at_time", "ctime", "timestamp", "time", "notify_time", "created_at"):
                value = source.get(key)
                timestamp = self._normalize_timestamp(value)
                if timestamp > 0:
                    return timestamp
        return 0

    def _normalize_timestamp(self, value: object) -> int:
        """Convert common second/millisecond timestamp values to Unix seconds."""
        if value is None:
            return 0
        try:
            timestamp = int(float(str(value).strip()))
        except (TypeError, ValueError):
            return 0
        if timestamp > 10_000_000_000:
            timestamp //= 1000
        return timestamp

    def _mentions_self(self, at_details: object) -> bool:
        """Check at_details when Douyin includes it; otherwise trust the @ feed."""
        if not isinstance(at_details, list) or not at_details:
            return True
        if not self._self_mid:
            return False
        for item in at_details:
            if isinstance(item, dict) and str(item.get("mid") or item.get("uid") or "") == self._self_mid:
                return True
        return False


class DouyinEventService:
    """Turn Douyin mention events into local summary tasks."""

    def __init__(self, repository: TaskRepository, task_service: TaskService, mail_service: MailService) -> None:
        self._repository = repository
        self._task_service = task_service
        self._mail_service = mail_service

    def process_mentions(self, mentions: list[DouyinMentionItem]) -> None:
        """Persist every mention once and create tasks for valid commands."""
        for mention in mentions:
            event = self._repository.create_douyin_event_if_absent(
                notification_id=mention.notification_id,
                comment_id=mention.comment_id,
                sender_mid=mention.sender_mid,
                sender_name=mention.sender_name,
                content=mention.content,
            )
            if event.status is not DouyinEventStatus.NEW:
                continue
            self._process_one_event(event, mention)
        self.sync_task_statuses()

    def sync_task_statuses(self) -> None:
        """Propagate linked task terminal states back to 抖音 events."""
        for event in self._repository.list_open_douyin_events():
            if event.task_id is None:
                continue
            try:
                task = self._task_service.get_task(event.task_id)
            except KeyError:
                self._repository.update_douyin_event_fields(
                    event.id,
                    status=DouyinEventStatus.FAILED.value,
                    error_message="关联任务不存在。",
                )
                continue
            if task.status is TaskStatus.SUCCESS:
                self._deliver_task_result(event, task)
            elif task.status is TaskStatus.FAILED:
                self._repository.update_douyin_event_fields(
                    event.id,
                    status=DouyinEventStatus.TASK_FAILED.value,
                    delivery_status=DouyinDeliveryStatus.FAILED.value,
                    error_message=task.error_message,
                )

    def _process_one_event(self, event: DouyinEventRecord, mention: DouyinMentionItem) -> None:
        """Validate one mention and create or reuse the corresponding summary task."""
        if not mention.mentions_self:
            self._repository.update_douyin_event_fields(
                event.id,
                status=DouyinEventStatus.IGNORED.value,
                error_message="通知未确认 @ 到当前账号。",
            )
            return

        extracted = extract_douyin_video_source(mention.content)
        if extracted is None:
            self._repository.update_douyin_event_fields(
                event.id,
                status=DouyinEventStatus.IGNORED.value,
                error_message="评论中未包含 BV 号或 抖音视频链接。",
            )
            return

        source_url, video_id = extracted
        recipient = self._repository.find_douyin_user_email_by_uid(mention.sender_mid)
        if recipient is None:
            self._repository.update_douyin_event_fields(
                event.id,
                source_url=source_url,
                video_id=video_id,
                status=DouyinEventStatus.FAILED.value,
                delivery_status=DouyinDeliveryStatus.FAILED.value,
                error_message="该 抖音用户未绑定邮箱。",
            )
            return

        try:
            task = self._task_service.submit_task(source_url, reuse_success=True, send_mail=False)
        except Exception as exc:  # noqa: BLE001 - event table needs the visible failure reason.
            self._repository.update_douyin_event_fields(
                event.id,
                source_url=source_url,
                video_id=video_id,
                status=DouyinEventStatus.FAILED.value,
                delivery_status=DouyinDeliveryStatus.FAILED.value,
                error_message=str(exc),
            )
            return

        next_status = DouyinEventStatus.TASK_SUCCESS if task.status is TaskStatus.SUCCESS else DouyinEventStatus.TASK_CREATED
        updated_event = self._repository.update_douyin_event_fields(
            event.id,
            source_url=source_url,
            video_id=video_id,
            task_id=task.id,
            recipient_email=recipient.email,
            status=next_status.value,
            delivery_status=DouyinDeliveryStatus.PENDING.value,
            error_message="",
        )
        if task.status is TaskStatus.SUCCESS:
            self._deliver_task_result(updated_event, task)

    def _deliver_task_result(self, event: DouyinEventRecord, task: TaskRecord) -> None:
        """Send one completed task result to the email bound to this 抖音 event."""
        if not event.recipient_email:
            self._repository.update_douyin_event_fields(
                event.id,
                status=DouyinEventStatus.FAILED.value,
                delivery_status=DouyinDeliveryStatus.FAILED.value,
                error_message="该 抖音用户未绑定邮箱。",
            )
            return
        if not task.markdown_content or not task.markdown_file_path:
            self._repository.update_douyin_event_fields(
                event.id,
                status=DouyinEventStatus.TASK_FAILED.value,
                delivery_status=DouyinDeliveryStatus.FAILED.value,
                error_message="关联任务缺少 Markdown 内容或文件路径。",
            )
            return

        try:
            self._mail_service.send_markdown(
                task.video_title or task.video_id,
                task.markdown_content,
                Path(task.markdown_file_path),
                to_email=event.recipient_email,
            )
        except Exception as exc:  # noqa: BLE001 - event delivery failure must be visible on the page.
            self._repository.update_douyin_event_fields(
                event.id,
                status=DouyinEventStatus.FAILED.value,
                delivery_status=DouyinDeliveryStatus.FAILED.value,
                error_message=str(exc),
            )
            return

        self._repository.update_douyin_event_fields(
            event.id,
            status=DouyinEventStatus.TASK_SUCCESS.value,
            delivery_status=DouyinDeliveryStatus.SENT.value,
            delivered_at=utc_now_text(),
            error_message="",
        )
        self._repository.update_task_fields(task.id, mail_status=MailStatus.SENT.value)


class DouyinMentionPoller:
    """Background scheduler that periodically polls Douyin @ notifications."""

    _AUTH_CHECK_EVERY_POLLS = 10
    _FIRST_BACKOFF_RANGE = (300, 600)
    _SECOND_BACKOFF_RANGE = (600, 1800)
    _MAX_CONSECUTIVE_FAILURES = 5

    def __init__(
        self,
        config: AppConfig,
        auth_service: DouyinAuthService,
        mention_api: DouyinMentionApi,
        event_service: DouyinEventService,
    ) -> None:
        self._config = config
        self._auth_service = auth_service
        self._mention_api = mention_api
        self._event_service = event_service
        self._logger = logging.getLogger("mysakura.Douyin")
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._startup_cutoff_seconds = int(time.time())
        self._seen_notification_ids: set[str] = set()
        self._state = DouyinListenerState(
            enabled=config.douyin_enable_listener,
            running=False,
            login_status="DISABLED" if not config.douyin_enable_listener else "UNKNOWN",
            account_mid="",
            account_name="",
            last_poll_at="",
            next_poll_at="",
            last_interval_seconds=0,
            poll_count=0,
            consecutive_failures=0,
            startup_cutoff_seconds=self._startup_cutoff_seconds,
            skipped_old_count=0,
            paused_reason="",
            last_error="",
        )

    def start(self) -> None:
        """Start the polling thread when listener config is complete."""
        if not self._config.douyin_enable_listener:
            return
        if not self._config.douyin_cookie or not self._config.douyin_self_mid:
            self._state.login_status = "CONFIG_ERROR"
            self._state.last_error = "请配置 DOUYIN_COOKIE 和 DOUYIN_SELF_MID。"
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._startup_cutoff_seconds = int(time.time())
        self._seen_notification_ids.clear()
        self._state.startup_cutoff_seconds = self._startup_cutoff_seconds
        self._state.skipped_old_count = 0
        self._state.paused_reason = ""
        self._thread = threading.Thread(target=self._run_loop, name="Douyin-mention-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the background polling thread during application shutdown."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._state.running = False

    def snapshot(self) -> DouyinListenerState:
        """Return current listener state for the local page."""
        return self._state

    def poll_once(self) -> None:
        """Run one protected mention poll without checking login on every loop."""
        if self._state.paused_reason:
            return

        self._state.poll_count += 1
        self._state.last_poll_at = utc_now_text()
        try:
            if self._should_check_login():
                self._refresh_login_status()
            mentions = self._mention_api.fetch_mentions()
            new_mentions = self._filter_mentions_after_startup(mentions)
            self._event_service.process_mentions(new_mentions)
            self._state.consecutive_failures = 0
            self._state.login_status = "LOGGED_IN"
            self._state.last_error = ""
        except DouyinApiError as exc:
            self._handle_douyin_api_error(exc)
        except Exception as exc:  # noqa: BLE001 - listener state must show the original reason.
            self._record_retryable_failure(exc)

    def _should_check_login(self) -> bool:
        """Keep Cookie validation low-frequency so normal loops only hit the @ feed."""
        return self._state.poll_count == 1 or self._state.poll_count % self._AUTH_CHECK_EVERY_POLLS == 0

    def _refresh_login_status(self) -> None:
        """Refresh login metadata and fail visibly when the configured Cookie is unsafe."""
        account = self._auth_service.check_login()
        self._state.login_status = "LOGGED_IN"
        self._state.account_mid = account["mid"]
        self._state.account_name = account["uname"]

    def _filter_mentions_after_startup(self, mentions: list[DouyinMentionItem]) -> list[DouyinMentionItem]:
        """Process only @ notifications created after this listener process started."""
        new_mentions = [
            mention
            for mention in mentions
            if (
                mention.notification_id
                and mention.timestamp_seconds > self._startup_cutoff_seconds
                and mention.notification_id not in self._seen_notification_ids
            )
        ]
        self._seen_notification_ids.update(mention.notification_id for mention in new_mentions)
        self._state.skipped_old_count += len(mentions) - len(new_mentions)
        return new_mentions

    def _handle_douyin_api_error(self, exc: DouyinApiError) -> None:
        """Classify 抖音 errors so account-risk signals stop the poller immediately."""
        message = str(exc)
        if self._is_account_risk_error(message):
            self._pause_for_account_risk(message)
            return
        if self._is_auth_related_error(message):
            try:
                self._refresh_login_status()
            except Exception as auth_exc:  # noqa: BLE001 - any auth failure means the Cookie should rest.
                self._pause_for_account_risk(str(auth_exc))
                return
        self._record_retryable_failure(exc)

    def _record_retryable_failure(self, exc: Exception) -> None:
        """Back off retryable network/API failures without hiding the latest reason."""
        self._state.login_status = "ERROR"
        self._state.consecutive_failures += 1
        self._state.last_error = str(exc)
        self._logger.warning("Douyin listener poll failed: %s", exc)
        if self._state.consecutive_failures > self._MAX_CONSECUTIVE_FAILURES:
            self._pause(f"连续失败 {self._state.consecutive_failures} 次，已自动暂停，请人工检查网络、Cookie 或 抖音访问状态。")

    def _pause_for_account_risk(self, detail: str) -> None:
        """Stop polling when 抖音 returns a likely risk-control or auth signal."""
        self._pause(f"疑似风控或 Cookie 异常，已自动暂停：{detail}")

    def _pause(self, reason: str) -> None:
        """Move the listener into a manual-check state and stop future requests."""
        self._state.running = False
        self._state.login_status = "PAUSED"
        self._state.paused_reason = reason
        self._state.last_error = reason
        self._state.next_poll_at = ""
        self._stop_event.set()

    def _is_account_risk_error(self, message: str) -> bool:
        """Recognize response text that usually means continuing would increase risk."""
        lowered = message.lower()
        return any(
            marker in lowered
            for marker in (
                "412",
                "429",
                "precondition",
                "too many requests",
                "验证码",
                "风控",
                "访问受限",
                "账号异常",
                "安全验证",
            )
        )

    def _is_auth_related_error(self, message: str) -> bool:
        """Recognize Cookie/login errors and re-check auth before deciding to continue."""
        lowered = message.lower()
        return any(marker in lowered for marker in ("cookie", "未登录", "失效", "-101", "login", "登录"))

    def _schedule_next_poll(self) -> int:
        """Pick the next wait time and expose it to the UI before sleeping."""
        if self._state.consecutive_failures == 0:
            lower = self._config.douyin_poll_min_seconds
            upper = self._config.douyin_poll_max_seconds
        elif self._state.consecutive_failures <= 2:
            lower, upper = self._FIRST_BACKOFF_RANGE
        else:
            lower, upper = self._SECOND_BACKOFF_RANGE

        wait_seconds = random.randint(lower, upper)
        self._state.last_interval_seconds = wait_seconds
        self._state.next_poll_at = _seconds_from_now_text(wait_seconds)
        return wait_seconds

    def _run_loop(self) -> None:
        """Poll until the app shuts down, without blocking the summary worker."""
        self._state.running = True
        while not self._stop_event.is_set() and not self._state.paused_reason:
            self.poll_once()
            if self._state.paused_reason:
                break
            wait_seconds = self._schedule_next_poll()
            self._stop_event.wait(wait_seconds)
        self._state.running = False


def _seconds_from_now_text(seconds: int) -> str:
    """Return a readable local timestamp for the next scheduled poll."""
    return (datetime.now() + timedelta(seconds=seconds)).replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")

