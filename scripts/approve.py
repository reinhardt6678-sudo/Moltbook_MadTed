"""过一眼待审草稿，点头的才发出去。

配合 `heartbeat.py --queue` 用：那边把这一轮想发的评论攒进 memory/pending.jsonl，
这里逐条给人看，批准的当场发到 Moltbook，并把**批准那一刻**该落的状态落下去
（讨论串、记忆、额度）——排队模式下这些状态是推迟的，不是丢掉的。

什么时候值得开着它跑：刚上线的头几天、刚改完人设文档、刚换过模型。这几种情况下
出问题的不是代码而是"它到底会说什么"，而那正是只有人能判的。

用法：
    python scripts/approve.py            # 逐条过
    python scripts/approve.py --list     # 只看有哪些，不发也不改
    python scripts/approve.py --clear    # 全部否决，清空队列
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import heartbeat  # noqa: E402
import pending  # noqa: E402
from budget import CommentBudget  # noqa: E402
from config import force_utf8_stdio, load_dotenv  # noqa: E402
from memory import Memory  # noqa: E402
from moltbook_client import MoltbookClient, MoltbookError, comment_id  # noqa: E402

log = logging.getLogger("madted.approve")


def _show(draft: dict, index: int, total: int) -> None:
    print("=" * 72)
    print(f"[{index}/{total}] {pending.summarize(draft)}")
    print(f"排队时间：{draft.get('queued_at', '?')}   角度：{draft.get('angle', '?')}")
    thinking = (draft.get("thinking") or "").strip()
    if thinking:
        print(f"\n为什么是这条：\n{thinking}")
    print(f"\n要发出去的内容：\n{draft.get('reply', '')}\n")


def _apply_follow_up(draft: dict, threads: dict, mem: Memory) -> bool:
    """把一条追问草稿的状态落到讨论串上。返回是否落成了。

    落的是**增量**：审批可能发生在几小时后，那时这条串在盘上大概率已经动过。
    """
    post_id = draft["post_id"]
    thread = threads.get(post_id)
    if thread is None:
        log.warning("讨论串 %s 已经不在盘上了（可能已收尾），只发评论、不改状态", post_id)
        return False

    commit = draft.get("commit", {})
    thread["turns"].extend(commit.get("opponent_turns", []))
    thread["turns"].append({"role": "self", "text": draft["reply"]})
    for rid in commit.get("seen_reply_ids", []):
        if rid and rid not in thread.setdefault("seen_reply_ids", []):
            thread["seen_reply_ids"].append(rid)
    thread["idle_cycles"] = 0
    thread["last_activity_at"] = datetime.now(timezone.utc).isoformat()
    thread["reply_language"] = commit.get("reply_language", thread.get("reply_language", ""))
    thread["rounds"] = thread.get("rounds", 0) + 1
    angle = commit.get("angle", "none")
    if angle and angle != "none":
        thread.setdefault("used_angles", []).append(angle)
    for flag in commit.get("verdict_flags", []):
        thread[flag] = True

    close = commit.get("close")
    if close:
        heartbeat._close_thread(
            mem, thread, post_id, outcome=close["outcome"], note=close.get("note", "")
        )
    return True


def _apply_deliberate(draft: dict, threads: dict, own_id: str) -> bool:
    """把一条新杠草稿装进讨论串档案。已经有同 id 的就不动——不能盖掉真跑的结果。"""
    post_id = draft["post_id"]
    if post_id in threads:
        log.warning("%s 在盘上已经有讨论串了，评论照发，但不覆盖已有状态", post_id)
        return False
    thread = dict(draft.get("commit", {}).get("thread") or {})
    thread["own_comment_ids"] = [own_id] if own_id else []
    thread["opened_at"] = datetime.now(timezone.utc).isoformat()
    thread["last_activity_at"] = thread["opened_at"]
    threads[post_id] = thread
    return True


def _send(draft: dict, client: MoltbookClient, threads: dict, mem: Memory, budget: CommentBudget) -> bool:
    """真的发出去，并落状态。返回是否发成功。"""
    post_id = draft["post_id"]
    try:
        result = client.create_comment(post_id, draft["reply"])
    except MoltbookError as exc:
        log.error("发送失败，这条留在队列里：%s", exc)
        return False

    own_id = comment_id(result or {})
    if draft.get("kind") == "follow_up":
        if _apply_follow_up(draft, threads, mem) and own_id:
            threads[post_id].setdefault("own_comment_ids", []).append(own_id)
        client.mark_post_read(post_id)
    else:
        _apply_deliberate(draft, threads, own_id)

    budget.spend()
    # 补一条不带 pending 标记的独白——排队时那条是"想发"，这条才是"发了"，
    # 战报统计的是后者（见 daily_report._read_monologues）。
    heartbeat._append_monologue(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": draft.get("kind", "deliberate"),
            "post_id": post_id,
            "opponent": draft.get("opponent", ""),
            "approved_from": draft.get("id", ""),
            "angle": draft.get("angle", ""),
            "thinking": draft.get("thinking", ""),
            "reply": draft["reply"],
        }
    )
    return True


def main() -> None:
    force_utf8_stdio()
    parser = argparse.ArgumentParser(description="过一眼待审草稿")
    parser.add_argument("--list", action="store_true", help="只列出来，什么都不发")
    parser.add_argument("--clear", action="store_true", help="全部否决，清空队列")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_dotenv()

    drafts = pending.load()
    if not drafts:
        print("队列是空的。先跑 python scripts/heartbeat.py --queue")
        return

    if args.list:
        for i, draft in enumerate(drafts, 1):
            print(f"{i}. {pending.summarize(draft)}")
            print(f"   {draft.get('reply', '')[:120]}")
        print(f"\n共 {len(drafts)} 条。逐条审：python scripts/approve.py")
        return

    if args.clear:
        pending.save([])
        print(f"已否决并清空 {len(drafts)} 条草稿，一条都没发。")
        return

    client = MoltbookClient.from_env()
    mem = Memory()
    threads = heartbeat._load_threads()
    budget = CommentBudget()

    kept: list[dict] = []
    sent = rejected = 0
    total = len(drafts)

    for i, draft in enumerate(drafts, 1):
        _show(draft, i, total)
        if budget.remaining <= 0:
            print(f"{budget.summary()}——剩下的全留在队列里，等额度回来再审。")
            kept.extend(drafts[i - 1:])
            break
        choice = input("发出去？[y] 发 / [n] 否决 / [s] 留着下次再说 / [q] 退出：").strip().lower()
        if choice == "y":
            if _send(draft, client, threads, mem, budget):
                sent += 1
            else:
                kept.append(draft)
        elif choice == "n":
            rejected += 1
        elif choice == "q":
            kept.extend(drafts[i - 1:])
            break
        else:
            kept.append(draft)

    # 先落状态再写队列：反过来的话，中途崩一次就等于草稿已经从队列里消失、
    # 评论却发出去了，而讨论串档案什么都不知道。
    mem.save()
    heartbeat._save_threads(threads)
    pending.save(kept)
    print(f"\n发出 {sent} 条，否决 {rejected} 条，队列里还剩 {len(kept)} 条。")
    print(f"杠力值 {mem.data['state']['gang_power']}｜{budget.summary()}")


if __name__ == "__main__":
    main()
