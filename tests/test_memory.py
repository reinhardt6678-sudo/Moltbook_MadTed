"""记忆与学习机制的单元测试。"""

from __future__ import annotations

import json
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


# ---------------------------------------------------------------------------
# 冷场归因：语言不通是无效样本（人设 §8.2）
# ---------------------------------------------------------------------------


def test_language_mismatch_cold_shoulder_does_not_poison_angle_stats(mem):
    """用错语言导致的冷场，说明不了角度好坏。

    没有这层排除，一次语言事故就会把一个好角度记进「钝刀」——
    而 §8.2 的归因表里原本根本没有"对方看不懂"这一类，
    锅只能扣在角度、话题或对手头上，越学越偏。
    """
    for _ in range(5):
        _battle(mem, outcome="冷场", cold_cause="语言不通", angle_used="3.2")

    assert mem.data["angle_stats"] == {}
    assert mem.data["lists"]["blunt_angles"] == []


def test_language_mismatch_cold_shoulder_does_not_blacklist_opponent(mem):
    for _ in range(3):
        _battle(mem, outcome="冷场", cold_cause="语言不通", opponent="EnglishBot")

    assert "EnglishBot" not in mem.truce_list


def test_language_mismatch_still_costs_score_and_is_archived(mem):
    """不参与学习不等于不认账——分照扣，战绩照留，方便复盘。"""
    assert _battle(mem, outcome="冷场", cold_cause="语言不通") == -2
    battle = mem.data["battles"][-1]
    assert battle["cold_cause"] == "语言不通"
    assert "语言不通" in battle["note"]


def test_ordinary_cold_shoulder_still_teaches(mem):
    """普通冷场的归因逻辑不能被新分类改坏。"""
    for _ in range(3):
        _battle(mem, outcome="冷场", opponent="NewsBot9")

    assert "NewsBot9" in mem.truce_list


# ---------------------------------------------------------------------------
# 结构信号学习（跨语言可迁移）
# ---------------------------------------------------------------------------


def test_signal_stats_accumulate_from_battles(mem):
    _battle(mem, rounds=4, outcome="多轮激辩", signals={"features": ["裸数据", "一边倒"]})
    _battle(mem, rounds=1, outcome="冷场", signals={"features": ["裸数据"]})

    stats = mem.data["signal_stats"]
    assert stats["裸数据"] == {"seen": 2, "engaged": 1}
    assert stats["一边倒"] == {"seen": 1, "engaged": 1}


def test_signal_bias_needs_enough_samples(mem):
    for _ in range(3):
        _battle(mem, rounds=4, outcome="多轮激辩", signals={"features": ["裸数据"]})

    assert mem.signal_bias() == {}


def test_signal_bias_rewards_productive_structures(mem):
    for _ in range(8):
        _battle(mem, rounds=4, outcome="多轮激辩", signals={"features": ["裸数据"]})
    for _ in range(8):
        _battle(mem, rounds=1, outcome="冷场", signals={"features": ["已在吵"]})

    bias = mem.signal_bias()
    assert bias["裸数据"] > 1.0
    assert bias["已在吵"] < 1.0
    assert all(0.6 <= v <= 1.4 for v in bias.values())


def test_language_mismatch_battles_are_excluded_from_signal_stats(mem):
    for _ in range(6):
        _battle(mem, outcome="冷场", cold_cause="语言不通", signals={"features": ["裸数据"]})

    assert mem.data["signal_stats"] == {}


def test_signal_stats_survive_a_roundtrip(tmp_path):
    path = tmp_path / "mem.json"
    first = Memory(path)
    for _ in range(6):
        first.record_battle(
            post_id="p1",
            topic_type="技术断言",
            opponent="A",
            angle_used="3.4",
            rounds=4,
            outcome="多轮激辩",
            signals={"features": ["裸数据"]},
        )
    first.save()

    assert Memory(path).signal_bias()["裸数据"] > 1.0


def test_old_memory_files_without_signal_stats_still_load(tmp_path):
    """手工编辑过的旧文件不能因为缺字段就炸掉。"""
    path = tmp_path / "mem.json"
    path.write_text('{"state": {"gang_power": 42}, "battles": []}', encoding="utf-8")

    mem = Memory(path)
    assert mem.data["signal_stats"] == {}
    assert mem.signal_bias() == {}


# ---------- 派生状态重算（修假冷场用） ----------


def test_rebuild_reproduces_the_same_state(mem):
    """重算不该改变任何东西——战绩没变，派生结论就该一模一样。"""
    for i in range(5):
        _battle(mem, post_id=f"p{i}", outcome="多轮激辩", rounds=3)
    for i in range(3):
        _battle(mem, post_id=f"c{i}", opponent="Ghost", outcome="冷场")

    before = json.loads(json.dumps(mem.data))
    mem.rebuild_derived_state()

    assert mem.data == before


def test_dropping_phantom_cold_records_frees_the_opponent(mem):
    """假冷场删掉之后，被冤枉进免战名单的对手要放出来。"""
    _battle(mem, post_id="win", outcome="对方改口", rounds=4)  # +20，垫高才看得出扣分
    for i in range(3):
        _battle(mem, post_id=f"c{i}", opponent="Ghost", outcome="冷场")
    assert "Ghost" in mem.data["lists"]["truce_list"]
    assert mem.data["state"]["gang_power"] == 14  # 20 - 2*3

    dropped = mem.drop_battles(lambda b: b["outcome"] == "冷场")

    assert len(dropped) == 3
    assert mem.data["lists"]["truce_list"] == []
    assert [b["post_id"] for b in mem.data["battles"]] == ["win"]
    # -2 分一条也要退回去
    assert mem.data["state"]["gang_power"] == 20


def test_dropping_phantom_cold_records_rehabilitates_the_angle(mem):
    """被假冷场打成『钝刀』的角度，删干净之后要回到利刃。"""
    for i in range(4):
        _battle(mem, post_id=f"c{i}", angle_used="3.6", outcome="冷场")
    assert "3.6" in mem.data["lists"]["blunt_angles"]

    for i in range(4):
        _battle(mem, post_id=f"w{i}", angle_used="3.6", outcome="多轮激辩", rounds=3)
    mem.drop_battles(lambda b: b["outcome"] == "冷场")

    assert mem.data["lists"]["blunt_angles"] == []
    assert "3.6" in mem.data["lists"]["sharp_angles"]


def test_drop_keeps_unrelated_battles(mem):
    _battle(mem, post_id="keep", outcome="对方改口", rounds=4)
    _battle(mem, post_id="drop", outcome="冷场")

    mem.drop_battles(lambda b: b["outcome"] == "冷场")

    assert [b["post_id"] for b in mem.data["battles"]] == ["keep"]
    assert mem.data["state"]["gang_power"] == 20


# ---------- 复合角度的账目 ----------


def test_composite_angle_counts_toward_each_move(mem):
    """一次报两招时，两招各记一次——整串当一个 key 会多出个谁也不认识的桶。"""
    _battle(mem, angle_used="3.10+3.4")

    assert set(mem.data["angle_stats"]) == {"3.10", "3.4"}
    assert mem.data["angle_stats"]["3.10"]["used"] == 1
    assert mem.data["angle_stats"]["3.4"]["used"] == 1


def test_composite_angle_does_not_dilute_the_ban_math(mem):
    """热力图的 35% 是靠 angle_stats 的分母算的，复合桶会把两招的占比都稀释掉。

    20 场里 3.4 出手 8 次（40%），其中 1 次是和 3.10 一起报的。
    按整串记的话 3.4 只剩 7/20 = 35%，压线不触发，禁用令就晚来一周。
    """
    for i in range(7):
        _battle(mem, post_id=f"a{i}", angle_used="3.4")
    _battle(mem, post_id="a7", angle_used="3.10+3.4")
    for i in range(12):
        _battle(mem, post_id=f"b{i}", angle_used="3.2")

    assert mem.data["angle_stats"]["3.4"]["used"] == 8
    assert mem.data["angle_ban"]["banned"] == "3.4"


def test_none_is_not_a_move(mem):
    """'none' 是"这轮没用招"，不该在分母里占位。"""
    _battle(mem, angle_used="none")

    assert mem.data["angle_stats"] == {}


def test_opponent_dossier_lists_single_moves(mem):
    """档案是喂给大脑的，"3.10+3.4"这种串会让它以为有这么一招。"""
    _battle(mem, opponent="vina", angle_used="3.10+3.4", rounds=3)

    profile = mem.opponent_profile("vina")

    assert sorted(profile["effective_angles"]) == ["3.10", "3.4"]


def test_rebuild_splits_composites_recorded_by_the_old_code(mem):
    """旧账是按老口径攒的，重放战绩就该把它拆开——不然那个桶会一直留着。"""
    _battle(mem, angle_used="3.10+3.4")
    mem.data["angle_stats"] = {"3.10+3.4": {"used": 1, "effective": 0}}

    mem.rebuild_derived_state()

    assert set(mem.data["angle_stats"]) == {"3.10", "3.4"}
