"""一次 heartbeat 周期的完整流程（人设文档 §6.1）。

顺序刻意如此：
  1. 先处理已参与讨论串的新回复（有新角度就追，没有就体面收尾）
  2. 剩余额度再去 feed 里开新杠
  3. 更新记忆、写独白日志

用法：
    python scripts/heartbeat.py                # 正常跑
    python scripts/heartbeat.py --dry-run      # 不真的发帖，只打印
    python scripts/heartbeat.py --max-new 2    # 限制本轮最多开 2 个新杠
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from brain import Brain, FollowUp, Monologue  # noqa: E402
from config import load_dotenv  # noqa: E402
from memory import Memory  # noqa: E402
from moltbook_client import MoltbookClient, MoltbookError  # noqa: E402
import radar  # noqa: E402

log = logging.getLogger("madted")

ROOT = Path(__file__).resolve().parent.parent
MONOLOGUE_DIR = ROOT / "reports" / "monologue"
THREADS_PATH = ROOT / "memory" / "active-threads.json"

# 软保底：单帖最多来回多少轮（人设 §6.3）。正常应该在角度耗尽时自然结束。
MAX_ROUNDS = 8


def _load_threads() -> dict:
    if THREADS_PATH.exists():
        return json.loads(THREADS_PATH.read_text(encoding="utf-8"))
    return {}


def _save_threads(threads: dict) -> None:
    THREADS_PATH.parent.mkdir(parents=True, exist_ok=True)
    THREADS_PATH.write_text(
        json.dumps(threads, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _append_monologue(entry: dict) -> None:
    """内心独白按天存档，日报和主人都从这里读。"""
    MONOLOGUE_DIR.mkdir(parents=True, exist_ok=True)
    path = MONOLOGUE_DIR / f"{date.today().isoformat()}.jsonl"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _format_post(post: dict) -> str:
    author = post.get("author")
    author_name = author.get("name") if isinstance(author, dict) else author
    return (
        f"标题：{post.get('title', '(无标题)')}\n"
        f"作者：@{author_name or 'unknown'}\n"
        f"社区：{post.get('submolt', 'unknown')}\n"
        f"热度：{post.get('upvotes', 0)} 赞 / {post.get('comment_count', 0)} 评论\n"
        f"正文：\n{post.get('content') or post.get('body') or '(无正文)'}"
    )


def _format_transcript(thread: dict) -> str:
    lines = [f"原帖《{thread['title']}》 —— @{thread['opponent']}", ""]
    for turn in thread["turns"]:
        who = "我(MadTed)" if turn["role"] == "self" else f"@{thread['opponent']}"
        lines.append(f"{who}：{turn['text']}")
    return "\n".join(lines)


# ---------- 阶段 1：跟进已有讨论串 ----------


def follow_up_threads(
    client: MoltbookClient, brain: Brain, mem: Memory, threads: dict, *, dry_run: bool
) -> int:
    """检查已参与的讨论串有没有新回复，有新角度就追，没有就收尾。"""
    handled = 0
    sharp, blunt, banned = mem.angle_preference()

    try:
        notifications = client.get_notifications(unread_only=True)
    except MoltbookError as exc:
        log.error("拉取通知失败：%s", exc)
        return 0

    replied_posts = {
        str(n.get("post_id") or n.get("target_id") or "")
        for n in notifications
        if n.get("type") in ("reply", "comment", "mention")
    }

    for post_id, thread in list(threads.items()):
        if thread.get("closed"):
            continue
        if post_id not in replied_posts:
            continue

        try:
            replies = client.get_replies(post_id)
        except MoltbookError as exc:
            log.error("拉取 %s 的回复失败：%s", post_id, exc)
            continue

        new_replies = [
            r for r in replies if str(r.get("id")) not in thread.get("seen_reply_ids", [])
        ]
        if not new_replies:
            continue

        for reply in new_replies:
            thread["turns"].append({"role": "opponent", "text": reply.get("content", "")})
            thread.setdefault("seen_reply_ids", []).append(str(reply.get("id")))

        # 软保底：轮数到顶就强制收尾
        if thread["rounds"] >= MAX_ROUNDS:
            log.info("%s 已达软保底 %d 轮，收尾", post_id, MAX_ROUNDS)
            _close_thread(mem, thread, post_id, outcome="多轮激辩", note="到软保底轮数，主动收尾")
            handled += 1
            continue

        decision: FollowUp | None = brain.follow_up(
            _format_transcript(thread),
            thread.get("used_angles", []),
            sharp=sharp,
            blunt=blunt,
            banned=banned,
            opponent_profile=mem.opponent_profile(thread["opponent"]),
        )
        if decision is None:
            continue

        _append_monologue(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "kind": "follow_up",
                "post_id": post_id,
                "opponent": thread["opponent"],
                "round": thread["rounds"] + 1,
                "thinking": decision.thinking,
                "has_new_angle": decision.has_new_angle,
                "conceded": decision.conceded,
                "angle": decision.angle,
                "reply": decision.reply,
            }
        )

        if dry_run:
            log.info("[dry-run] 追问 %s: %s", post_id, decision.reply)
        else:
            try:
                client.create_comment(post_id, decision.reply)
            except MoltbookError as exc:
                log.error("追问发送失败：%s", exc)
                continue

        thread["turns"].append({"role": "self", "text": decision.reply})
        thread["rounds"] += 1
        if decision.angle != "none":
            thread.setdefault("used_angles", []).append(decision.angle)
        handled += 1

        if not decision.has_new_angle:
            outcome = "我认输" if decision.conceded else "多轮激辩"
            _close_thread(mem, thread, post_id, outcome=outcome, note=decision.thinking[:120])

    return handled


def _close_thread(mem: Memory, thread: dict, post_id: str, *, outcome: str, note: str) -> None:
    thread["closed"] = True
    mem.record_battle(
        post_id=post_id,
        topic_type=thread.get("topic_type", "未分类"),
        opponent=thread["opponent"],
        angle_used=thread.get("used_angles", ["unknown"])[0],
        rounds=thread["rounds"],
        outcome=outcome,
        note=note,
    )
    log.info("讨论串 %s 收尾：%s（%d 轮）", post_id, outcome, thread["rounds"])


def _reap_cold_threads(mem: Memory, threads: dict, *, stale_cycles: int = 3) -> None:
    """连续几个周期没人回应的讨论串判为冷场，归因后关闭（人设 §8.2）。"""
    for post_id, thread in threads.items():
        if thread.get("closed"):
            continue
        thread["idle_cycles"] = thread.get("idle_cycles", 0) + 1
        if thread["idle_cycles"] >= stale_cycles and thread["rounds"] <= 1:
            _close_thread(
                mem, thread, post_id, outcome="冷场", note="连续多个周期零回应"
            )


# ---------- 阶段 2：开新杠 ----------


def open_new_battles(
    client: MoltbookClient,
    brain: Brain,
    mem: Memory,
    threads: dict,
    *,
    max_new: int,
    dry_run: bool,
) -> tuple[int, list[str]]:
    """浏览 feed，挑杠点最多的帖子出手。返回 (出手数, 放弃理由列表)。"""
    keywords = radar.load_keywords()
    sharp, blunt, banned = mem.angle_preference()

    try:
        posts = client.get_feed(sort="hot", limit=60)
    except MoltbookError as exc:
        log.error("拉取 feed 失败：%s", exc)
        return 0, []

    # 已经参与过的帖子不再重复开杠
    posts = [p for p in posts if str(p.get("id")) not in threads]

    accepted, rejected = radar.rank_feed(
        posts,
        keywords,
        truce_list=mem.truce_list,
        low_yield_topics=mem.low_yield_topics,
    )
    log.info("扫描 %d 条帖子 → %d 条候选，%d 条直接划走", len(posts), len(accepted), len(rejected))

    restraint_reasons = [
        c.veto_reason or "杠点太弱" for c in rejected if c.veto_reason or c.score < 4.0
    ]
    skipped_summaries: list[str] = []
    engaged = 0

    for cand in accepted:
        if engaged >= max_new:
            break

        monologue: Monologue | None = brain.deliberate(
            _format_post(cand.post),
            skipped_summaries,
            sharp=sharp,
            blunt=blunt,
            banned=banned,
            opponent_profile=mem.opponent_profile(cand.author) or None,
        )
        if monologue is None:
            continue

        _append_monologue(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "kind": "deliberate",
                "post_id": cand.post_id,
                "author": cand.author,
                "radar_score": cand.score,
                "radar_reasons": cand.reasons,
                **monologue.model_dump(),
            }
        )

        if monologue.verdict != "出手":
            skipped_summaries.append(f"{cand.summary()} → {monologue.why_this_one}")
            restraint_reasons.append(monologue.restraint_reason or "杠点太弱")
            continue

        if dry_run:
            log.info("[dry-run] 对 %s 出手: %s", cand.post_id, monologue.reply)
        else:
            try:
                client.create_comment(cand.post_id, monologue.reply)
            except MoltbookError as exc:
                log.error("评论发送失败：%s", exc)
                continue

        threads[cand.post_id] = {
            "title": cand.title,
            "opponent": cand.author,
            "topic_type": ",".join(cand.hits.keys()) or "未分类",
            "rounds": 1,
            "used_angles": [monologue.angle],
            "turns": [{"role": "self", "text": monologue.reply}],
            "seen_reply_ids": [],
            "idle_cycles": 0,
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "closed": False,
        }
        engaged += 1
        log.info("已对《%s》出手（角度 %s）", cand.title[:30], monologue.angle)

    return engaged, restraint_reasons


# ---------- 主流程 ----------


def run_cycle(*, dry_run: bool = False, max_new: int = 3, effort: str = "medium") -> None:
    client = MoltbookClient.from_env(dry_run=dry_run)
    brain = Brain(effort=effort)
    mem = Memory()
    threads = _load_threads()

    log.info("=== heartbeat 开始 | 杠力值 %s (%s) ===",
             mem.data["state"]["gang_power"], mem.data["state"]["rank"])

    followed = follow_up_threads(client, brain, mem, threads, dry_run=dry_run)
    log.info("阶段1：跟进了 %d 个讨论串", followed)

    _reap_cold_threads(mem, threads)

    engaged, restraint = open_new_battles(
        client, brain, mem, threads, max_new=max_new, dry_run=dry_run
    )
    log.info("阶段2：开了 %d 个新杠，忍住了 %d 条", engaged, len(restraint))

    mem.record_restraint(restraint)
    mem.save()
    _save_threads(threads)
    log.info("=== heartbeat 结束 | 杠力值 %s ===", mem.data["state"]["gang_power"])


def main() -> None:
    parser = argparse.ArgumentParser(description="MadTed 的一次 heartbeat 周期")
    parser.add_argument("--dry-run", action="store_true", help="不真的发评论，只打印")
    parser.add_argument("--max-new", type=int, default=3, help="本轮最多开几个新杠")
    parser.add_argument(
        "--effort",
        default="medium",
        choices=["low", "medium", "high", "xhigh", "max"],
        help="Claude 的 effort 档位，越高越能挖出刁钻角度但也越贵",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # 自动读 .env，这样 cron 和 Windows 都不用先 source
    load_dotenv()
    run_cycle(dry_run=args.dry_run, max_new=args.max_new, effort=args.effort)


if __name__ == "__main__":
    main()
