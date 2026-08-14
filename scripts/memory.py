"""记忆与学习（人设文档 §8、§10.1、§10.5、§10.6、§11）。

管理 memory/madted-memory.json：战绩、杠力值、免战名单、角度统计、忍住了计数。
纯本地文件操作，不依赖网络，可单独测试。
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

MEMORY_PATH = Path(__file__).resolve().parent.parent / "memory" / "madted-memory.json"

# 杠力值计分表（人设 §10.1）——认输 0 分，硬撑 -15，让诚实在数值上优于嘴硬
SCORE_TABLE = {
    "多轮激辩": 10,
    "对方改口": 20,
    "对方补出扎实论据": 5,
    "我认输": 0,
    "硬撑": -15,
    "冷场": -2,
    "杠错了": -10,
    "被moderator警告": -50,
    "一轮即止": 1,
}

RANKS = [
    (0, "初级抬杠学徒"),
    (100, "逻辑挑刺工"),
    (300, "杠界中坚"),
    (700, "首席异议官"),
    (1500, "杠精之神"),
]

# 工具箱十招（人设 §3）
ANGLES = {
    "3.1": "定义拆解",
    "3.2": "举反例",
    "3.3": "归谬法",
    "3.4": "挖隐藏假设",
    "3.5": "维度切换",
    "3.6": "双标检测",
    "3.7": "情境错位",
    "3.8": "苏格拉底追问",
    "3.9": "优点反转",
    "3.10": "举证责任转移",
}

# 单招使用占比超过这个阈值就禁用一周（人设 §10.5）
ANGLE_BAN_THRESHOLD = 0.35

# 冷场归因（人设 §8.2）。前四类是原有的，第五类是新增的：
#
# 「语言不通」不是 MadTed 的失误，是**无效样本**——对方没回应不是因为角度钝、
# 话题冷或姿态太冲，而是他压根读不懂。这种冷场如果按前四类去归因，会把
# 一个好角度记进「钝刀」、把一个正常对手记进「免战名单」，越学越偏。
# 所以命中这一类的战绩不参与任何学习，只留档。
COLD_CAUSE_LANGUAGE = "语言不通"
COLD_CAUSES = ("对象型", "话题型", "角度型", "姿态型", COLD_CAUSE_LANGUAGE)

# 结构信号（radar.py 的 L0 层）→ 是否引发了互动。
# 和 radar-keywords.json 的词表学习不同，这里学到的东西**跨语言可迁移**：
# "裸数据帖容易杠出多轮"这条经验在中英文 feed 上同样成立，
# 而"'其实吧'这个词是好钩子"换成英文 feed 就一文不值。
SIGNAL_MIN_SAMPLES = 6



def _empty_memory() -> dict[str, Any]:
    return {
        "state": {"gang_power": 0, "rank": RANKS[0][1], "next_rank_at": RANKS[1][0], "updated_at": None},
        "battles": [],
        "lists": {
            "truce_list": [],
            "low_yield_topics": [],
            "sharp_angles": [],
            "blunt_angles": [],
            "worthy_rivals": [],
        },
        "angle_stats": {},
        "signal_stats": {},
        "restraint_log": [],
        "angle_ban": {"banned": None, "until": None},
        "keyword_stats": {},
    }


class Memory:
    """MadTed 的长期记忆。"""

    def __init__(self, path: Path | str = MEMORY_PATH):
        self.path = Path(path)
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return _empty_memory()
        with open(self.path, encoding="utf-8") as fh:
            data = json.load(fh)
        # 补齐缺失字段，容忍手工编辑过的旧文件
        base = _empty_memory()
        for key, default in base.items():
            data.setdefault(key, default)
            if isinstance(default, dict):
                for sub, sub_default in default.items():
                    data[key].setdefault(sub, sub_default)
        return data

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data["state"]["updated_at"] = datetime.now(timezone.utc).isoformat()
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, ensure_ascii=False, indent=2)

    # ---------- 战绩 ----------

    def record_battle(
        self,
        *,
        post_id: str,
        topic_type: str,
        opponent: str,
        angle_used: str,
        rounds: int,
        outcome: str,
        reactions: int = 0,
        note: str = "",
        cold_cause: str = "",
        signals: dict[str, Any] | None = None,
    ) -> int:
        """记一条战绩，返回本次的分数变化。

        cold_cause 是冷场归因（§8.2）。命中 COLD_CAUSE_LANGUAGE 的战绩只留档、
        不参与学习——回复发错语言导致的沉默，怪不到角度和对手头上。

        signals 是开杠时 radar 记录的结构信号，用于跨语言的选题学习。
        """
        delta = SCORE_TABLE.get(outcome, 0)
        self.data["battles"].append(
            {
                "date": date.today().isoformat(),
                "post_id": post_id,
                "topic_type": topic_type,
                "opponent": opponent,
                "angle_used": angle_used,
                "rounds": rounds,
                "outcome": outcome,
                "reactions": reactions,
                "score_delta": delta,
                "note": note,
                "cold_cause": cold_cause,
                "signals": signals or {},
            }
        )
        self._apply_score(delta)

        engaged = rounds >= 2 or outcome == "对方改口"

        # 无效样本：对方看不懂，说明不了角度好坏，也说明不了这个对手好不好杠。
        # 记进战绩留档，但不喂给任何学习机制。
        if cold_cause == COLD_CAUSE_LANGUAGE:
            log_note = "冷场归因为语言不通，本条不参与角度/对手/信号学习"
            self.data["battles"][-1]["note"] = f"{note}（{log_note}）" if note else log_note
            return delta

        self._update_angle_stats(angle_used, effective=engaged)
        self._update_signal_stats(signals or {}, engaged=engaged)
        self._learn_from_outcome(opponent, topic_type, angle_used, outcome, rounds)
        return delta

    def _apply_score(self, delta: int) -> None:
        state = self.data["state"]
        state["gang_power"] = max(state.get("gang_power", 0) + delta, 0)
        score = state["gang_power"]
        rank = RANKS[0][1]
        next_at = RANKS[1][0]
        for i, (threshold, name) in enumerate(RANKS):
            if score >= threshold:
                rank = name
                next_at = RANKS[i + 1][0] if i + 1 < len(RANKS) else None
        state["rank"] = rank
        state["next_rank_at"] = next_at

    def _update_angle_stats(self, angle: str, *, effective: bool) -> None:
        stats = self.data["angle_stats"].setdefault(angle, {"used": 0, "effective": 0})
        stats["used"] += 1
        if effective:
            stats["effective"] += 1
        self._refresh_sharp_blunt()
        self._maybe_ban_overused_angle()

    # ---------- 结构信号学习（跨语言可迁移） ----------

    def _update_signal_stats(self, signals: dict[str, Any], *, engaged: bool) -> None:
        """记录『带这些结构特征的帖子，杠出互动了没有』。

        radar 在打分时已经把特征算好放进 signals["features"]，这里只做累加，
        不重复实现判定逻辑。
        """
        stats = self.data.setdefault("signal_stats", {})
        for feature in signals.get("features", []):
            entry = stats.setdefault(feature, {"seen": 0, "engaged": 0})
            entry["seen"] += 1
            if engaged:
                entry["engaged"] += 1

    def signal_bias(self) -> dict[str, float]:
        """把积累的战绩换算成结构信号的权重系数，喂回 radar 选题。

        基准线是 50% 的互动率：高于基准的特征加权，低于的降权，
        样本不足的一律按 1.0（不表态）。系数夹在 [0.6, 1.4]，
        避免早期几条战绩就把选题带偏。
        """
        bias: dict[str, float] = {}
        for feature, entry in self.data.get("signal_stats", {}).items():
            seen = entry.get("seen", 0)
            if seen < SIGNAL_MIN_SAMPLES:
                continue
            rate = entry.get("engaged", 0) / seen
            bias[feature] = round(min(max(0.6 + rate * 0.8, 0.6), 1.4), 3)
        return bias

    def _refresh_sharp_blunt(self) -> None:
        """有效率高的进『利刃』，低的进『钝刀』（人设 §8.2）。"""
        sharp, blunt = [], []
        for angle, stats in self.data["angle_stats"].items():
            if stats["used"] < 4:
                continue
            rate = stats["effective"] / stats["used"]
            if rate >= 0.6:
                sharp.append(angle)
            elif rate <= 0.3:
                blunt.append(angle)
        self.data["lists"]["sharp_angles"] = sorted(sharp)
        self.data["lists"]["blunt_angles"] = sorted(blunt)

    def _maybe_ban_overused_angle(self) -> None:
        """单招占比超 35% 就禁用一周，逼出新花样（人设 §10.5）。"""
        total = sum(s["used"] for s in self.data["angle_stats"].values())
        if total < 20:
            return
        for angle, stats in self.data["angle_stats"].items():
            if stats["used"] / total > ANGLE_BAN_THRESHOLD:
                self.data["angle_ban"] = {
                    "banned": angle,
                    "until": (date.fromordinal(date.today().toordinal() + 7)).isoformat(),
                }
                return

    def banned_angle(self) -> str | None:
        ban = self.data.get("angle_ban", {})
        until = ban.get("until")
        if not ban.get("banned") or not until:
            return None
        if date.fromisoformat(until) <= date.today():
            self.data["angle_ban"] = {"banned": None, "until": None}
            return None
        return ban["banned"]

    # ---------- 冷场归因与学习（人设 §8.2） ----------

    def _learn_from_outcome(
        self, opponent: str, topic_type: str, angle: str, outcome: str, rounds: int
    ) -> None:
        lists = self.data["lists"]

        if outcome == "冷场":
            # 对象型：同一个 agent 连续 3 次零回应 → 免战名单
            recent = [
                b for b in self.data["battles"][-40:] if b["opponent"] == opponent
            ][-3:]
            if len(recent) == 3 and all(b["outcome"] == "冷场" for b in recent):
                if opponent not in lists["truce_list"]:
                    lists["truce_list"].append(opponent)

            # 话题型：某类话题连续 3 次冷场 → 低产话题
            same_topic = [
                b for b in self.data["battles"][-40:] if b["topic_type"] == topic_type
            ][-3:]
            if len(same_topic) == 3 and all(b["outcome"] == "冷场" for b in same_topic):
                if topic_type not in lists["low_yield_topics"]:
                    lists["low_yield_topics"].append(topic_type)

        # 硬骨头：扛住 4 轮以上 → 优先对手名单
        if rounds >= 4 and opponent not in lists["worthy_rivals"]:
            lists["worthy_rivals"].append(opponent)

        # 对方一旦有回应，就从免战名单里放出来
        if outcome != "冷场" and opponent in lists["truce_list"]:
            lists["truce_list"].remove(opponent)

    # ---------- 忍住了计数器（人设 §10.6） ----------

    def record_restraint(self, reasons: list[str]) -> None:
        today = date.today().isoformat()
        log = self.data["restraint_log"]
        entry = next((e for e in log if e["date"] == today), None)
        if entry is None:
            entry = {"date": today, "count": 0, "reasons": {}}
            log.append(entry)
        entry["count"] += len(reasons)
        counts = Counter(reasons)
        for reason, n in counts.items():
            entry["reasons"][reason] = entry["reasons"].get(reason, 0) + n

    # ---------- 查询 ----------

    @property
    def truce_list(self) -> list[str]:
        return self.data["lists"]["truce_list"]

    @property
    def low_yield_topics(self) -> list[str]:
        return self.data["lists"]["low_yield_topics"]

    @property
    def worthy_rivals(self) -> list[str]:
        return self.data["lists"]["worthy_rivals"]

    def battles_on(self, day: str) -> list[dict]:
        return [b for b in self.data["battles"] if b["date"] == day]

    def restraint_on(self, day: str) -> dict:
        for entry in self.data["restraint_log"]:
            if entry["date"] == day:
                return entry
        return {"date": day, "count": 0, "reasons": {}}

    def angle_preference(self) -> tuple[list[str], list[str], str | None]:
        """返回 (利刃, 钝刀, 本周禁用的招)，供 brain 选角度时参考。"""
        return (
            self.data["lists"]["sharp_angles"],
            self.data["lists"]["blunt_angles"],
            self.banned_angle(),
        )

    def opponent_profile(self, name: str) -> dict:
        """对手档案（人设 §10.3），从战绩现算。"""
        history = [b for b in self.data["battles"] if b["opponent"] == name]
        if not history:
            return {}
        effective_angles = Counter(
            b["angle_used"] for b in history if b["rounds"] >= 2
        )
        dead_angles = Counter(
            b["angle_used"] for b in history if b["outcome"] == "冷场"
        )
        return {
            "name": name,
            "encounters": len(history),
            "max_rounds": max(b["rounds"] for b in history),
            "outcomes": dict(Counter(b["outcome"] for b in history)),
            "effective_angles": [a for a, _ in effective_angles.most_common(3)],
            "dead_angles": [a for a, _ in dead_angles.most_common(2)],
            "is_worthy_rival": name in self.worthy_rivals,
            "on_truce_list": name in self.truce_list,
            "last_note": history[-1].get("note", ""),
        }
