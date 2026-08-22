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
        # 空跑也要 key：dry_run 只挡写操作（见 _request），读全都照样发到服务端。
        # 放行无 key 的空跑，等于让每个 GET 带着空 Authorization 出门，然后把
        # 401/重试耗尽的日志摆在你面前——"你没配 key"就这样伪装成了平台故障，
        # 而 --dry-run 的全部意义正是上线前把这类问题看出来。
        if not api_key:
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
        """拉取某个帖子下的回复，**连嵌套的子回复一起**摊平返回。

        端点只返回顶层评论，子回复裹在各自父评论的 `replies` 里。别人回我
        一定是挂在我那条评论下面（depth≥1），所以只读顶层那批
        等于把所有真正冲我来的回复全漏掉——见 flatten_comments。
        """
        data = self._try_paths(
            "GET",
            [
                (f"/posts/{post_id}/comments", None),
                (f"/posts/{post_id}/replies", None),
            ],
            cache_key="get_replies",
            params={"limit": limit},
        )
        return flatten_comments(_unwrap_list(data, "comments", "replies"))

    def get_home(self) -> dict:
        """首页看板。官方文档（HEARTBEAT.md）里发现『有人回复你』的正门：
        返回体里的 activity_on_your_posts 就是收件箱。
        """
        return self._request("GET", "/home") or {}

    def get_inbox_activity(self) -> list[dict]:
        """收件箱里的活动条目——发现『对方回复了我』的**辅助**途径，不是唯一途径。

        先走官方文档写明的 /home，再退回 /notifications 系列。之前这里只硬编码了
        一个 /notifications——而官方文档根本没有这个端点，它一 404，
        整个跟进阶段就直接 return 0，一条回复都看不见。

        另外也不再只拉未读：未读状态是主人在网页端点一下就会变的东西，
        拿它当发现回复的开关，等于让"主人看过没有"决定 agent 看不看得见回复。
        真正的判据是 get_replies() 里有没有新 id（见 heartbeat.py）。
        """
        try:
            home = self.get_home()
        except MoltbookError as exc:
            log.info("/home 不可用（%s），改试 /notifications 系列", exc)
        else:
            # 认"这个键在不在"，不认"列表空不空"——空收件箱是常态，
            # 拿空列表当失败会让每个周期都白跑一轮回退请求。
            for key in ("activity_on_your_posts", "activity", "notifications"):
                value = home.get(key)
                if isinstance(value, list):
                    return value
            log.info("/home 里没有 activity_on_your_posts 字段，改试 /notifications 系列")

        data = self._try_paths(
            "GET",
            [("/notifications", None), ("/agents/notifications", None)],
            cache_key="get_notifications",
        )
        return _unwrap_list(data, "notifications")

    def mark_post_read(self, post_id: str) -> None:
        """把某个帖子下的活动标记为已读（官方文档写的是按帖子标，不是按通知 id）。

        标记失败无所谓：读回复靠 get_replies()，这里只是别让主人的未读数一直涨。
        """
        try:
            self._try_paths(
                "POST",
                [
                    (f"/notifications/read-by-post/{post_id}", None),
                    (f"/notifications/read", {"post_id": post_id}),
                ],
                cache_key="mark_read",
            )
        except MoltbookError as exc:
            log.info("标记 %s 已读失败（不影响跟进）：%s", post_id, exc)

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
        # 解掉信封再返回：调用方要拿里面的评论 id 记住"这条是我说的"，
        # 拿不到的话就认不出自己，后面判不出谁在回我。
        return _unwrap_obj(result, "comment", "reply")

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


# ---------- 字段提取 ----------
#
# 同一个东西不同端点叫法不一样（评论 id 有 id / comment_id / _id 三种写法，
# 作者有时是字符串有时是对象）。以前这些提取散在调用处、每处只认一两个写法，
# 猜错就静默变成空字符串——空字符串会一路穿过去当成"没有回复"，
# 最后被记成冷场。集中在这里，认全所有见过的写法。


def _first_str(source: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = source.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value)
    return ""


def comment_id(comment: dict) -> str:
    return _first_str(comment, ("id", "comment_id", "commentId", "_id"))


def author_name(obj: dict) -> str:
    """取评论/帖子的作者名。作者可能是嵌套对象，也可能直接是字符串。"""
    for key in ("author", "agent", "user", "created_by"):
        value = obj.get(key)
        if isinstance(value, dict):
            name = _first_str(value, ("name", "username", "handle", "agent_name", "id"))
            if name:
                return name
        elif isinstance(value, str) and value.strip():
            return value
    return _first_str(obj, ("author_name", "username", "agent_name"))


def parent_comment_id(comment: dict) -> str:
    """这条评论回的是哪条评论。顶层评论没有父级，返回空字符串。"""
    for key in ("parent_id", "parentId", "parent_comment_id", "in_reply_to", "reply_to"):
        value = comment.get(key)
        if isinstance(value, dict):
            nested = _first_str(value, ("id", "comment_id", "_id"))
            if nested:
                return nested
        elif isinstance(value, (str, int)) and str(value).strip():
            return str(value)
    parent = comment.get("parent")
    if isinstance(parent, dict):
        return _first_str(parent, ("id", "comment_id", "_id"))
    return ""


def flatten_comments(items: Any) -> list[dict]:
    """把嵌套的评论树摊平成一条一条的评论。

    评论区是有层级的（线上见过 5 层深），子回复裹在父评论的 `replies` 里，
    端点本身只返回顶层那批。**别人回我一定是挂在我那条评论下面**，也就是
    depth≥1——只读顶层等于把所有冲我来的回复都留在视野之外。线上表现是
    对手在楼里回了三条、其中一条直接回我，agent 一条没读到，
    最后把这串记成"对方停止回应，收尾"。

    父子关系优先信服务端给的 parent_id，缺了就按树结构补：摊平之后
    这个字段是唯一还能分辨"这条回的是谁"的东西。
    """
    flat: list[dict] = []

    def walk(nodes: Any, parent: str) -> None:
        if not isinstance(nodes, list):
            return
        for node in nodes:
            if not isinstance(node, dict):
                continue
            item = {key: value for key, value in node.items() if key != "replies"}
            if parent and not parent_comment_id(item):
                item["parent_id"] = parent
            flat.append(item)
            walk(node.get("replies"), comment_id(item) or parent)

    walk(items, "")
    return flat


def agent_self_name(status: dict) -> str:
    """从 /agents/status 的返回体里取自己的名字。

    名字在嵌套的 agent 对象里（`{"success": true, "agent": {"name": …}}`），
    以前只读顶层的 name/username，拿到的永远是空字符串——认不出自己，
    就分不清评论区里哪条是自己说的、哪条是冲自己来的。
    """
    if not isinstance(status, dict):
        return ""
    direct = _first_str(status, ("name", "username", "handle", "agent_name"))
    if direct:
        return direct
    for key in ("agent", "me", "profile", "data"):
        nested = status.get(key)
        if isinstance(nested, dict):
            found = _first_str(nested, ("name", "username", "handle", "agent_name"))
            if found:
                return found
    return ""


def notification_post_id(notification: dict) -> str:
    """通知指向哪个帖子。取不到就返回空——调用方应当忽略，不要当成 '所有帖子'。"""
    direct = _first_str(
        notification,
        ("post_id", "postId", "target_id", "targetId", "subject_id", "thread_id"),
    )
    if direct:
        return direct
    for key in ("post", "target", "subject", "data", "payload"):
        nested = notification.get(key)
        if isinstance(nested, dict):
            found = _first_str(nested, ("post_id", "postId", "id", "_id"))
            if found:
                return found
    return ""


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


def _unwrap_obj(data: Any, *keys: str) -> dict:
    """写操作的返回体解信封。

    读的那条路一直有 _unwrap_list 解 `{"comments": [...]}`，写的这条没有：
    POST 完评论返回的是 `{"success": true, "comment": {...}}`，
    顶层没有 id，于是 `own_comment_ids` 永远存不进东西。
    """
    if not isinstance(data, dict):
        return {}
    if _first_str(data, ("id", "comment_id", "commentId", "_id")):
        return data
    for candidate in (*keys, "data", "result"):
        value = data.get(candidate)
        if isinstance(value, dict):
            return value
    return data
