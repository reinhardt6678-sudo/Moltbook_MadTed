"""Moltbook API 客户端。

封装 https://www.moltbook.com/api/v1 的调用，内置限流与重试。

⚠️ 端点细节以官方文档为准（https://www.moltbook.com/developers 和 /skill.md）。
本文件的端点路径整理自公开教程与第三方 SDK，首次接入时请用 --dry-run 核对。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import requests

log = logging.getLogger(__name__)

BASE_URL = os.environ.get("MOLTBOOK_BASE_URL", "https://www.moltbook.com/api/v1")

# 平台限流（保守取值，比官方公布的更紧一点，留安全边际）
RATE_LIMITS = {
    "requests_per_minute": 90,   # 官方 100，留 10% 余量
    "post_cooldown_sec": 1830,   # 官方约 30 分钟一帖
    "comment_cooldown_sec": 25,  # 官方约 20 秒一条评论
}


class MoltbookError(RuntimeError):
    """API 调用失败。"""

    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class RateLimitError(MoltbookError):
    """触发限流，应当退避。"""


@dataclass
class _Throttle:
    """进程内限流器：滑动窗口 + 每类动作的冷却时间。"""

    _lock: threading.Lock = field(default_factory=threading.Lock)
    _window: list[float] = field(default_factory=list)
    _last_action: dict[str, float] = field(default_factory=dict)

    def wait_for_slot(self) -> None:
        """确保不超过每分钟请求数上限。"""
        with self._lock:
            now = time.monotonic()
            self._window = [t for t in self._window if now - t < 60]
            if len(self._window) >= RATE_LIMITS["requests_per_minute"]:
                sleep_for = 60 - (now - self._window[0]) + 0.1
                log.info("接近每分钟限流，等待 %.1fs", sleep_for)
                time.sleep(max(sleep_for, 0))
                now = time.monotonic()
                self._window = [t for t in self._window if now - t < 60]
            self._window.append(now)

    def remaining_cooldown(self, action: str, cooldown: float) -> float:
        """距离下一次可执行该动作还差多少秒（0 表示现在就能做）。"""
        with self._lock:
            last = self._last_action.get(action)
            if last is None:
                return 0.0
            elapsed = time.monotonic() - last
            return max(cooldown - elapsed, 0.0)

    def mark(self, action: str) -> None:
        with self._lock:
            self._last_action[action] = time.monotonic()


class MoltbookClient:
    """Moltbook REST 客户端。

    用法：
        client = MoltbookClient.from_env()
        feed = client.get_feed(sort="hot", limit=50)
    """

    def __init__(self, api_key: str, *, base_url: str = BASE_URL, dry_run: bool = False):
        if not api_key and not dry_run:
            raise ValueError("缺少 API key。请设置 MOLTBOOK_API_KEY 环境变量。")
        self.base_url = base_url.rstrip("/")
        self.dry_run = dry_run
        self._throttle = _Throttle()
        # 记住每类操作实际走通的候选端点下标，避免每次都从头试
        self._resolved: dict[str, int] = {}
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "MadTed/1.0 (+https://github.com/reinhardt6678-sudo/Moltbook_MadTed)",
            }
        )

    @classmethod
    def from_env(cls, *, dry_run: bool = False) -> "MoltbookClient":
        return cls(os.environ.get("MOLTBOOK_API_KEY", ""), dry_run=dry_run)

    # ---------- 底层请求 ----------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        max_retries: int = 4,
    ) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"

        if self.dry_run and method != "GET":
            log.info("[dry-run] %s %s payload=%s", method, url, json)
            return {"dry_run": True, "id": "dry_run_id"}

        last_error: Exception | None = None
        for attempt in range(max_retries):
            self._throttle.wait_for_slot()
            try:
                resp = self._session.request(
                    method, url, params=params, json=json, timeout=30
                )
            except requests.RequestException as exc:
                last_error = exc
                backoff = 2 ** attempt
                log.warning("网络错误 %s，%ss 后重试", exc, backoff)
                time.sleep(backoff)
                continue

            if resp.status_code == 429:
                retry_after = float(resp.headers.get("retry-after", 2 ** attempt))
                log.warning("被限流(429)，等待 %.0fs", retry_after)
                time.sleep(retry_after)
                last_error = RateLimitError("429 Too Many Requests", 429, resp.text)
                continue

            if resp.status_code >= 500:
                backoff = 2 ** attempt
                log.warning("服务端错误 %s，%ss 后重试", resp.status_code, backoff)
                time.sleep(backoff)
                last_error = MoltbookError("server error", resp.status_code, resp.text)
                continue

            if not resp.ok:
                # 4xx 不重试——请求本身有问题。
                # 把响应体带进异常消息：端点/字段名对不上时，服务端返回的
                # 说明往往直接point出错在哪，比光看状态码省事得多。
                body = resp.text[:500].strip()
                detail = f"，响应：{body}" if body else ""
                raise MoltbookError(
                    f"{method} {path} 失败: {resp.status_code}{detail}",
                    resp.status_code,
                    resp.text[:500],
                )

            if not resp.content:
                return None
            return resp.json()

        raise MoltbookError(f"{method} {path} 重试 {max_retries} 次后仍失败: {last_error}")

    def _try_paths(
        self,
        method: str,
        attempts: list[tuple[str, dict[str, Any] | None]],
        *,
        cache_key: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """依次尝试多个候选端点，404 就换下一个。

        端点路径整理自第三方资料，各来源写法不一（评论有的写
        /posts/{id}/comments，有的写 /comments）。**404 意味着请求压根没被
        处理**，所以换一条路径重试是安全的——不会重复发出内容。

        第一条走通的会被记住，本进程后续直接用它，不再逐个试。
        dry-run 下不记忆，因为根本没真的碰过服务端。
        """
        order = list(range(len(attempts)))
        hit = self._resolved.get(cache_key)
        if hit is not None and hit < len(attempts):
            order.remove(hit)
            order.insert(0, hit)

        last_error: MoltbookError | None = None
        for index in order:
            path, payload = attempts[index]
            try:
                result = self._request(method, path, params=params, json=payload)
            except MoltbookError as exc:
                if exc.status == 404:
                    log.info("%s %s → 404，换下一个候选端点", method, path)
                    last_error = exc
                    continue
                raise
            if not self.dry_run and self._resolved.get(cache_key) != index:
                self._resolved[cache_key] = index
                log.info("确认可用端点：%s %s", method, path)
            return result

        raise last_error or MoltbookError(f"{method} 的候选端点全部不可用（{cache_key}）")

    # ---------- 读取 ----------

    def get_feed(self, *, sort: str = "hot", limit: int = 50, submolt: str | None = None) -> list[dict]:
        """拉取 feed。sort 取值一般为 hot / new / top。"""
        params: dict[str, Any] = {"sort": sort, "limit": limit}
        if submolt:
            params["submolt"] = submolt
        data = self._request("GET", "/feed", params=params)
        return _unwrap_list(data, "posts")

    def get_replies(self, post_id: str, *, limit: int = 100) -> list[dict]:
        """拉取某个帖子下的回复。"""
        data = self._try_paths(
            "GET",
            [
                (f"/posts/{post_id}/comments", None),
                (f"/posts/{post_id}/replies", None),
            ],
            cache_key="get_replies",
            params={"limit": limit},
        )
        return _unwrap_list(data, "comments", "replies")

    def get_notifications(self, *, unread_only: bool = True) -> list[dict]:
        """拉取通知——这是发现『对方回复了我』的主要途径。"""
        params = {"unread": "true"} if unread_only else {}
        data = self._request("GET", "/notifications", params=params)
        return _unwrap_list(data, "notifications")

    def mark_notification_read(self, notification_id: str) -> None:
        self._request("POST", f"/notifications/{notification_id}/read")

    def get_agent_status(self) -> dict:
        """心跳/状态检查，也用于确认 agent 是否已被认领。"""
        return self._request("GET", "/agents/status") or {}

    # ---------- 写入（带冷却） ----------

    def create_comment(self, post_id: str, content: str) -> dict:
        """在帖子下发表评论。受 comment_cooldown 限制。"""
        wait = self._throttle.remaining_cooldown("comment", RATE_LIMITS["comment_cooldown_sec"])
        if wait > 0:
            log.info("评论冷却中，等待 %.0fs", wait)
            time.sleep(wait)
        result = self._try_paths(
            "POST",
            [
                (f"/posts/{post_id}/comments", {"content": content}),
                (f"/posts/{post_id}/replies", {"content": content}),
                ("/comments", {"post_id": post_id, "content": content}),
            ],
            cache_key="create_comment",
        )
        self._throttle.mark("comment")
        return result or {}

    def create_post(self, *, submolt: str, title: str, content: str) -> dict:
        """发帖。受 post_cooldown 限制（约 30 分钟一帖）。"""
        wait = self._throttle.remaining_cooldown("post", RATE_LIMITS["post_cooldown_sec"])
        if wait > 0:
            raise MoltbookError(f"发帖冷却中，还需 {wait:.0f}s。本轮跳过发帖。")
        result = self._request(
            "POST", "/posts", json={"submolt": submolt, "title": title, "content": content}
        )
        self._throttle.mark("post")
        return result or {}

    def can_post_now(self) -> bool:
        return self._throttle.remaining_cooldown("post", RATE_LIMITS["post_cooldown_sec"]) == 0


def _unwrap_list(data: Any, *keys: str) -> list[dict]:
    """API 有时返回 {"posts": [...]}，有时直接返回 [...]，统一成 list。

    可以给多个候选键——不同端点对同一批数据的叫法不一致
    （评论有的叫 comments 有的叫 replies）。
    """
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for candidate in (*keys, "data", "items", "results"):
            value = data.get(candidate)
            if isinstance(value, list):
                return value
    log.warning("无法从响应中解析出列表：%s", str(data)[:200])
    return []
