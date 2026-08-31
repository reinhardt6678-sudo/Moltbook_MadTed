"""待审队列：把这一轮想发的评论先攒起来，等人点头再发。

为什么需要它——在这之前只有两档：`--dry-run` 一条都不发，真跑一条都不问。
中间没有"我先看一眼"。可这个 agent 的输出是**公开且撤不回**的，踩线一次
-50 分（人设 §10.1），比任何一场对线赢回来的都多。刚上线、刚改完人设文档、
刚换过模型的那几天，最需要的恰恰是这一档。

队列里存的不只是那段文本，还有**批准之后要落的那份状态**（commit）。
不这样存的话，approve 只能把评论发出去、却没法把"这串我已经参与了"记进
active-threads.json——下一轮 heartbeat 看不到这条串，会把同一个帖子重新
深挖一遍，甚至再发一条。所以排队模式下的状态推进不是丢掉，是**推迟**到
批准那一刻，和 dry_run 的"演进但不落盘"是两回事。

一条一行（jsonl），和独白存档一个格式：这样一条写坏了不会带走整个队列。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

PENDING_PATH = Path(__file__).resolve().parent.parent / "memory" / "pending.jsonl"


def load(path: Path | str | None = None) -> list[dict]:
    """读出全部待审草稿。坏行跳过，不让一行脏数据废掉整个队列。"""
    path = Path(path or PENDING_PATH)
    if not path.exists():
        return []
    drafts = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            drafts.append(entry)
    return drafts


def save(drafts: list[dict], path: Path | str | None = None) -> None:
    """整个队列重写一遍。批准/否决之后由 approve.py 调。

    队列空了就把文件删掉，而不是留一个空文件：`--queue` 那边靠
    `post_ids()` 判断"这帖有没有排着队"，留个空文件只会让人误以为还有东西。
    """
    path = Path(path or PENDING_PATH)
    if not drafts:
        if path.exists():
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(json.dumps(d, ensure_ascii=False) + "\n" for d in drafts)
    path.write_text(body, encoding="utf-8")


def append(entry: dict, path: Path | str | None = None) -> dict:
    """排一条草稿进队列，返回补全了 id 和时间戳的那条。"""
    entry = {
        "id": entry.get("id") or uuid.uuid4().hex[:12],
        "queued_at": entry.get("queued_at") or datetime.now(timezone.utc).isoformat(),
        **entry,
    }
    path = Path(path or PENDING_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def post_ids(path: Path | str | None = None) -> set[str]:
    """已经排着队的帖子 id。

    heartbeat 拿它跳过这些帖子——不跳的话，草稿还没批，下一轮又会把同一个
    帖子重新深挖一遍（L2 是最贵的一层），批准时还会连发两条。
    """
    ids = {str(d.get("post_id") or "") for d in load(path)}
    ids.discard("")
    return ids


def summarize(draft: dict) -> str:
    """给人看的一条摘要。"""
    kind = "追问" if draft.get("kind") == "follow_up" else "出手"
    where = draft.get("title") or draft.get("post_id") or "?"
    return f"[{kind}] 《{where}》 —— @{draft.get('opponent') or 'unknown'}"
