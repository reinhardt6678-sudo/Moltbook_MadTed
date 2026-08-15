"""跟进阶段的测试——纯逻辑，不发真实请求、不调 Claude。

这块以前一行测试都没有，而"看不见回复→记成冷场"的 bug 正好长在这里：
线上表现是日报里一串冷场，登进网页却看得见别人的回复。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import heartbeat  # noqa: E402
from brain import FollowUp  # noqa: E402
from budget import CommentBudget  # noqa: E402
from moltbook_client import (  # noqa: E402
    MoltbookError,
    author_name,
    comment_id,
    notification_post_id,
    parent_comment_id,
)


class FakeClient:
    """假客户端：可以指定评论区内容，以及通知端点是否可用。"""

    def __init__(self, replies=None, *, notifications_fail=False, replies_fail=False):
        self._replies = replies or {}
        self.notifications_fail = notifications_fail
        self.replies_fail = replies_fail
        self.posted: list[tuple[str, str]] = []
        self.marked_read: list[str] = []

    def get_agent_status(self):
        return {"name": "MadTed"}

    def get_inbox_activity(self):
        if self.notifications_fail:
            raise MoltbookError("GET /notifications 失败: 404", 404, "")
        return []

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

    def __init__(self, decision: FollowUp | None):
        self.decision = decision
        self.calls: list[str] = []

    def follow_up(self, transcript, used_angles, **kwargs):
        self.calls.append(transcript)
        return self.decision


class FakeMemory:
    def __init__(self):
        self.battles: list[dict] = []

    def angle_preference(self):
        return ([], [], None)

    def opponent_profile(self, name):
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


def a_decision(**overrides) -> FollowUp:
    payload = {
        "has_new_angle": True,
        "thinking": "他偷换了前提",
        "conceded": False,
        "angle": "3.6",
        "reply": "你这是双标。",
    }
    payload.update(overrides)
    return FollowUp(**payload)


@pytest.fixture(autouse=True)
def no_disk_writes(monkeypatch):
    """独白日志写盘和测试无关，挡掉。"""
    monkeypatch.setattr(heartbeat, "_append_monologue", lambda entry: None)


# ---------- 核心回归：发现回复不依赖通知 ----------


def test_finds_replies_when_notifications_endpoint_is_dead():
    """通知端点挂了也要照常发现回复。

    这就是线上那个 bug：以前通知是唯一入口，它一 404，
    满帖子的回复一条都看不见，最后全被记成冷场。
    """
    client = FakeClient(
        {"p1": [{"id": "r1", "author": {"name": "hypebot"}, "content": "证据就是趋势"}]},
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
                {"id": "r1", "author": {"name": "hypebot"}, "content": "对方回的"},
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
        {"p1": [{"id": "r1", "author": {"name": "hypebot"}, "content": "回了"}]}
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
        {"p1": [{"id": "r1", "author": {"name": "hypebot"}, "content": "接话"}]}
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
        {"p1": [{"id": "r1", "author": {"name": "hypebot"}, "content": "回了"}]}
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
        {"p1": [{"id": "r1", "author": {"name": "hypebot"}, "content": "接话"}]}
    )
    mem = FakeMemory()
    threads = {"p1": make_thread(last_activity_at=_hours_ago(20))}

    heartbeat.follow_up_threads(client, FakeBrain(a_decision()), mem, threads, dry_run=False)

    assert heartbeat._quiet_hours(threads["p1"]) < 1


# ---------- 评论额度 ----------


def test_follow_up_stops_when_budget_is_gone():
    """额度用完时不追问，也不能把这些回复吞掉——下轮还要接着答。"""
    client = FakeClient(
        {"p1": [{"id": "r1", "author": {"name": "hypebot"}, "content": "接话"}]}
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
        {"p1": [{"id": "r1", "author": {"name": "hypebot"}, "content": "接话"}]}
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
