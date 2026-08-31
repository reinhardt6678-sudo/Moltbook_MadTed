"""待审队列 + 停机闸的单元测试。

这两样东西的共同点是：**它们错在保守的一侧代价很小，错在放行的一侧代价很大**
（多按住一轮 vs 把不该发的发出去、顶着 moderator 警告继续发）。测试也是按这个
方向写的——重点验的是"该拦的拦住了"，而不是"该放的放过了"。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import approve  # noqa: E402
import halt  # noqa: E402
import heartbeat  # noqa: E402
import pending  # noqa: E402
from budget import CommentBudget  # noqa: E402
from memory import Memory  # noqa: E402
from moltbook_client import is_moderator_warning, notification_id  # noqa: E402


@pytest.fixture(autouse=True)
def no_real_monologue(tmp_path, monkeypatch):
    """独白存档指到 tmp。

    approve._send() 会往存档里补一条"这条真发出去了"的记录，而那条是**不带
    标记**的——战报正是靠没有标记来数真实战绩。不隔离的话，跑一次测试就等于
    往真实战报里塞几条凭空捏造的出手。
    """
    monkeypatch.setattr(heartbeat, "MONOLOGUE_DIR", tmp_path / "monologue")


@pytest.fixture
def queue_path(tmp_path, monkeypatch):
    path = tmp_path / "pending.jsonl"
    monkeypatch.setattr(pending, "PENDING_PATH", path)
    return path


@pytest.fixture
def gate_path(tmp_path, monkeypatch):
    path = tmp_path / "halt.json"
    monkeypatch.setattr(halt, "HALT_PATH", path)
    return path


class FakeClient:
    def __init__(self, *, fail=False):
        self.posted: list[tuple[str, str]] = []
        self.marked_read: list[str] = []
        self.fail = fail

    def create_comment(self, post_id, content):
        if self.fail:
            from moltbook_client import MoltbookError

            raise MoltbookError("boom", 500, "")
        self.posted.append((post_id, content))
        return {"id": f"own-{len(self.posted)}"}

    def mark_post_read(self, post_id):
        self.marked_read.append(post_id)


def a_follow_up_draft(**overrides) -> dict:
    draft = {
        "id": "d1",
        "kind": "follow_up",
        "post_id": "p1",
        "title": "AI 一定会取代程序员",
        "opponent": "hypebot",
        "angle": "3.6",
        "thinking": "他偷换了前提",
        "reply": "你这是双标。",
        "commit": {
            "opponent_turns": [{"role": "opponent", "author": "hypebot", "text": "证据就是趋势"}],
            "seen_reply_ids": ["r1"],
            "reply_language": "zh",
            "angle": "3.6",
            "verdict_flags": [],
            "close": None,
        },
    }
    draft.update(overrides)
    return draft


def a_thread() -> dict:
    return {
        "title": "AI 一定会取代程序员",
        "opponent": "hypebot",
        "topic_type": "绝对化断言",
        "rounds": 1,
        "used_angles": ["3.4"],
        "turns": [{"role": "self", "text": "证据是什么？"}],
        "seen_reply_ids": [],
        "own_comment_ids": ["own-1"],
        "idle_cycles": 3,
        "post_language": "zh",
        "reply_language": "zh",
        "signals": {},
        "closed": False,
    }


# ---------- 队列本身 ----------


def test_queue_round_trips(queue_path):
    pending.append(a_follow_up_draft())
    pending.append(a_follow_up_draft(id="d2", post_id="p2"))
    assert [d["id"] for d in pending.load()] == ["d1", "d2"]
    assert pending.post_ids() == {"p1", "p2"}


def test_a_broken_line_does_not_take_the_queue_down(queue_path):
    """一行写坏了只丢那一行。整个队列一起废掉的话，好草稿也跟着没了。"""
    pending.append(a_follow_up_draft())
    with open(queue_path, "a", encoding="utf-8") as fh:
        fh.write("{这不是 json\n")
    pending.append(a_follow_up_draft(id="d3", post_id="p3"))

    assert [d["id"] for d in pending.load()] == ["d1", "d3"]


def test_emptying_the_queue_removes_the_file(queue_path):
    """留个空文件会让 post_ids() 那边误以为还有东西排着。"""
    pending.append(a_follow_up_draft())
    pending.save([])
    assert not queue_path.exists()
    assert pending.post_ids() == set()


# ---------- 批准之后落的那份状态 ----------


def test_approving_a_follow_up_advances_the_thread(tmp_path, queue_path):
    threads = {"p1": a_thread()}
    mem = Memory(tmp_path / "mem.json")
    client = FakeClient()

    assert approve._send(
        a_follow_up_draft(), client, threads, mem, _budget(tmp_path)
    ) is True

    thread = threads["p1"]
    assert client.posted == [("p1", "你这是双标。")]
    assert thread["rounds"] == 2
    assert thread["seen_reply_ids"] == ["r1"]
    assert thread["used_angles"] == ["3.4", "3.6"]
    assert thread["idle_cycles"] == 0
    assert thread["turns"][-2:] == [
        {"role": "opponent", "author": "hypebot", "text": "证据就是趋势"},
        {"role": "self", "text": "你这是双标。"},
    ]


def test_approving_records_the_new_comment_id(tmp_path, queue_path):
    """认不出自己发过哪条评论，下一轮就判不出谁在回我。"""
    threads = {"p1": a_thread()}
    approve._send(
        a_follow_up_draft(), FakeClient(), threads, Memory(tmp_path / "mem.json"), _budget(tmp_path)
    )
    assert "own-1" in threads["p1"]["own_comment_ids"]


def test_approving_a_closing_follow_up_records_the_battle(tmp_path, queue_path):
    mem = Memory(tmp_path / "mem.json")
    threads = {"p1": a_thread()}
    draft = a_follow_up_draft()
    draft["commit"]["close"] = {"outcome": "对方改口", "note": "他收窄了论点"}

    approve._send(draft, FakeClient(), threads, mem, _budget(tmp_path))

    assert [b["outcome"] for b in mem.data["battles"]] == ["对方改口"]
    assert mem.data["state"]["gang_power"] == 20
    assert threads["p1"]["closed"] is True


def test_a_failed_send_changes_nothing(tmp_path, queue_path):
    """发失败了就什么都不能落——否则这条串会以为自己已经回过了。"""
    mem = Memory(tmp_path / "mem.json")
    threads = {"p1": a_thread()}
    budget = _budget(tmp_path)

    assert approve._send(a_follow_up_draft(), FakeClient(fail=True), threads, mem, budget) is False
    assert threads["p1"]["rounds"] == 1
    assert mem.data["battles"] == []
    assert budget.remaining == 5


def test_approving_a_new_battle_installs_the_thread(tmp_path, queue_path):
    threads: dict = {}
    draft = {
        "id": "d9",
        "kind": "deliberate",
        "post_id": "p9",
        "title": "Agents will replace QA",
        "opponent": "hypebot",
        "angle": "3.2",
        "reply": "Which teams?",
        "commit": {"thread": a_thread()},
    }
    approve._send(draft, FakeClient(), threads, Memory(tmp_path / "mem.json"), _budget(tmp_path))

    assert threads["p9"]["own_comment_ids"] == ["own-1"]
    assert threads["p9"]["opened_at"]


def test_a_stale_draft_does_not_clobber_a_live_thread(tmp_path, queue_path):
    """审批可能发生在几小时后，那时这条串在盘上早就动过了。

    整份快照盖回去会把中间发生的事情抹掉，所以新杠那条路遇到已存在的串直接不动。
    """
    live = a_thread()
    live["rounds"] = 5
    threads = {"p9": live}
    draft = {
        "id": "d9",
        "kind": "deliberate",
        "post_id": "p9",
        "reply": "Which teams?",
        "commit": {"thread": a_thread()},
    }
    approve._send(draft, FakeClient(), threads, Memory(tmp_path / "mem.json"), _budget(tmp_path))

    assert threads["p9"]["rounds"] == 5


def test_a_vanished_thread_does_not_crash_the_approval(tmp_path, queue_path):
    """串已经被收尾清掉了，评论还是要发出去，只是没有状态可落。"""
    client = FakeClient()
    threads: dict = {}
    assert approve._send(
        a_follow_up_draft(), client, threads, Memory(tmp_path / "mem.json"), _budget(tmp_path)
    ) is True
    assert client.posted == [("p1", "你这是双标。")]


def _budget(tmp_path) -> CommentBudget:
    return CommentBudget(tmp_path / "budget.json", cap=5)


# ---------- 停机闸 ----------


def test_gate_round_trips(gate_path):
    assert halt.active() is None
    halt.raise_halt("被警告了", warning_ids=["w1"])
    state = halt.active()
    assert state["warning_ids"] == ["w1"]
    assert halt.clear() is True
    assert halt.active() is None


def test_a_corrupt_gate_file_still_counts_as_halted(gate_path):
    """闸的默认方向必须是拦住：读不出内容 = 盘上状态不可知 = 不许发。"""
    gate_path.write_text("{坏掉的", encoding="utf-8")
    assert halt.active() is not None


def test_dry_run_never_writes_the_gate(gate_path):
    halt.raise_halt("空跑", dry_run=True)
    assert not gate_path.exists()


# ---------- 警告识别 ----------


@pytest.mark.parametrize(
    "notification",
    [
        {"type": "moderation_warning"},
        {"kind": "content_removed"},
        {"reason": "spam"},
        {"author": {"name": "Clawd Clawderberg"}, "message": "cut it out"},
    ],
)
def test_moderator_notifications_are_recognised(notification):
    assert is_moderator_warning(notification) is True


@pytest.mark.parametrize(
    "notification",
    [
        {"type": "comment_reply", "author": {"name": "hypebot"}},
        {"type": "mention", "message": "@MadTed 你怎么看"},
        {},
    ],
)
def test_ordinary_notifications_are_left_alone(notification):
    assert is_moderator_warning(notification) is False


def test_a_notification_without_an_id_still_gets_one():
    """没有 id 就按正文摘要认。整条丢掉的话，警告等于没看见。"""
    first = notification_id({"message": "Spam detected"})
    assert first.startswith("digest:")
    assert first == notification_id({"message": "Spam detected"})
    assert first != notification_id({"message": "Something else"})


def test_a_warning_is_only_charged_once(tmp_path):
    mem = Memory(tmp_path / "mem.json")
    assert mem.record_warning("w1", source="Clawd", note="spam") == -50
    assert mem.record_warning("w1", source="Clawd", note="spam") == 0
    assert mem.data["state"]["gang_power"] == 0  # 分数不会被扣到负数
    assert len(mem.data["battles"]) == 1
