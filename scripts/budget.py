"""跨进程的评论额度（滚动 24 小时窗口）。

为什么要单独落一个文件：`moltbook_client.py` 里的 `_Throttle` 用的是
`time.monotonic()`，**只在一个进程内有效**。cron 每小时拉起一个新进程，
上一轮的冷却记录跟着进程一起消失——那套冷却挡得住"一轮之内连发"，
挡不住"一天跑 24 轮、每轮都发满"。

平台的评论上限是按天算的（首日之后约 50 条/天）。撞上去的后果不只是接口
报个 429：连续顶着日限发是 spam 的典型特征，Moltbook 有 moderator bot
（Clawd Clawderberg）专门清这个，而人设 §10.1 里"被 moderator 警告"是
-50 分，比任何一场对线赢回来的都多。所以额度必须落盘、跨进程共享。

**用滚动 24 小时窗口，不用自然日**：平台按哪个时区跨天我们并不知道，
滚动窗口在任何时区解释下都不会比自然日更宽松，还省掉了时区换算这个
最容易写错的地方。代价是"零点清零"这件事不会发生——额度是慢慢回来的。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

BUDGET_PATH = Path(__file__).resolve().parent.parent / "memory" / "comment-budget.json"

# 平台文档写的是 50 条/天。留 20% 余量：我们数的是"发成功了几条"，
# 平台数的可能包含被它自己判失败/删掉的，两边不必然对得上。
DEFAULT_DAILY_COMMENTS = 40

WINDOW = timedelta(hours=24)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class CommentBudget:
    """滚动 24 小时内还能发几条评论。

    用法：
        budget = CommentBudget()
        if budget.remaining > 0:
            client.create_comment(...)
            budget.spend()          # 立刻落盘

    每次 `spend()` 都立即写文件，不攒到最后统一保存——cron 拉起的进程
    随时可能被杀，攒着的话崩一次就等于凭空多出一批额度。

    `dry_run=True` 时只在内存里记账、不落盘：空跑照样会因为额度耗尽而停手，
    看到的行为和真跑一致，但不会把没发出去的评论算进真实额度。
    """

    def __init__(
        self,
        path: Path | str = BUDGET_PATH,
        cap: int | None = None,
        *,
        dry_run: bool = False,
    ):
        self.path = Path(path)
        self.cap = cap if cap is not None else _cap_from_env()
        self.dry_run = dry_run
        self._spent_at: list[datetime] = self._load()

    # ---------- 落盘 ----------

    def _load(self) -> list[datetime]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            stamps = [datetime.fromisoformat(s) for s in raw.get("spent_at", [])]
        except (json.JSONDecodeError, ValueError, TypeError, OSError) as exc:
            # 额度文件坏了不该让整轮 heartbeat 崩掉，但也不能当成"额度全新"——
            # 那等于给了自己一次白嫖满额度的机会。按"已用满"处理，这轮不发，
            # 文件会在下次 spend 时被重写成正常格式。
            log.error("额度文件读不了（%s），本轮按已用满处理：%s", self.path, exc)
            return [_now()] * self.cap
        # 容忍手工编辑过的无时区时间戳
        return [s if s.tzinfo else s.replace(tzinfo=timezone.utc) for s in stamps]

    def _save(self) -> None:
        if self.dry_run:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "cap": self.cap,
            "window_hours": int(WINDOW.total_seconds() // 3600),
            "spent_at": [s.isoformat() for s in self._spent_at],
        }
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ---------- 额度 ----------

    def _prune(self) -> None:
        cutoff = _now() - WINDOW
        self._spent_at = [s for s in self._spent_at if s > cutoff]

    @property
    def used(self) -> int:
        self._prune()
        return len(self._spent_at)

    @property
    def remaining(self) -> int:
        return max(self.cap - self.used, 0)

    def spend(self, count: int = 1) -> None:
        """记一条已发出去的评论。只在**真的发成功**之后调用。"""
        self._prune()
        now = _now()
        self._spent_at.extend([now] * count)
        self._save()

    def next_slot_in(self) -> timedelta | None:
        """还要等多久才回来一个额度。

        返回 None 有两种情况：额度还没用完（不用等），或者上限本身就是 0
        （等多久都不会回来）。`self.remaining` 会顺带 prune，所以下面取 min
        拿到的一定是还在窗口内的最老一条。
        """
        if self.remaining > 0 or not self._spent_at:
            return None
        oldest = min(self._spent_at)
        return max(oldest + WINDOW - _now(), timedelta(0))

    def summary(self) -> str:
        if self.remaining > 0:
            return f"评论额度 {self.used}/{self.cap}（滚动 24h）"
        wait = self.next_slot_in()
        if wait is None:
            return f"评论额度上限是 {self.cap}，本轮不发评论"
        return f"评论额度已用满 {self.used}/{self.cap}，约 {wait.total_seconds() / 60:.0f} 分钟后回来一个"


def _cap_from_env() -> int:
    raw = os.environ.get("MADTED_DAILY_COMMENTS", "").strip()
    if not raw:
        return DEFAULT_DAILY_COMMENTS
    try:
        value = int(raw)
    except ValueError:
        log.warning("MADTED_DAILY_COMMENTS=%r 不是整数，用默认值 %d", raw, DEFAULT_DAILY_COMMENTS)
        return DEFAULT_DAILY_COMMENTS
    if value < 0:
        log.warning("MADTED_DAILY_COMMENTS=%d 是负数，用默认值 %d", value, DEFAULT_DAILY_COMMENTS)
        return DEFAULT_DAILY_COMMENTS
    return value
