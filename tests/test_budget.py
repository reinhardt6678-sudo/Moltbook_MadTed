"""滚动 24 小时评论额度的测试。

这块的存在理由是 cron：`moltbook_client._Throttle` 用 `time.monotonic()` 记冷却，
每小时新起一个进程就全忘了。所以下面最要紧的一条是
`test_budget_survives_a_new_process`——额度必须靠文件跨进程活着，
不然"每小时一轮"等于"每天可以发 24 × max_new 条"。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from budget import DEFAULT_DAILY_COMMENTS, CommentBudget  # noqa: E402


def _stamps_ago(path: Path, *hours_ago: float) -> None:
    now = datetime.now(timezone.utc)
    path.write_text(
        json.dumps(
            {
                "spent_at": [
                    (now - timedelta(hours=h)).isoformat() for h in hours_ago
                ]
            }
        ),
        encoding="utf-8",
    )


# ---------- 基本计数 ----------


def test_fresh_budget_is_full(tmp_path):
    budget = CommentBudget(tmp_path / "b.json", cap=5)
    assert budget.used == 0
    assert budget.remaining == 5


def test_spend_decrements_remaining(tmp_path):
    budget = CommentBudget(tmp_path / "b.json", cap=3)
    budget.spend()
    budget.spend()
    assert budget.used == 2
    assert budget.remaining == 1


def test_remaining_never_goes_negative(tmp_path):
    budget = CommentBudget(tmp_path / "b.json", cap=1)
    budget.spend(3)
    assert budget.remaining == 0


# ---------- 跨进程 ----------


def test_budget_survives_a_new_process(tmp_path):
    """cron 每轮都是新进程，额度只能靠文件记住。"""
    path = tmp_path / "b.json"
    first = CommentBudget(path, cap=10)
    first.spend()
    first.spend()

    # 模拟下一个小时 cron 拉起的那个进程：全新对象，只有文件是共享的
    second = CommentBudget(path, cap=10)
    assert second.used == 2
    assert second.remaining == 8


def test_spend_is_persisted_immediately(tmp_path):
    """进程被杀也不能丢账——每条发出去就落盘，不攒到最后。"""
    path = tmp_path / "b.json"
    budget = CommentBudget(path, cap=10)
    budget.spend()

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert len(on_disk["spent_at"]) == 1


# ---------- 滚动窗口 ----------


def test_entries_older_than_24h_fall_off(tmp_path):
    path = tmp_path / "b.json"
    _stamps_ago(path, 30, 25, 23, 1)  # 前两条已经出窗口

    budget = CommentBudget(path, cap=10)
    assert budget.used == 2


def test_exhausted_budget_reports_when_a_slot_returns(tmp_path):
    path = tmp_path / "b.json"
    _stamps_ago(path, 23.5, 2)  # 最老的那条 30 分钟后出窗口

    budget = CommentBudget(path, cap=2)
    assert budget.remaining == 0
    wait = budget.next_slot_in()
    assert wait is not None
    assert 25 * 60 < wait.total_seconds() < 35 * 60


def test_next_slot_is_none_when_budget_is_available(tmp_path):
    budget = CommentBudget(tmp_path / "b.json", cap=5)
    budget.spend()
    assert budget.next_slot_in() is None


# ---------- 坏文件 / 配置 ----------


def test_corrupt_file_is_treated_as_exhausted_not_fresh(tmp_path):
    """读不了额度文件时必须按已用满处理。

    按"全新"处理的话，只要文件坏了就等于白送一整天额度——而这正是最该保守的时候。
    """
    path = tmp_path / "b.json"
    path.write_text("{ 这不是 json", encoding="utf-8")

    budget = CommentBudget(path, cap=10)
    assert budget.remaining == 0


def test_naive_timestamps_from_hand_editing_are_tolerated(tmp_path):
    path = tmp_path / "b.json"
    naive = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(tzinfo=None)
    path.write_text(json.dumps({"spent_at": [naive.isoformat()]}), encoding="utf-8")

    assert CommentBudget(path, cap=10).used == 1


def test_env_var_sets_the_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("MADTED_DAILY_COMMENTS", "7")
    assert CommentBudget(tmp_path / "b.json").cap == 7


def test_garbage_env_var_falls_back_to_default(tmp_path, monkeypatch):
    monkeypatch.setenv("MADTED_DAILY_COMMENTS", "很多")
    assert CommentBudget(tmp_path / "b.json").cap == DEFAULT_DAILY_COMMENTS


def test_dry_run_counts_in_memory_but_writes_nothing(tmp_path):
    """空跑要能跑出"额度用完就停"的行为，但不能污染真实额度。"""
    path = tmp_path / "b.json"
    budget = CommentBudget(path, cap=2, dry_run=True)
    budget.spend()
    budget.spend()

    assert budget.remaining == 0       # 空跑内部照样会被额度卡住
    assert not path.exists()           # 但没在真实账本上记一笔


def test_zero_cap_does_not_crash_on_summary(tmp_path):
    """上限 0（--daily-comments 0 / 手工封停）时不该炸——没记录可算"下一个额度"。"""
    budget = CommentBudget(tmp_path / "b.json", cap=0)
    assert budget.remaining == 0
    assert budget.next_slot_in() is None
    assert "0" in budget.summary()


def test_24_hourly_runs_cannot_exceed_the_cap(tmp_path):
    """整件事的验收条件：每小时一个新进程，一天下来也超不过上限。

    没有落盘的话这里会是 24 × 3 = 72 条——正好越过平台 50 条/天的线。
    """
    path = tmp_path / "b.json"
    sent = 0

    for _ in range(24):                       # 模拟 cron 拉起的 24 个独立进程
        budget = CommentBudget(path, cap=40)
        for _ in range(3):                    # 每轮最多想发 3 条
            if budget.remaining <= 0:
                break
            budget.spend()
            sent += 1

    assert sent == 40
