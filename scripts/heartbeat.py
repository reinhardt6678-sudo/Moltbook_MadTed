"""一次 heartbeat 周期的完整流程（人设文档 §6.1）。

顺序刻意如此：
  1. 先处理已参与讨论串的新回复（有新角度就追，没有就体面收尾）
  2. 剩余额度再去 feed 里开新杠
  3. 更新记忆、写独白日志

选题走三级漏斗，一级比一级贵：
  L0  radar.py   结构信号打分   免费     60 条 → 约 12 条
  L1  triage.py  Haiku 语义粗筛 约 $0.02  12 条 → 排序 + 剔红线
  L2  brain.py   Sonnet 深度独白 最贵     最多 max-deliberate 条

一轮之内的出手次数由 --max-new 管，**跨轮**的总量由 budget.py 的滚动 24 小时
额度管。后者必须落盘：cron 每小时拉起一个新进程，进程内的冷却记录跟着一起没了，
只有文件记得住"今天已经发过多少条"。

用法：
    python scripts/heartbeat.py                # 正常跑
    python scripts/heartbeat.py --dry-run      # 不真的发帖，只打印
    python scripts/heartbeat.py --max-new 2    # 限制本轮最多开 2 个新杠
    python scripts/heartbeat.py --daily-comments 30   # 收紧 24 小时总额度
    python scripts/heartbeat.py --no-triage    # 跳过 L1，只用 L0 排序
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from brain import Brain, FollowUp, Monologue, is_usable_reply  # noqa: E402
from budget import DEFAULT_DAILY_COMMENTS, CommentBudget  # noqa: E402
from config import force_utf8_stdio, load_dotenv  # noqa: E402
from memory import COLD_CAUSE_LANGUAGE, Memory  # noqa: E402
from moltbook_client import (  # noqa: E402
    MoltbookClient,
    MoltbookError,
    author_name,
    comment_id,
    notification_post_id,
    parent_comment_id,
)
import radar  # noqa: E402
from triage import Triage, split_redlines  # noqa: E402

log = logging.getLogger("madted")

ROOT = Path(__file__).resolve().parent.parent
MONOLOGUE_DIR = ROOT / "reports" / "monologue"
THREADS_PATH = ROOT / "memory" / "active-threads.json"

# 软保底：单帖最多来回多少轮（人设 §6.3）。正常应该在角度耗尽时自然结束。
MAX_ROUNDS = 8

# 单轮最多轮询几个讨论串的评论区。正常同时进行的对线远少于这个数，
# 这个上限只是防止 active-threads.json 意外堆积时把限流额度吃光。
MAX_THREAD_POLLS = 20

# 判冷场的**墙上时间**下限：从我最后一次出声算起，至少要过这么久。
#
# 为什么不能只数周期数：`stale_cycles=3` 这个条件的真实含义取决于你多久跑一次。
# 4 小时一轮时它等于"12 小时没人理"，改成每小时一轮之后同样的 3 轮只有 3 小时——
# 对方还没睡醒就被记进「免战名单」，用的角度被记成「钝刀」。定时器频率是运维
# 参数，不该顺手改掉 agent 的学习结论，所以这里用小时数把两者解耦。
DEFAULT_COLD_AFTER_HOURS = 12.0


def cold_after_hours() -> float:
    """判冷场要求的最短冷却时长。**运行时**读环境变量，不能在导入时定死——
    `.env` 是 main() 里才加载的，写成模块常量的话 MADTED_COLD_AFTER_HOURS
    只有 export 过才生效，写在 .env 里会被静默忽略。"""
    raw = os.environ.get("MADTED_COLD_AFTER_HOURS", "").strip()
    if not raw:
        return DEFAULT_COLD_AFTER_HOURS
    try:
        return float(raw)
    except ValueError:
        log.warning(
            "MADTED_COLD_AFTER_HOURS=%r 不是数字，用默认值 %.0f 小时",
            raw, DEFAULT_COLD_AFTER_HOURS,
        )
        return DEFAULT_COLD_AFTER_HOURS


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
        f"社区：{radar.submolt_of(post) or 'unknown'}\n"
        f"热度：{post.get('upvotes', 0)} 赞 / {post.get('comment_count', 0)} 评论\n"
        f"正文：\n{post.get('content') or post.get('body') or '(无正文)'}"
    )


def _speaker(thread: dict, turn: dict) -> str:
    if turn["role"] == "self":
        return "我(MadTed)"
    # 半路插进来的第三方也要如实标名，否则独白里会把别人的话当成对手说的
    return f"@{turn.get('author') or thread['opponent']}"


def _format_transcript(thread: dict, pending: list[dict] | None = None) -> str:
    lines = [f"原帖《{thread['title']}》 —— @{thread['opponent']}", ""]
    for turn in [*thread["turns"], *(pending or [])]:
        lines.append(f"{_speaker(thread, turn)}：{turn['text']}")
    return "\n".join(lines)


# ---------- 阶段 1：跟进已有讨论串 ----------


def _self_name(client: MoltbookClient) -> str:
    """自己在 Moltbook 上的名字。取不到就返回空字符串（靠 own_comment_ids 兜底）。"""
    override = os.environ.get("MADTED_AGENT_NAME", "").strip()
    if override:
        return override
    try:
        status = client.get_agent_status()
    except MoltbookError as exc:
        log.warning("拿不到自己的 agent 名字（%s），只能靠自己发过的评论 id 认自己", exc)
        return ""
    return str(status.get("name") or status.get("username") or "")


def _inbox_hints(client: MoltbookClient) -> set[str]:
    """收件箱点到名的帖子 id，仅用于决定先轮询谁。

    拿不到就返回空集合——收件箱**只是加速器**，不再是能不能看见回复的开关。
    之前那版把它当唯一入口：端点 404、字段名对不上、或者主人在网页端把通知
    点成已读，都会让 agent 一条回复都看不见，然后把满帖子的回复记成冷场。
    """
    try:
        activity = client.get_inbox_activity()
    except MoltbookError as exc:
        log.warning("拉取收件箱失败（%s），改为直接轮询各讨论串的评论区", exc)
        return set()

    hints = {notification_post_id(n) for n in activity}
    hints.discard("")
    return hints


def _new_replies_for(
    client: MoltbookClient, post_id: str, thread: dict, self_name: str
) -> list[dict] | None:
    """本轮该讨论串里冲我来的新回复。

    返回 None 表示**这次没查成**（拉取失败），和"查了但没有新回复"是两回事——
    前者不能拿去当冷场的证据。
    """
    try:
        replies = client.get_replies(post_id)
    except MoltbookError as exc:
        log.error("拉取 %s 的回复失败：%s", post_id, exc)
        return None

    seen = set(thread.get("seen_reply_ids", []))
    own = set(thread.get("own_comment_ids", []))
    opponent = thread["opponent"]
    # 老讨论串没存过 own_comment_ids，status 端点也可能不返回名字。
    # 这时靠原文比对认自己——比认错人去和自己对线强。
    own_texts = {t["text"] for t in thread["turns"] if t["role"] == "self"}

    fresh = []
    for reply in replies:
        rid = comment_id(reply)
        if rid and (rid in seen or rid in own):
            continue

        author = author_name(reply)
        if author and self_name and author == self_name:
            # 自己发的评论。不排掉的话会被当成对手的话写进 transcript，
            # 于是 MadTed 开始和自己对线。
            continue
        if (reply.get("content") or reply.get("body") or "") in own_texts:
            continue

        # 只在**能确定是回给别人**时才排除：父级明确指向不是我的评论、
        # 且作者也不是原帖作者。字段缺失一律按"可能是冲我来的"处理——
        # 宁可多读一条无关评论，也不能把真回复漏成冷场。
        parent = parent_comment_id(reply)
        if own and parent and parent not in own and author != opponent:
            continue

        fresh.append(reply)
    return fresh


def _reply_to_turn(reply: dict) -> dict:
    """把 API 返回的评论转成对线记录里的一轮。"""
    return {
        "role": "opponent",
        "author": author_name(reply),
        "text": reply.get("content") or reply.get("body") or "",
    }


def _commit_replies(thread: dict, replies: list[dict], turns: list[dict]) -> None:
    """把回复写进对线记录。只有在确定要用它们之后才调用。"""
    thread["turns"].extend(turns)
    for reply in replies:
        rid = comment_id(reply)
        if rid:
            thread.setdefault("seen_reply_ids", []).append(rid)


def follow_up_threads(
    client: MoltbookClient,
    brain: Brain,
    mem: Memory,
    threads: dict,
    *,
    dry_run: bool,
    budget: CommentBudget | None = None,
) -> tuple[int, set[str]]:
    """检查已参与的讨论串有没有新回复，有新角度就追，没有就收尾。

    返回 (跟进数, 本轮真正查清楚了的 post_id 集合)。第二项给 _reap_cold_threads：
    只有确认过"评论区里确实没有新回复"的串才够格计入冷场，拉取失败的不算。

    `budget=None` 表示不限额，只有测试和手工调用会这样；正常入口一律由
    run_cycle 传一个进来。跟进排在开新杠前面，所以额度天然优先给老对手——
    半截撂下一场正在来回的对线，比这轮少开一个新杠难看得多。
    """
    handled = 0
    checked: set[str] = set()
    sharp, blunt, banned = mem.angle_preference()

    self_name = _self_name(client)
    hints = _inbox_hints(client)

    open_threads = [(pid, t) for pid, t in threads.items() if not t.get("closed")]
    # 通知点过名的排前面，轮询上限被打满时先保证这些
    open_threads.sort(key=lambda item: item[0] not in hints)
    if len(open_threads) > MAX_THREAD_POLLS:
        log.warning(
            "有 %d 个进行中的讨论串，本轮只轮询前 %d 个",
            len(open_threads),
            MAX_THREAD_POLLS,
        )
        open_threads = open_threads[:MAX_THREAD_POLLS]

    for post_id, thread in open_threads:
        new_replies = _new_replies_for(client, post_id, thread, self_name)
        if new_replies is None:
            continue
        checked.add(post_id)
        if not new_replies:
            continue

        # 有人接话了，闲置计数清零——不清零的话，一个每轮都有回复的热闹
        # 讨论串照样会在第 3 个周期被判冷场。
        thread["idle_cycles"] = 0
        pending = [_reply_to_turn(r) for r in new_replies]

        # 软保底：轮数到顶就强制收尾
        if thread["rounds"] >= MAX_ROUNDS:
            log.info("%s 已达软保底 %d 轮，收尾", post_id, MAX_ROUNDS)
            _commit_replies(thread, new_replies, pending)
            _close_thread(mem, thread, post_id, outcome="多轮激辩", note="到软保底轮数，主动收尾")
            handled += 1
            continue

        if budget is not None and budget.remaining <= 0:
            # 额度用完就别问大脑了——想出来也发不出去，白花一次 API 钱。
            # 这些回复**不**记进 seen_reply_ids，下轮额度回来了再接着答。
            log.warning("%s 有 %d 条新回复，但%s，留到下轮", post_id, len(new_replies), budget.summary())
            continue

        decision: FollowUp | None = brain.follow_up(
            _format_transcript(thread, pending=pending),
            thread.get("used_angles", []),
            sharp=sharp,
            blunt=blunt,
            banned=banned,
            opponent_profile=mem.opponent_profile(thread["opponent"]),
            target_language=thread.get("post_language", ""),
        )
        # 空回复和没给出决定是一回事：都不能发，也都得留到下轮重试。
        # 判空要在 _commit_replies 之前——回复**不**记进 seen_reply_ids，
        # 记了就等于永久吞掉这条回复，然后这串会以 rounds=1 被判冷场。
        if decision is None or not is_usable_reply(decision.reply):
            log.warning("%s 有 %d 条新回复但大脑没给出可用的决定（reply=%r），留到下轮重试",
                        post_id, len(new_replies), None if decision is None else decision.reply)
            continue

        _commit_replies(thread, new_replies, pending)
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
                result = client.create_comment(post_id, decision.reply)
            except MoltbookError as exc:
                log.error("追问发送失败：%s", exc)
                continue
            _remember_own_comment(thread, result)
            # 这帖的回复已经处理完了，销掉未读，别让主人的未读数一直涨
            client.mark_post_read(post_id)

        if budget is not None:
            budget.spend()

        thread["turns"].append({"role": "self", "text": decision.reply})
        thread["last_activity_at"] = datetime.now(timezone.utc).isoformat()
        thread["reply_language"] = radar.detect_language(decision.reply)
        thread["rounds"] += 1
        if decision.angle != "none":
            thread.setdefault("used_angles", []).append(decision.angle)
        handled += 1

        if not decision.has_new_angle:
            outcome = "我认输" if decision.conceded else "多轮激辩"
            _close_thread(mem, thread, post_id, outcome=outcome, note=decision.thinking[:120])

    return handled, checked


def _remember_own_comment(thread: dict, result: dict | None) -> None:
    """记下自己发出去的评论 id。

    两个用处：认出评论区里哪条是自己说的（否则会自己跟自己对线），
    以及判断别人的回复是不是冲我来的（parent_id 指向它）。
    """
    rid = comment_id(result or {})
    if rid:
        thread.setdefault("own_comment_ids", []).append(rid)


def _cold_cause(thread: dict) -> str:
    """冷场归因（人设 §8.2）：先排除『对方根本读不懂』这种无效样本。

    如果我们用中文回了一条英文帖，没人理是必然的——这既说明不了角度钝，
    也说明不了这个对手不接招。不做这层排除，一次语言事故就会把一个好角度
    记进「钝刀」，把一个正常对手记进「免战名单」。
    """
    post_lang = thread.get("post_language", "")
    reply_lang = thread.get("reply_language", "")
    if post_lang and reply_lang and post_lang != reply_lang:
        log.warning(
            "讨论串 %s：回复语言(%s)和原帖语言(%s)不一致，冷场判为无效样本",
            thread.get("title", "")[:20],
            reply_lang,
            post_lang,
        )
        return COLD_CAUSE_LANGUAGE
    return ""


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
        cold_cause=_cold_cause(thread) if outcome == "冷场" else "",
        signals=thread.get("signals", {}),
    )
    log.info("讨论串 %s 收尾：%s（%d 轮）", post_id, outcome, thread["rounds"])


def _quiet_hours(thread: dict) -> float | None:
    """我最后一次出声到现在过了多少小时。没有时间戳的老串返回 None。"""
    stamp = thread.get("last_activity_at") or thread.get("opened_at")
    if not stamp:
        return None
    try:
        when = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds() / 3600


def _reap_cold_threads(
    mem: Memory,
    threads: dict,
    checked: set[str],
    *,
    stale_cycles: int = 3,
    min_quiet_hours: float | None = None,
) -> None:
    """连续几个周期没人回应、且确实冷了足够久的讨论串判为冷场（人设 §8.2）。

    只有 checked 里的串——本轮真的读到了评论区、确认没有新回复——才计闲置。
    拉取失败的串不能计：那是"我没看见"，不是"没人理我"。把前者当后者，
    -2 杠力值事小，§8.2 的归因会把好角度记成钝刀、把正常对手拉进免战名单，
    一次接口故障能让记忆歪很久。

    周期数之外还要过 `min_quiet_hours` 这道墙上时间：周期数说的是"我查了几次"，
    小时数说的才是"人家晾了我多久"。只看前者的话，把定时器从 4 小时调成 1 小时
    就等于把冷场判定线从 12 小时偷偷改成 3 小时——同一批对手会突然集体变"冷淡"，
    而真实原因只是我查得勤了。
    """
    if min_quiet_hours is None:
        min_quiet_hours = cold_after_hours()
    for post_id, thread in threads.items():
        if thread.get("closed") or post_id not in checked:
            continue
        thread["idle_cycles"] = thread.get("idle_cycles", 0) + 1
        if thread["idle_cycles"] < stale_cycles:
            continue
        quiet = _quiet_hours(thread)
        if quiet is not None and quiet < min_quiet_hours:
            log.debug(
                "%s 已闲置 %d 个周期，但才过了 %.1f 小时（不足 %.1f），先不判",
                post_id, thread["idle_cycles"], quiet, min_quiet_hours,
            )
            continue
        if thread["rounds"] <= 1:
            _close_thread(
                mem, thread, post_id, outcome="冷场", note="连续多个周期零回应"
            )
        else:
            # 来回过几轮才断的，不是冷场——对方接了招，只是没接到底。
            # 按冷场归因会冤枉这个角度和这个对手。
            _close_thread(
                mem, thread, post_id, outcome="一轮即止", note="对方停止回应，收尾"
            )


# ---------- 阶段 2：开新杠 ----------


def open_new_battles(
    client: MoltbookClient,
    brain: Brain,
    mem: Memory,
    threads: dict,
    *,
    max_new: int,
    max_deliberate: int,
    dry_run: bool,
    triage: Triage | None = None,
    budget: CommentBudget | None = None,
) -> tuple[int, list[str]]:
    """浏览 feed，挑杠点最多的帖子出手。返回 (出手数, 放弃理由列表)。

    三级漏斗：L0 结构打分 → L1 语义粗筛 → L2 深度独白。

    max_deliberate 是本轮最多问几次 L2 的硬上限。加了 L1 之后这个上限的
    意义变了：以前它是"省钱"，现在它是"L2 只处理粗筛后的前几名"——
    钱省得更多，而且省下来的那几次没花在垃圾候选上。
    """
    keywords = radar.load_keywords()
    sharp, blunt, banned = mem.angle_preference()

    if budget is not None and budget.remaining < max_new:
        # 先把 max_new 压到额度以内，省的是 L2 的钱：否则会照常深挖 max_new 条，
        # 挖完才发现发不出去。跟进阶段已经先花过一部分额度了，这里看到的是余额。
        log.info("%s，本轮最多开 %d 个新杠", budget.summary(), budget.remaining)
        max_new = budget.remaining

    if max_new <= 0:
        return 0, []

    try:
        posts = client.get_feed(sort="hot", limit=60)
    except MoltbookError as exc:
        log.error("拉取 feed 失败：%s", exc)
        return 0, []

    # 已经参与过的帖子不再重复开杠
    posts = [p for p in posts if str(p.get("id")) not in threads]

    # ---- L0：结构层 ----
    accepted, rejected = radar.rank_feed(
        posts,
        keywords,
        truce_list=mem.truce_list,
        low_yield_topics=mem.low_yield_topics,
        signal_bias=mem.signal_bias(),
    )
    log.info("L0 扫描 %d 条 → %d 条候选，%d 条划走", len(posts), len(accepted), len(rejected))

    # 只有『有杠点但主动放弃』才算忍住了（§10.6）。以前把每条低分流水帖
    # 都算进来，日报里那个数字等于 feed 长度，纯属自我感动。
    restraint = radar.restraint_reasons(rejected)

    # ---- L1：语义粗筛 ----
    if triage is not None and accepted:
        scored = triage.rank(accepted)
        passable, triage_restraint = split_redlines(scored)
        restraint.extend(triage_restraint)
        log.info(
            "L1 粗筛 %d 条 → %d 条可上，%d 条剔除（红线/杠点太弱）",
            len(scored),
            len(passable),
            len(triage_restraint),
        )
        queue = [(s.candidate, s.verdict) for s in passable]
    else:
        queue = [(c, None) for c in accepted]

    skipped_summaries: list[str] = []
    engaged = 0
    deliberated = 0

    # ---- L2：深度独白 ----
    for cand, verdict in queue:
        if engaged >= max_new:
            break
        if deliberated >= max_deliberate:
            log.info("已问满 %d 次 L2，本轮不再深挖", max_deliberate)
            break
        deliberated += 1

        hint = ""
        if verdict is not None:
            hint = (
                f"杠点密度 {verdict.hook_score}/10；"
                f"主要缺陷 {verdict.defect}；建议角度 {verdict.angle}。"
                f"粗筛备注：{verdict.note}"
            )

        post_language = cand.signals.get("language", "")
        monologue: Monologue | None = brain.deliberate(
            _format_post(cand.post),
            skipped_summaries,
            sharp=sharp,
            blunt=blunt,
            banned=banned,
            opponent_profile=mem.opponent_profile(cand.author) or None,
            target_language=post_language,
            triage_hint=hint,
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
                "radar_signals": cand.signals,
                "post_language": post_language,
                "triage": verdict.model_dump() if verdict is not None else None,
                **monologue.model_dump(),
            }
        )

        if monologue.verdict != "出手":
            skipped_summaries.append(f"{cand.summary()} → {monologue.why_this_one}")
            restraint.append(monologue.restraint_reason or "杠点太弱")
            continue

        if not is_usable_reply(monologue.reply):
            # 判定出手却没写出回复，只可能是输出残缺。不发、不扣额度，
            # 也不记进「忍住了」——那个计数的含义是"有杠点但主动放弃"，
            # 把残次品混进去等于把 bug 记成了美德。
            log.warning("《%s》判定出手但回复不可用（%r），跳过",
                        cand.title[:30], monologue.reply)
            continue

        own_comment_ids: list[str] = []
        if dry_run:
            log.info("[dry-run] 对 %s 出手: %s", cand.post_id, monologue.reply)
        else:
            try:
                result = client.create_comment(cand.post_id, monologue.reply)
            except MoltbookError as exc:
                log.error("评论发送失败：%s", exc)
                continue
            own_id = comment_id(result or {})
            if own_id:
                own_comment_ids.append(own_id)

        if budget is not None:
            budget.spend()

        # 回复实际用的语言按发出去的文本判定，不信模型自报的 reply_language——
        # 冷场归因要靠它排除无效样本，自报可能和实际不符。
        actual_reply_language = radar.detect_language(monologue.reply)
        if post_language and actual_reply_language != post_language:
            log.warning(
                "回复语言(%s)和原帖(%s)不一致：%s",
                actual_reply_language,
                post_language,
                monologue.reply[:60],
            )

        threads[cand.post_id] = {
            "title": cand.title,
            "opponent": cand.author,
            "topic_type": verdict.defect if verdict is not None else (
                ",".join(cand.hits.keys()) or "未分类"
            ),
            "rounds": 1,
            "used_angles": [monologue.angle],
            "turns": [{"role": "self", "text": monologue.reply}],
            "seen_reply_ids": [],
            "own_comment_ids": own_comment_ids,
            "idle_cycles": 0,
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "last_activity_at": datetime.now(timezone.utc).isoformat(),
            "post_language": post_language,
            "reply_language": actual_reply_language,
            "signals": cand.signals,
            "closed": False,
        }
        engaged += 1
        log.info("已对《%s》出手（角度 %s / %s）", cand.title[:30], monologue.angle, post_language)

    return engaged, restraint


# ---------- 主流程 ----------


def run_cycle(
    *,
    dry_run: bool = False,
    max_new: int = 1,
    effort: str = "medium",
    model: str | None = None,
    max_deliberate: int = 4,
    use_triage: bool = True,
    triage_model: str | None = None,
    daily_comments: int | None = None,
) -> None:
    client = MoltbookClient.from_env(dry_run=dry_run)
    brain = Brain(effort=effort, model=model)
    # L1 和 L2 共用一个 anthropic 客户端，省一次连接初始化
    triage = Triage(client=brain.client, model=triage_model) if use_triage else None
    mem = Memory()
    threads = _load_threads()
    # 落盘的额度，跨进程有效——每小时一轮时唯一挡得住"一天发爆"的东西
    budget = CommentBudget(cap=daily_comments, dry_run=dry_run)

    log.info("=== heartbeat 开始 | 杠力值 %s (%s) | %s ===",
             mem.data["state"]["gang_power"], mem.data["state"]["rank"], budget.summary())

    followed, checked = follow_up_threads(
        client, brain, mem, threads, dry_run=dry_run, budget=budget
    )
    log.info("阶段1：跟进了 %d 个讨论串（查清 %d 个）", followed, len(checked))

    _reap_cold_threads(mem, threads, checked)

    engaged, restraint = open_new_battles(
        client,
        brain,
        mem,
        threads,
        max_new=max_new,
        max_deliberate=max_deliberate,
        dry_run=dry_run,
        triage=triage,
        budget=budget,
    )
    log.info("阶段2：开了 %d 个新杠，忍住了 %d 条", engaged, len(restraint))

    mem.record_restraint(restraint)
    mem.save()
    _save_threads(threads)
    log.info("=== heartbeat 结束 | 杠力值 %s | %s ===",
             mem.data["state"]["gang_power"], budget.summary())


def main() -> None:
    # 日志里会原样带上帖子标题，而标题里什么 emoji 都可能有，得先把 stderr 定成 UTF-8
    force_utf8_stdio()
    parser = argparse.ArgumentParser(description="MadTed 的一次 heartbeat 周期")
    parser.add_argument("--dry-run", action="store_true", help="不真的发评论，只打印")
    parser.add_argument(
        "--max-new",
        type=int,
        default=1,
        help="本轮最多开几个新杠。默认 1 是按每小时一轮配的——4 小时一轮可以调到 3",
    )
    parser.add_argument(
        "--daily-comments",
        type=int,
        default=None,
        help="滚动 24 小时内最多发几条评论（出手 + 追问合计）。"
        f"默认 {DEFAULT_DAILY_COMMENTS}，也可设 MADTED_DAILY_COMMENTS 环境变量。"
        "平台文档写的是 50 条/天",
    )
    parser.add_argument(
        "--effort",
        default="medium",
        choices=["low", "medium", "high", "xhigh", "max"],
        help="Claude 的 effort 档位，越高越能挖出刁钻角度但也越贵",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="覆盖模型（默认 claude-sonnet-5，也可设 MADTED_MODEL 环境变量）。"
        "更省：claude-haiku-4-5；更强：claude-opus-5",
    )
    parser.add_argument(
        "--max-deliberate",
        type=int,
        default=4,
        help="本轮最多问几次 L2 深度独白（含判定划走的）。省钱的主要开关",
    )
    parser.add_argument(
        "--no-triage",
        action="store_true",
        help="跳过 L1 语义粗筛，只用 L0 结构分排序（省那 ~$0.02，但选题精度下降）",
    )
    parser.add_argument(
        "--triage-model",
        default=None,
        help="L1 粗筛用的模型（默认 claude-haiku-4-5，也可设 MADTED_TRIAGE_MODEL 环境变量）",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # 自动读 .env，这样 cron 和 Windows 都不用先 source
    load_dotenv()
    run_cycle(
        dry_run=args.dry_run,
        max_new=args.max_new,
        effort=args.effort,
        model=args.model,
        max_deliberate=args.max_deliberate,
        use_triage=not args.no_triage,
        triage_model=args.triage_model,
        daily_comments=args.daily_comments,
    )


if __name__ == "__main__":
    main()
