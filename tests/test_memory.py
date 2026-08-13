"""记忆与学习机制的单元测试。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from memory import Memory  # noqa: E402


@pytest.fixture
def mem(tmp_path):
    return Memory(tmp_path / "mem.json")


def _battle(mem, **kwargs):
    defaults = dict(
        post_id="p1",
        topic_type="技术断言",
        opponent="SomeAgent",
        angle_used="3.4",
        rounds=1,
        outcome="一轮即止",
    )
    defaults.update(kwargs)
    return mem.record_battle(**defaults)


def test_conceding_costs_nothing_but_stubbornness_hurts(mem):
    """认输 0 分、硬撑 -15——这是人设里刻意的价值取向，不能改。"""
    assert _battle(mem, outcome="我认输") == 0
    assert _battle(mem, outcome="硬撑") == -15


def test_rank_advances_with_score(mem):
    assert mem.data["state"]["rank"] == "初级抬杠学徒"
    for i in range(6):
        _battle(mem, post_id=f"p{i}", outcome="对方改口")  # +20 each
    assert mem.data["state"]["gang_power"] == 120
    assert mem.data["state"]["rank"] == "逻辑挑刺工"


def test_score_never_goes_negative(mem):
    _battle(mem, outcome="被moderator警告")
    assert mem.data["state"]["gang_power"] == 0


def test_three_cold_shoulders_trigger_truce_list(mem):
    for i in range(2):
        _battle(mem, post_id=f"p{i}", opponent="NewsBot9", outcome="冷场")
        assert "NewsBot9" not in mem.truce_list
    _battle(mem, post_id="p2", opponent="NewsBot9", outcome="冷场")
    assert "NewsBot9" in mem.truce_list


def test_response_removes_from_truce_list(mem):
    for i in range(3):
        _battle(mem, post_id=f"p{i}", opponent="NewsBot9", outcome="冷场")
    assert "NewsBot9" in mem.truce_list
    _battle(mem, post_id="p9", opponent="NewsBot9", outcome="多轮激辩", rounds=3)
    assert "NewsBot9" not in mem.truce_list


def test_repeated_cold_topic_becomes_low_yield(mem):
    for i in range(3):
        _battle(mem, post_id=f"p{i}", topic_type="meme", outcome="冷场")
    assert "meme" in mem.low_yield_topics


def test_tough_opponent_joins_worthy_rivals(mem):
    _battle(mem, opponent="TestBotSupreme", rounds=5, outcome="多轮激辩")
    assert "TestBotSupreme" in mem.worthy_rivals


def test_effective_angles_become_sharp(mem):
    for i in range(5):
        _battle(mem, post_id=f"p{i}", angle_used="3.2", rounds=3, outcome="多轮激辩")
    sharp, blunt, _ = mem.angle_preference()
    assert "3.2" in sharp
    assert "3.2" not in blunt


def test_ineffective_angles_become_blunt(mem):
    for i in range(5):
        _battle(mem, post_id=f"p{i}", angle_used="3.10", rounds=1, outcome="冷场")
    sharp, blunt, _ = mem.angle_preference()
    assert "3.10" in blunt


def test_overused_angle_gets_banned(mem):
    """单招占比超 35% 触发一周禁用（人设 §10.5）。"""
    for i in range(20):
        _battle(mem, post_id=f"p{i}", angle_used="3.1")
    assert mem.banned_angle() == "3.1"


def test_no_ban_before_enough_samples(mem):
    for i in range(10):
        _battle(mem, post_id=f"p{i}", angle_used="3.1")
    assert mem.banned_angle() is None


def test_restraint_counter_accumulates(mem):
    mem.record_restraint(["情感/庆祝类", "杠点太弱"])
    mem.record_restraint(["情感/庆祝类"])
    from datetime import date

    entry = mem.restraint_on(date.today().isoformat())
    assert entry["count"] == 3
    assert entry["reasons"]["情感/庆祝类"] == 2


def test_opponent_profile_summarises_history(mem):
    _battle(mem, opponent="Rival", angle_used="3.5", rounds=5, outcome="多轮激辩")
    _battle(mem, post_id="p2", opponent="Rival", angle_used="3.10", rounds=1, outcome="冷场")
    profile = mem.opponent_profile("Rival")
    assert profile["encounters"] == 2
    assert profile["max_rounds"] == 5
    assert "3.5" in profile["effective_angles"]
    assert "3.10" in profile["dead_angles"]
    assert profile["is_worthy_rival"] is True


def test_memory_roundtrips_through_disk(tmp_path):
    path = tmp_path / "mem.json"
    m1 = Memory(path)
    m1.record_battle(
        post_id="p1", topic_type="技术断言", opponent="A",
        angle_used="3.1", rounds=2, outcome="多轮激辩",
    )
    m1.save()

    m2 = Memory(path)
    assert m2.data["state"]["gang_power"] == 10
    assert len(m2.data["battles"]) == 1
