"""维护脚本的判据测试——纯逻辑，不碰真实记忆文件。

这些判据是"删哪些战绩"的唯一依据，删错了就是把真账一起抹掉，
所以边界（备注不符、日期在 --before 之后、别的 outcome）要钉住。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import repair_memory  # noqa: E402


def a_battle(**overrides) -> dict:
    battle = {
        "date": "2026-08-17",
        "post_id": "p1",
        "opponent": "vina",
        "angle_used": "3.2",
        "rounds": 2,
        "outcome": "一轮即止",
        "note": "对方停止回应，收尾",
    }
    battle.update(overrides)
    return battle


def test_blind_reader_verdict_is_recognised():
    assert repair_memory._is_blind_reader_verdict(a_battle(), None) is True


def test_other_outcomes_are_left_alone():
    """只认自动判出来的那一类，手写备注和别的结局不能误伤。"""
    assert repair_memory._is_blind_reader_verdict(a_battle(outcome="我认输"), None) is False
    assert repair_memory._is_blind_reader_verdict(
        a_battle(note="角度用完了，体面收尾"), None
    ) is False


def test_before_protects_records_written_after_the_fix():
    """管道修好之后，「对方停止回应」是有观测基础的判断，不该跟着老账一起删。"""
    assert repair_memory._is_blind_reader_verdict(a_battle(date="2026-08-19"), "2026-08-19") is False
    assert repair_memory._is_blind_reader_verdict(a_battle(date="2026-08-17"), "2026-08-19") is True


def test_phantom_covers_both_bugs():
    """两次犯的是同一个毛病，一次跑完收拾干净。"""
    cold = a_battle(outcome="冷场", note="连续多个周期零回应")
    assert repair_memory._is_phantom(cold, None) is True
    assert repair_memory._is_phantom(a_battle(), None) is True
    assert repair_memory._is_phantom(a_battle(outcome="多轮激辩"), None) is False


def test_prune_only_touches_open_threads():
    """已经收尾的串留着当历史，只有还会被喂给大脑的才需要清干净。"""
    threads = {
        "open": {
            "closed": False,
            "turns": [
                {"role": "self", "text": "我说的"},
                {"role": "opponent", "author": "路人甲", "text": "旁人在楼里聊别的"},
            ],
            "seen_reply_ids": ["b1"],
        },
        "closed": {
            "closed": True,
            "turns": [
                {"role": "self", "text": "我说的"},
                {"role": "opponent", "author": "路人乙", "text": "旁人"},
            ],
            "seen_reply_ids": ["b2"],
        },
    }

    pruned = repair_memory.prune_bystander_turns(threads)

    assert pruned == [("open", 1)]
    assert threads["open"]["turns"] == [{"role": "self", "text": "我说的"}]
    assert threads["open"]["seen_reply_ids"] == []
    assert len(threads["closed"]["turns"]) == 2
    assert threads["closed"]["seen_reply_ids"] == ["b2"]


def test_prune_is_a_noop_when_nothing_to_clean():
    threads = {"open": {"closed": False, "turns": [{"role": "self", "text": "我说的"}]}}
    assert repair_memory.prune_bystander_turns(threads) == []
