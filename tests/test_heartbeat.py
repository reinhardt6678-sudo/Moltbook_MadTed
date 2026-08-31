"""跟进阶段的测试——纯逻辑，不发真实请求、不调 Claude。

这块以前一行测试都没有，而"看不见回复→记成冷场"的 bug 正好长在这里：
线上表现是日报里一串冷场，登进网页却看得见别人的回复。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import halt  # noqa: E402
import heartbeat  # noqa: E402
import pending  # noqa: E402
from brain import FollowUp, Monologue  # noqa: E402
from budget import CommentBudget  # noqa: E402
from memory import Memory  # noqa: E402
from moltbook_client import (  # noqa: E402
    MoltbookError,
    author_name,
    comment_id,
    notification_post_id,
    parent_comment_id,
)


class FakeClient:
    """假客户端：可以指定评论区内容，以及通知端点是否可用。"""

    def __init__(self, replies=None, *, notifications_fail=False, replies_fail=False, feed=None):
        self._replies = replies or {}
        self._feed = feed or []
        self.notifications_fail = notifications_fail
        self.replies_fail = replies_fail
        self.posted: list[tuple[str, str]] = []
        self.marked_read: list[str] = []
        # 收件箱条目。默认空，测 moderator 警告的用例往里塞。
        self.inbox: list[dict] = []

    def get_feed(self, **kwargs):
        return list(self._feed)

    def get_agent_status(self):
        return {"name": "MadTed"}

    def get_inbox_activity(self):
        if self.notifications_fail:
            raise MoltbookError("GET /notifications 失败: 404", 404, "")
        return list(self.inbox)

    def mark_post_read(self, post_id):
        self.marked_read.append(post_id)

    def get_replies(self, post_id, *, limit=100):
        if self.replies_fail:
            raise MoltbookError("boom", 500, "")
        return self._replies.get(post_id, [])

    def create_comment(self, post_id, content):
        self.posted.append((post_id, content))
        return {"id": f"own-{len(self.posted)}"}


class FakeBrain:
    """假大脑：按预设返回决策，None 表示这轮没给出结果。"""

    def __init__(self, decision: FollowUp | None, monologue: Monologue | None = None):
        self.decision = decision
        self.monologue = monologue
        self.calls: list[str] = []

    def follow_up(self, transcript, used_angles, **kwargs):
        self.calls.append(transcript)
        return self.decision

    def deliberate(self, post_text, skipped_summaries, **kwargs):
        self.calls.append(post_text)
        return self.monologue


class FakeMemory:
    def __init__(self):
        self.battles: list[dict] = []
        self.truce_list: list[str] = []
        self.low_yield_topics: list[str] = []

    def angle_preference(self):
        return ([], [], None)

    def opponent_profile(self, name):
        return {}

    def signal_bias(self):
        return {}

    def record_battle(self, **kwargs):
        self.battles.append(kwargs)


def make_thread(**overrides) -> dict:
    thread = {
        "title": "AI 一定会取代程序员",
        "opponent": "hypebot",
        "topic_type": "绝对化断言",
        "rounds": 1,
        "used_angles": ["3.4"],
        "turns": [{"role": "self", "text": "这个'一定'的证据是什么？"}],
        "seen_reply_ids": [],
        "own_comment_ids": ["own-1"],
        "idle_cycles": 0,
        "post_language": "zh",
        "reply_language": "zh",
        "signals": {},
        "closed": False,
    }
    thread.update(overrides)
    return thread


def _hours_ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _spent_budget(*, cap: int) -> CommentBudget:
    """测试用额度。dry_run 保证既不读也不写仓库里的真实账本。"""
    return CommentBudget(
        Path(__file__).resolve().parent / "_never-written-budget.json",
        cap=cap,
        dry_run=True,
    )


def a_reply(**overrides) -> dict:
    """一条**真的冲我来的**回复：挂在我那条评论（own-1）底下。

    评论区是有层级的，别人回我一定是挂在我的评论下面。楼里那些顶层评论是
    旁人自己在讨论，不算对方回了我——见 test_bystander_comment_is_not_a_reply。
    """
    reply = {
        "id": "r1",
        "author": {"name": "hypebot"},
        "content": "证据就是趋势",
        "parent_id": "own-1",
    }
    reply.update(overrides)
    return reply


def a_decision(**overrides) -> FollowUp:
    payload = {
        "has_new_angle": True,
        "thinking": "他偷换了前提",
        "conceded": False,
        "opponent_moved": False,
        "opponent_added_evidence": False,
        "factual_error": False,
        "angle": "3.6",
        "reply": "你这是双标。",
    }
    payload.update(overrides)
    return FollowUp(**payload)


def a_monologue(**overrides) -> Monologue:
    payload = {
        "scanned": "《Agents will completely replace human QA teams》—— @hypebot",
        "first_reaction": "又来了",
        "weak_points": "completely / every single 都是全称断言",
        "verdict": "出手",
        "why_this_one": "刚划走的两条都没这么绝对",
        "angle": "3.2",
        "angle_reason": "举个反例最快",
        "prediction": "他会退到'大部分'",
        "reply": "Which teams, and how did you count them?",
    }
    payload.update(overrides)
    return Monologue(**payload)


def a_post(**overrides) -> dict:
    post = {
        "id": "p9",
        "title": "Agents will completely replace human QA teams",
        "content": "Every single team I know has already switched. "
                   "This is inevitable and nobody should argue.",
        "author": {"name": "hypebot"},
        "upvotes": 30,
        "comment_count": 2,
    }
    post.update(overrides)
    return post


# fixture 会把 _append_monologue 挡掉，这里先留一份真身：要验真实写盘行为的
# 测试直接调它。
# EN: the fixture stubs _append_monologue out, so keep a handle on the real one
# here — tests that check actual disk writes call it directly.
_real_append_monologue = heartbeat._append_monologue


@pytest.fixture(autouse=True)
def no_disk_writes(monkeypatch):
    """独白日志写盘和测试无关，挡掉。"""
    monkeypatch.setattr(heartbeat, "_append_monologue", lambda entry, **kwargs: None)


# ---------- 核心回归：发现回复不依赖通知 ----------


def test_finds_replies_when_notifications_endpoint_is_dead():
    """通知端点挂了也要照常发现回复。

    这就是线上那个 bug：以前通知是唯一入口，它一 404，
    满帖子的回复一条都看不见，最后全被记成冷场。
    """
    client = FakeClient(
        {"p1": [a_reply()]},
        notifications_fail=True,
    )
    brain = FakeBrain(a_decision())
    threads = {"p1": make_thread()}

    handled, checked = heartbeat.follow_up_threads(
        client, brain, FakeMemory(), threads, dry_run=False
    )

    assert handled == 1
    assert checked == {"p1"}
    assert client.posted == [("p1", "你这是双标。")]
    assert threads["p1"]["rounds"] == 2


def test_fetch_failure_is_not_evidence_of_cold_shoulder():
    """拉取失败的串不能计入闲置——那是'我没看见'，不是'没人理我'。"""
    client = FakeClient({"p1": []}, replies_fail=True)
    mem = FakeMemory()
    threads = {"p1": make_thread(idle_cycles=2)}

    _, checked = heartbeat.follow_up_threads(
        client, FakeBrain(None), mem, threads, dry_run=False
    )
    heartbeat._reap_cold_threads(mem, threads, checked)

    assert checked == set()
    assert threads["p1"]["idle_cycles"] == 2  # 没涨
    assert threads["p1"]["closed"] is False
    assert mem.battles == []


def test_confirmed_silence_still_counts_as_cold():
    """真的查过、评论区确实没人接话，该判冷场还是要判。"""
    client = FakeClient({"p1": []})
    mem = FakeMemory()
    threads = {"p1": make_thread(idle_cycles=2)}

    _, checked = heartbeat.follow_up_threads(
        client, FakeBrain(None), mem, threads, dry_run=False
    )
    heartbeat._reap_cold_threads(mem, threads, checked)

    assert threads["p1"]["closed"] is True
    assert mem.battles[0]["outcome"] == "冷场"


# ---------- 别把自己的话当成对手的话 ----------


def test_own_comment_is_not_treated_as_a_reply():
    """评论区里自己那条不能被当成对手的新回复，否则会自己跟自己对线。"""
    client = FakeClient(
        {
            "p1": [
                {"id": "own-1", "author": {"name": "MadTed"}, "content": "我自己发的"},
                a_reply(content="对方回的"),
            ]
        }
    )
    brain = FakeBrain(a_decision())
    threads = {"p1": make_thread()}

    heartbeat.follow_up_threads(client, brain, FakeMemory(), threads, dry_run=False)

    opponent_turns = [t for t in threads["p1"]["turns"] if t["role"] == "opponent"]
    assert [t["text"] for t in opponent_turns] == ["对方回的"]


def test_third_party_reply_to_me_is_picked_up():
    """别的机器人回复我，也算数（主人看到的正是这种）。"""
    client = FakeClient(
        {
            "p1": [
                {
                    "id": "r9",
                    "author": {"name": "skeptic_bot"},
                    "parent_id": "own-1",
                    "content": "我插一句",
                }
            ]
        }
    )
    brain = FakeBrain(a_decision())
    threads = {"p1": make_thread()}

    handled, _ = heartbeat.follow_up_threads(
        client, brain, FakeMemory(), threads, dry_run=False
    )

    assert handled == 1
    # transcript 里要如实标出是谁说的，不能记到 hypebot 头上
    assert "@skeptic_bot" in brain.calls[0]


def test_reply_aimed_at_someone_else_is_skipped():
    """明确回给第三方的评论不该被当成冲我来的。"""
    client = FakeClient(
        {
            "p1": [
                {
                    "id": "r5",
                    "author": {"name": "lurker"},
                    "parent_id": "other-comment",
                    "content": "楼上说得对",
                }
            ]
        }
    )
    threads = {"p1": make_thread()}

    handled, checked = heartbeat.follow_up_threads(
        client, FakeBrain(a_decision()), FakeMemory(), threads, dry_run=False
    )

    assert handled == 0
    assert checked == {"p1"}


# ---------- 别吞掉回复 ----------


def test_reply_is_retried_when_brain_returns_nothing():
    """大脑没给出决定时，回复不能被记成已读——记了就永远丢了。"""
    client = FakeClient(
        {"p1": [a_reply(content="回了")]}
    )
    threads = {"p1": make_thread()}

    heartbeat.follow_up_threads(
        client, FakeBrain(None), FakeMemory(), threads, dry_run=False
    )

    assert threads["p1"]["seen_reply_ids"] == []
    assert threads["p1"]["turns"] == make_thread()["turns"]

    # 下一轮大脑恢复了，同一条回复要能被重新处理
    handled, _ = heartbeat.follow_up_threads(
        client, FakeBrain(a_decision()), FakeMemory(), threads, dry_run=False
    )
    assert handled == 1
    assert threads["p1"]["seen_reply_ids"] == ["r1"]


def test_idle_counter_resets_on_new_reply():
    """有人接话就把闲置计数清零，否则热闹的串也会被判冷场。"""
    client = FakeClient(
        {"p1": [a_reply(content="接话")]}
    )
    mem = FakeMemory()
    threads = {"p1": make_thread(idle_cycles=2)}

    _, checked = heartbeat.follow_up_threads(
        client, FakeBrain(a_decision()), mem, threads, dry_run=False
    )
    heartbeat._reap_cold_threads(mem, threads, checked)

    assert threads["p1"]["idle_cycles"] == 1
    assert threads["p1"]["closed"] is False


def test_abandoned_multi_round_thread_is_not_cold():
    """来回过几轮才断的不算冷场——对方接过招，按冷场归因会冤枉这个角度。"""
    client = FakeClient({"p1": []})
    mem = FakeMemory()
    threads = {"p1": make_thread(rounds=3, idle_cycles=2)}

    _, checked = heartbeat.follow_up_threads(
        client, FakeBrain(None), mem, threads, dry_run=False
    )
    heartbeat._reap_cold_threads(mem, threads, checked)

    assert threads["p1"]["closed"] is True
    assert mem.battles[0]["outcome"] == "一轮即止"


def test_own_reply_id_is_remembered_for_next_cycle():
    """追问发出去之后要记下自己的评论 id，下轮才认得出哪条是自己。"""
    client = FakeClient(
        {"p1": [a_reply(content="回了")]}
    )
    threads = {"p1": make_thread()}

    heartbeat.follow_up_threads(
        client, FakeBrain(a_decision()), FakeMemory(), threads, dry_run=False
    )

    assert "own-1" in threads["p1"]["own_comment_ids"]
    assert len(threads["p1"]["own_comment_ids"]) == 2


# ---------- 字段提取要认全各种写法 ----------


@pytest.mark.parametrize(
    "comment,expected",
    [
        ({"id": "c1"}, "c1"),
        ({"comment_id": "c2"}, "c2"),
        ({"_id": "c3"}, "c3"),
        ({"id": 44}, "44"),
        ({}, ""),
    ],
)
def test_comment_id_variants(comment, expected):
    assert comment_id(comment) == expected


@pytest.mark.parametrize(
    "comment,expected",
    [
        ({"author": {"name": "a"}}, "a"),
        ({"author": {"username": "b"}}, "b"),
        ({"author": "c"}, "c"),
        ({"agent": {"name": "d"}}, "d"),
        ({"author_name": "e"}, "e"),
        ({}, ""),
    ],
)
def test_author_name_variants(comment, expected):
    assert author_name(comment) == expected


@pytest.mark.parametrize(
    "comment,expected",
    [
        ({"parent_id": "p"}, "p"),
        ({"parentId": "p"}, "p"),
        ({"in_reply_to": "p"}, "p"),
        ({"parent": {"id": "p"}}, "p"),
        ({}, ""),
    ],
)
def test_parent_comment_id_variants(comment, expected):
    assert parent_comment_id(comment) == expected


@pytest.mark.parametrize(
    "notification,expected",
    [
        ({"post_id": "p1"}, "p1"),
        ({"target_id": "p1"}, "p1"),
        ({"postId": "p1"}, "p1"),
        ({"post": {"id": "p1"}}, "p1"),
        ({"data": {"post_id": "p1"}}, "p1"),
        ({"type": "reply"}, ""),
    ],
)
def test_notification_post_id_variants(notification, expected):
    assert notification_post_id(notification) == expected


# ---------- 判冷场要看墙上时间，不是查了几次 ----------
#
# 定时器从 4 小时改成 1 小时之后，同样的 stale_cycles=3 从"12 小时没人理"
# 变成了"3 小时没人理"。改运维频率不该顺手改掉 agent 的学习结论。


def test_recently_active_thread_is_not_cold_just_because_we_checked_often():
    """刚出手 1 小时，查了 3 遍没人回——那是人家没上线，不是冷场。"""
    client = FakeClient({"p1": []})
    mem = FakeMemory()
    threads = {"p1": make_thread(idle_cycles=2, last_activity_at=_hours_ago(1))}

    _, checked = heartbeat.follow_up_threads(
        client, FakeBrain(None), mem, threads, dry_run=False
    )
    heartbeat._reap_cold_threads(mem, threads, checked)

    assert threads["p1"]["idle_cycles"] == 3   # 照常计数
    assert threads["p1"]["closed"] is False    # 但先不判
    assert mem.battles == []


def test_thread_quiet_long_enough_is_still_reaped():
    """晾了一整天确实是冷场，时间够了就该判。"""
    client = FakeClient({"p1": []})
    mem = FakeMemory()
    threads = {"p1": make_thread(idle_cycles=2, last_activity_at=_hours_ago(20))}

    _, checked = heartbeat.follow_up_threads(
        client, FakeBrain(None), mem, threads, dry_run=False
    )
    heartbeat._reap_cold_threads(mem, threads, checked)

    assert threads["p1"]["closed"] is True
    assert mem.battles[0]["outcome"] == "冷场"


def test_thread_without_timestamp_falls_back_to_cycle_count():
    """老的 active-threads.json 没有时间戳字段，不能因此永远判不了冷场。"""
    client = FakeClient({"p1": []})
    mem = FakeMemory()
    threads = {"p1": make_thread(idle_cycles=2)}  # 无 last_activity_at / opened_at
    assert heartbeat._quiet_hours(threads["p1"]) is None

    _, checked = heartbeat.follow_up_threads(
        client, FakeBrain(None), mem, threads, dry_run=False
    )
    heartbeat._reap_cold_threads(mem, threads, checked)

    assert threads["p1"]["closed"] is True


def test_follow_up_refreshes_the_activity_timestamp():
    """每次追问都要刷新时间戳，否则一场热闹的多轮对线会拿开局时间去判冷场。"""
    client = FakeClient(
        {"p1": [a_reply(content="接话")]}
    )
    mem = FakeMemory()
    threads = {"p1": make_thread(last_activity_at=_hours_ago(20))}

    heartbeat.follow_up_threads(client, FakeBrain(a_decision()), mem, threads, dry_run=False)

    assert heartbeat._quiet_hours(threads["p1"]) < 1


# ---------- 评论额度 ----------


def test_follow_up_stops_when_budget_is_gone():
    """额度用完时不追问，也不能把这些回复吞掉——下轮还要接着答。"""
    client = FakeClient(
        {"p1": [a_reply(content="接话")]}
    )
    mem = FakeMemory()
    brain = FakeBrain(a_decision())
    threads = {"p1": make_thread()}

    handled, _ = heartbeat.follow_up_threads(
        client, brain, mem, threads, dry_run=False, budget=_spent_budget(cap=0)
    )

    assert handled == 0
    assert client.posted == []
    assert brain.calls == []                      # 也没白花 API 钱
    assert threads["p1"]["seen_reply_ids"] == []  # 回复留着，下轮再答
    assert threads["p1"]["rounds"] == 1


def test_follow_up_spends_one_unit_per_reply():
    client = FakeClient(
        {"p1": [a_reply(content="接话")]}
    )
    mem = FakeMemory()
    budget = _spent_budget(cap=5)
    threads = {"p1": make_thread()}

    heartbeat.follow_up_threads(
        client, FakeBrain(a_decision()), mem, threads, dry_run=False, budget=budget
    )

    assert client.posted == [("p1", "你这是双标。")]
    assert budget.used == 1


def test_no_budget_means_no_feed_fetch_and_no_llm_spend():
    """额度归零时连 feed 都不该拉——L2 深挖出来也发不出去，纯烧钱。"""
    class ExplodingClient:
        def get_feed(self, **kwargs):
            raise AssertionError("额度为 0 时不该拉 feed")

    engaged, restraint = heartbeat.open_new_battles(
        ExplodingClient(),
        FakeBrain(None),
        FakeMemory(),
        {},
        max_new=3,
        max_deliberate=4,
        dry_run=False,
        budget=_spent_budget(cap=0),
    )

    assert engaged == 0
    assert restraint == []


def test_cold_threshold_is_read_at_runtime_not_import_time(monkeypatch):
    """阈值必须运行时读——.env 是 main() 里才加载的，导入时定死等于只认 export。"""
    monkeypatch.setenv("MADTED_COLD_AFTER_HOURS", "3")
    assert heartbeat.cold_after_hours() == 3.0

    monkeypatch.setenv("MADTED_COLD_AFTER_HOURS", "半天")
    assert heartbeat.cold_after_hours() == heartbeat.DEFAULT_COLD_AFTER_HOURS


def test_lower_cold_threshold_lets_a_recent_thread_be_reaped():
    """调低阈值就该更早判冷场——参数是真的接上了，不是摆设。"""
    client = FakeClient({"p1": []})
    mem = FakeMemory()
    threads = {"p1": make_thread(idle_cycles=2, last_activity_at=_hours_ago(2))}

    _, checked = heartbeat.follow_up_threads(
        client, FakeBrain(None), mem, threads, dry_run=False
    )
    heartbeat._reap_cold_threads(mem, threads, checked, min_quiet_hours=1)

    assert threads["p1"]["closed"] is True


# ---------- 残次品不许出门（截断后的输出） ----------


def test_truncated_follow_up_is_not_posted_and_the_reply_waits_for_next_round():
    """回复塌成占位符时，这轮什么都别做——但**不能**把对方的回复吞掉。

    吞掉了下轮就看不见它，这串会以 rounds=1 熬到冷场，
    最后 agent 学到的是"这个角度没人接"，而真相是它自己没说出话来。
    """
    client = FakeClient(
        {"p1": [a_reply(content="接话")]}
    )
    mem = FakeMemory()
    budget = _spent_budget(cap=5)
    threads = {"p1": make_thread()}

    handled, _ = heartbeat.follow_up_threads(
        client,
        FakeBrain(a_decision(reply="x")),
        mem,
        threads,
        dry_run=False,
        budget=budget,
    )

    assert handled == 0
    assert client.posted == []
    assert budget.used == 0
    assert threads["p1"]["seen_reply_ids"] == []
    assert threads["p1"]["rounds"] == 1


def test_truncated_new_battle_is_not_posted():
    """判定出手但回复是残次品：不发、不扣额度、不开讨论串。

    2026-08-13 的真实事故——一条正文只有 'x' 的评论发到了 @vina 帖子下。
    """
    client = FakeClient(feed=[a_post()])
    mem = FakeMemory()
    budget = _spent_budget(cap=5)
    threads: dict = {}

    engaged, restraint = heartbeat.open_new_battles(
        client,
        FakeBrain(None, a_monologue(reply="x")),
        mem,
        threads,
        max_new=1,
        max_deliberate=4,
        dry_run=False,
        budget=budget,
    )

    assert engaged == 0
    assert client.posted == []
    assert budget.used == 0
    assert threads == {}
    # 也不算「忍住了」——那是主动放弃的计数，残次品混进去等于把 bug 记成美德
    assert restraint == []


def test_a_complete_new_battle_still_goes_out():
    """把关不能把正常的出手也拦下来。"""
    client = FakeClient(feed=[a_post()])
    budget = _spent_budget(cap=5)
    threads: dict = {}

    engaged, _ = heartbeat.open_new_battles(
        client,
        FakeBrain(None, a_monologue()),
        FakeMemory(),
        threads,
        max_new=1,
        max_deliberate=4,
        dry_run=False,
        budget=budget,
    )

    assert engaged == 1
    assert client.posted == [("p9", "Which teams, and how did you count them?")]
    assert budget.used == 1
    assert threads["p9"]["rounds"] == 1


# ---------- 旁人在楼里说话 ≠ 对方回我 ----------


def test_bystander_comment_is_not_a_reply():
    """楼里旁人的顶层评论不算对方回我，既不该触发追问，也不该清零闲置计数。

    这是线上那个 bug：认不出自己就判不出"这条是回谁的"，于是热帖底下任何人
    说任何话都被当成对方回了我。后果是明明没人接话却每轮都去追问，额度全花在
    自说自话上；idle_cycles 又被旁人的发言按住清零，这串永远等不到收尾。
    """
    client = FakeClient(
        {
            "p1": [
                {"id": "b1", "author": {"name": "路人甲"}, "content": "同意楼主"},
                {"id": "b2", "author": {"name": "路人乙"}, "content": "顺便问个别的"},
                # 对手本人在自己楼里另起一条，也没回我
                {"id": "b3", "author": {"name": "hypebot"}, "content": "补充一点"},
            ]
        }
    )
    mem = FakeMemory()
    threads = {"p1": make_thread(idle_cycles=2)}

    handled, checked = heartbeat.follow_up_threads(
        client, FakeBrain(a_decision()), mem, threads, dry_run=False
    )
    heartbeat._reap_cold_threads(mem, threads, checked)

    assert handled == 0
    assert client.posted == []
    assert threads["p1"]["idle_cycles"] == 3
    assert [t["role"] for t in threads["p1"]["turns"]] == ["self"]


def test_reply_nested_under_a_reply_to_me_counts():
    """回"回我的人"的那条也算这场对线——都在我挑起的那串底下。"""
    client = FakeClient(
        {
            "p1": [
                {"id": "r1", "author": {"name": "路人甲"}, "content": "我替楼主答",
                 "parent_id": "own-1"},
                {"id": "r2", "author": {"name": "hypebot"}, "content": "我也补一句",
                 "parent_id": "r1"},
            ]
        }
    )
    threads = {"p1": make_thread()}

    handled, _ = heartbeat.follow_up_threads(
        client, FakeBrain(a_decision()), FakeMemory(), threads, dry_run=False
    )

    assert handled == 1
    assert [t["text"] for t in threads["p1"]["turns"] if t["role"] == "opponent"] == [
        "我替楼主答",
        "我也补一句",
    ]


def test_mention_counts_as_a_reply():
    """评论区是平的时候（没有 parent_id），点名 @ 我就是唯一的信号。"""
    client = FakeClient(
        {"p1": [{"id": "r1", "author": {"name": "hypebot"},
                 "content": "@MadTed 你这个说法本身就有问题"}]}
    )
    threads = {"p1": make_thread()}

    handled, _ = heartbeat.follow_up_threads(
        client, FakeBrain(a_decision()), FakeMemory(), threads, dry_run=False
    )

    assert handled == 1


def test_self_is_recovered_when_own_comment_ids_are_empty():
    """老讨论串没存过自己的评论 id，要能靠作者名把自己回捞出来。

    否则"挂在我评论下面"这个判据没有锚点，真回复照样会被当成旁人讨论漏掉。
    """
    client = FakeClient(
        {
            "p1": [
                {"id": "c-mine", "author": {"name": "MadTed"},
                 "content": "这个'一定'的证据是什么？"},
                {"id": "r1", "author": {"name": "hypebot"}, "content": "证据是趋势",
                 "parent_id": "c-mine"},
            ]
        }
    )
    threads = {"p1": make_thread(own_comment_ids=[])}

    handled, _ = heartbeat.follow_up_threads(
        client, FakeBrain(a_decision()), FakeMemory(), threads, dry_run=False
    )

    assert handled == 1
    # 回捞到的要存回去，下一轮不用再猜
    assert "c-mine" in threads["p1"]["own_comment_ids"]


def test_self_name_reads_the_nested_agent_object():
    """名字在 agent 子对象里。读成空字符串的话，认自己就只剩 id 一条路。"""

    class Status:
        def get_agent_status(self):
            return {"success": True, "agent": {"name": "codingclaude_559248874"}}

    assert heartbeat._self_name(Status()) == "codingclaude_559248874"


def test_unidentifiable_self_does_not_count_as_silence():
    """认不出自己就没有判"这条回给谁"的锚点，这一轮该按没查成算。

    否则整个评论区都归不了类，看上去就是"没人回我"——又一次把
    "我没看见"当成"没人理我"，好角度会因此进「钝刀」。
    """

    class NamelessClient(FakeClient):
        def get_agent_status(self):
            return {"success": True}

    client = NamelessClient(
        {"p1": [{"id": "b1", "author": {"name": "路人甲"}, "content": "楼里聊别的"}]}
    )
    mem = FakeMemory()
    # 自己的评论 id 没存过，turns 里的原文也对不上评论区
    threads = {"p1": make_thread(own_comment_ids=[], turns=[{"role": "self", "text": "对不上"}])}

    handled, checked = heartbeat.follow_up_threads(
        client, FakeBrain(a_decision()), mem, threads, dry_run=False
    )

    assert handled == 0
    assert checked == set()          # 没查成 → 不计入闲置
    heartbeat._reap_cold_threads(mem, threads, checked)
    assert threads["p1"]["idle_cycles"] == 0


# ---------- 空跑不许留下痕迹 ----------


def _wire_cycle(monkeypatch, tmp_path):
    """把 run_cycle 的外部依赖换成假的，只留下记忆和讨论串这两条真实的落盘路径。

    Memory 和 _save_threads 用的是真货、只是把路径挪进 tmp——"空跑到底写不写盘"
    正是这两个要测的东西，换成假的就等于测了个寂寞。

    EN: swap run_cycle's external dependencies for fakes, keeping the two real
    persistence paths — memory and threads. Memory and _save_threads are the
    genuine article, only pointed at tmp: whether a dry run writes to disk is
    exactly what those two decide, so faking them would test nothing at all.
    """
    memory_path = tmp_path / "madted-memory.json"
    threads_path = tmp_path / "active-threads.json"

    Memory(memory_path).save()
    threads_path.write_text(
        json.dumps({"p1": make_thread()}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 对方回了我一条（挂在 own-1 下面）→ 跟进阶段有活干；
    # feed 里再放一条够格的帖子 → 开新杠阶段也有活干。
    # EN: one reply aimed at me (nested under own-1) gives the follow-up stage
    # work; one qualifying post in the feed gives the new-battle stage its own.
    client = FakeClient(replies={"p1": [a_reply()]}, feed=[a_post()])

    monkeypatch.setattr(
        heartbeat, "MoltbookClient", SimpleNamespace(from_env=lambda **kw: client)
    )
    monkeypatch.setattr(
        heartbeat, "Brain", lambda **kw: FakeBrain(a_decision(), a_monologue())
    )
    monkeypatch.setattr(heartbeat, "Memory", lambda **kw: Memory(memory_path, **kw))
    monkeypatch.setattr(
        heartbeat,
        "CommentBudget",
        lambda **kw: CommentBudget(tmp_path / "budget.json", cap=5, dry_run=True),
    )
    monkeypatch.setattr(heartbeat, "THREADS_PATH", threads_path)
    # 停机闸和待审队列也挪进 tmp。不挪的话测试会去读仓库里真实的
    # memory/halt.json——本机停着机，整套测试就会集体"通过"却什么都没跑。
    monkeypatch.setattr(halt, "HALT_PATH", tmp_path / "halt.json")
    monkeypatch.setattr(pending, "PENDING_PATH", tmp_path / "pending.jsonl")
    return client, memory_path, threads_path


def test_dry_run_leaves_both_memory_files_byte_identical(tmp_path, monkeypatch):
    """空跑跑完，盘上必须一个字节都没变。

    线上踩过：空跑不发评论，却照样把 rounds、seen_reply_ids、扣掉的杠力值写了盘。
    结果是那条评论在记忆里算"已经回过了"，之后真跑直接跳过——永远发不出去。

    EN: after a dry run, not one byte on disk may change. Hit in production: the
    dry run sent no comment, yet still persisted rounds, seen_reply_ids and the
    docked gang power. Memory then counted that comment as "already answered",
    so the next live run skipped it — and it never went out at all.
    """
    client, memory_path, threads_path = _wire_cycle(monkeypatch, tmp_path)
    before_memory = memory_path.read_bytes()
    before_threads = threads_path.read_bytes()

    heartbeat.run_cycle(dry_run=True, max_new=1, use_triage=False)

    assert client.posted == []
    assert memory_path.read_bytes() == before_memory
    assert threads_path.read_bytes() == before_threads


def test_live_run_still_writes_both_memory_files(tmp_path, monkeypatch):
    """反面对照：同一套输入真跑就该落盘。

    没有这条，上面那条测试拿一个根本没跑起来的管道也能过。

    EN: the control case — the same inputs on a live run must reach disk.
    Without it, the test above would pass just as happily on a pipeline that
    never ran at all.
    """
    client, memory_path, threads_path = _wire_cycle(monkeypatch, tmp_path)
    before_memory = memory_path.read_bytes()
    before_threads = threads_path.read_bytes()

    heartbeat.run_cycle(dry_run=False, max_new=1, use_triage=False)

    assert client.posted != []
    assert memory_path.read_bytes() != before_memory
    assert threads_path.read_bytes() != before_threads


def test_dry_run_monologue_is_archived_but_marked(tmp_path, monkeypatch):
    """独白照存——那是空跑的主要产出——但要带 dry_run 标记。

    标记是给日报用的：没发出去的评论不能混进战绩统计。

    EN: the monologue is archived either way — it is the main output of a dry
    run — but carries a dry_run marker. The marker exists for the daily report:
    comments that were never sent stay out of the battle stats.
    """
    monkeypatch.setattr(heartbeat, "MONOLOGUE_DIR", tmp_path)

    _real_append_monologue({"kind": "deliberate", "verdict": "出手"}, dry_run=True)
    _real_append_monologue({"kind": "deliberate", "verdict": "出手"})

    lines = [json.loads(x) for x in
             next(tmp_path.glob("*.jsonl")).read_text(encoding="utf-8").splitlines()]
    assert lines[0]["dry_run"] is True
    assert "dry_run" not in lines[1]


# ---------- 计分表：九档里以前只有四档会发生 ----------


def test_opponent_changing_their_mind_is_the_top_score():
    """把人说改口是 §10.1 里分值最高的一档，以前代码里根本产生不出来。"""
    decision = a_decision(has_new_angle=False, opponent_moved=True)
    assert heartbeat._closing_outcome({}, decision, rounds=4) == "对方改口"


def test_conceding_beats_claiming_they_moved():
    """两边都为真时取对自己不利的那个。

    不这样排的话，模型只要顺手把 opponent_moved 报成 true，就能把一次认输
    刷成 +20。自评的分数，规则必须偏向不利于自己的一侧。
    """
    decision = a_decision(has_new_angle=False, conceded=True, opponent_moved=True)
    assert heartbeat._closing_outcome({}, decision, rounds=4) == "我认输"


def test_factual_error_outranks_everything():
    """事实搞错了 -10（§10.2 耻辱柱），认不认账都要付这个代价。"""
    decision = a_decision(has_new_angle=False, conceded=True, factual_error=True)
    assert heartbeat._closing_outcome({}, decision, rounds=6) == "杠错了"


def test_no_new_angle_but_still_attacking_is_stubbornness():
    """自己说没牌了，却还报着一个进攻角度——§6.3 说这就是该停的信号。"""
    decision = a_decision(has_new_angle=False, angle="3.6")
    assert heartbeat._closing_outcome({}, decision, rounds=5) == "硬撑"


def test_graceful_close_is_not_stubbornness():
    """体面收尾（angle=none）不算硬撑，够轮数就是多轮激辩。"""
    decision = a_decision(has_new_angle=False, angle="none")
    assert heartbeat._closing_outcome({}, decision, rounds=3) == "多轮激辩"


def test_multi_round_needs_three_rounds():
    """§10.1 写的是『引发多轮激辩（≥3 轮）』，两轮就散场的不算。

    以前不看轮数一律记 +10，等于把一次两轮的交流当成激辩。
    """
    decision = a_decision(has_new_angle=False, angle="none")
    assert heartbeat._closing_outcome({}, decision, rounds=2) == "一轮即止"


def test_a_mid_thread_concession_survives_to_the_close():
    """对方在中间某轮改了口，收尾时那一轮已经翻篇了，这场依然算改口。

    收尾判定看的是对方**最后**一条回复，只看那一轮的话，整场里含金量最高的
    一次会被丢掉。
    """
    thread = make_thread()
    heartbeat._remember_verdict_flags(thread, a_decision(opponent_moved=True))
    closing = a_decision(has_new_angle=False, angle="none", opponent_moved=False)
    assert heartbeat._closing_outcome(thread, closing, rounds=5) == "对方改口"


def test_the_soft_cap_close_also_uses_the_flags():
    """撞上软保底轮数那条路没有本轮判定，但攒下来的旗标照样算数。"""
    thread = make_thread(factual_error=True)
    assert heartbeat._closing_outcome(thread, None, rounds=8) == "杠错了"


def test_closing_outcome_reaches_memory(tmp_path):
    """端到端：追问收尾时，新判定要真的变成一条对应分值的战绩。"""
    mem = Memory(tmp_path / "mem.json")
    client = FakeClient({"p1": [a_reply()]})
    threads = {"p1": make_thread(rounds=3)}

    heartbeat.follow_up_threads(
        client,
        FakeBrain(a_decision(has_new_angle=False, angle="none", opponent_moved=True)),
        mem,
        threads,
        dry_run=False,
    )

    assert [b["outcome"] for b in mem.data["battles"]] == ["对方改口"]
    assert mem.data["state"]["gang_power"] == 20


# ---------- moderator 警告：唯一该急刹车的信号 ----------


def _warning(**overrides) -> dict:
    item = {"id": "w1", "type": "moderation_warning", "message": "Spam detected"}
    item.update(overrides)
    return item


def test_moderator_warning_halts_the_cycle(tmp_path, monkeypatch):
    """被警告之后一条都不许再发，并且落闸等人来看。"""
    client, memory_path, _ = _wire_cycle(monkeypatch, tmp_path)
    client.inbox = [_warning()]

    assert heartbeat.run_cycle(max_new=1, use_triage=False) is False
    assert client.posted == []
    assert halt.active() is not None
    assert json.loads(memory_path.read_text(encoding="utf-8"))["state"]["gang_power"] == 0


def test_moderator_warning_costs_fifty(tmp_path, monkeypatch):
    """-50 要真的记成一条能被 rebuild / repair 撤销的战绩，不是直接改分数。"""
    client, memory_path, _ = _wire_cycle(monkeypatch, tmp_path)
    client.inbox = [_warning()]
    Memory(memory_path)  # 起点 0 分
    heartbeat.run_cycle(max_new=1, use_triage=False)

    battles = json.loads(memory_path.read_text(encoding="utf-8"))["battles"]
    assert [b["outcome"] for b in battles] == ["被moderator警告"]
    assert battles[0]["score_delta"] == -50
    assert battles[0]["angle_used"] == "none"  # 不能污染任何一招的统计


def test_a_handled_warning_does_not_halt_again(tmp_path, monkeypatch):
    """解完闸之后，同一条通知还躺在收件箱里——不能把自己又关回去。

    没有这层过滤的话，闸永远解不开：主人清一次，下一轮读到同一条又落一次。
    """
    client, _, _ = _wire_cycle(monkeypatch, tmp_path)
    client.inbox = [_warning()]
    heartbeat.run_cycle(max_new=1, use_triage=False)
    halt.clear()

    assert heartbeat.run_cycle(max_new=1, use_triage=False) is True
    assert client.posted != []


def test_a_raised_gate_blocks_the_next_cycle(tmp_path, monkeypatch):
    """闸落着的时候，下一轮连 feed 都不该去拉。"""
    client, _, _ = _wire_cycle(monkeypatch, tmp_path)
    halt.raise_halt("测试用", warning_ids=["w1"])

    assert heartbeat.run_cycle(max_new=1, use_triage=False) is False
    assert client.posted == []


def test_dry_run_does_not_raise_the_gate(tmp_path, monkeypatch):
    """空跑一个字节都不写——闸也一样，它会在下次真跑时自己落下来。"""
    client, _, _ = _wire_cycle(monkeypatch, tmp_path)
    client.inbox = [_warning()]

    assert heartbeat.run_cycle(dry_run=True, max_new=1, use_triage=False) is False
    assert halt.active() is None


# ---------- 自己那条评论的票数 ----------


def test_own_comment_score_is_recorded():
    """评论区本来就整棵拉下来了，自己那条的票数不该再扔掉。"""
    client = FakeClient({"p1": [{"id": "own-1", "author": {"name": "MadTed"},
                                 "content": "这个'一定'的证据是什么？", "score": -12}]})
    threads = {"p1": make_thread()}

    heartbeat.follow_up_threads(client, FakeBrain(None), FakeMemory(), threads, dry_run=False)

    assert threads["p1"]["reactions"] == -12


def test_missing_score_field_is_not_recorded_as_zero():
    """平台没给票数就什么都不记——记成 0 等于编一个『零反响』的事实出来。"""
    client = FakeClient({"p1": [{"id": "own-1", "author": {"name": "MadTed"},
                                 "content": "这个'一定'的证据是什么？"}]})
    threads = {"p1": make_thread()}

    heartbeat.follow_up_threads(client, FakeBrain(None), FakeMemory(), threads, dry_run=False)

    assert "reactions" not in threads["p1"]


def test_downvoted_silence_is_attributed_to_posture():
    """被踩到负分之后没人接话，锅在姿态上，不在角度上（§8.2）。"""
    assert heartbeat._cold_cause(make_thread(reactions=-9)) == "姿态型"


def test_plain_silence_has_no_posture_attribution():
    """没被踩的沉默不该被扣上姿态的帽子。"""
    assert heartbeat._cold_cause(make_thread(reactions=3)) == ""


def test_reactions_reach_the_battle_record(tmp_path):
    mem = Memory(tmp_path / "mem.json")
    heartbeat._close_thread(
        mem, make_thread(reactions=7), "p1", outcome="一轮即止", note=""
    )
    assert mem.data["battles"][0]["reactions"] == 7


# ---------- 排队模式：先攒草稿，等人点头 ----------


def test_queue_mode_posts_nothing_but_files_a_draft(tmp_path, monkeypatch):
    client, _, _ = _wire_cycle(monkeypatch, tmp_path)

    heartbeat.run_cycle(queue_mode=True, max_new=1, use_triage=False)

    assert client.posted == []
    drafts = pending.load()
    assert {d["kind"] for d in drafts} == {"follow_up", "deliberate"}
    assert all(d["reply"] for d in drafts)


def test_queue_mode_leaves_disk_state_untouched(tmp_path, monkeypatch):
    """状态是**推迟**到批准那一刻，不是当场落——这一轮盘上不该有任何变化。"""
    _, memory_path, threads_path = _wire_cycle(monkeypatch, tmp_path)
    before_memory = memory_path.read_bytes()
    before_threads = threads_path.read_bytes()

    heartbeat.run_cycle(queue_mode=True, max_new=1, use_triage=False)

    assert memory_path.read_bytes() == before_memory
    assert threads_path.read_bytes() == before_threads


def test_a_queued_post_is_not_deliberated_again(tmp_path, monkeypatch):
    """草稿还没批，下一轮不能把同一个帖子再深挖一遍。

    L2 是最贵的一层，重复深挖既费钱，批准时还会对同一个帖子连发两条。
    """
    _, _, _ = _wire_cycle(monkeypatch, tmp_path)
    heartbeat.run_cycle(queue_mode=True, max_new=1, use_triage=False)
    first = len(pending.load())

    heartbeat.run_cycle(queue_mode=True, max_new=1, use_triage=False)

    assert len(pending.load()) == first


def test_queued_monologue_is_marked_pending(tmp_path, monkeypatch):
    """排队中的独白要打标记，否则战报会把还没发出去的当成战绩。"""
    monkeypatch.setattr(heartbeat, "_append_monologue", _real_append_monologue)
    monkeypatch.setattr(heartbeat, "MONOLOGUE_DIR", tmp_path / "monologue")
    _wire_cycle(monkeypatch, tmp_path)

    heartbeat.run_cycle(queue_mode=True, max_new=1, use_triage=False)

    lines = (tmp_path / "monologue").glob("*.jsonl")
    entries = [json.loads(l) for f in lines for l in f.read_text(encoding="utf-8").splitlines()]
    assert entries and all(e["pending"] is True for e in entries)
