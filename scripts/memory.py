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
    ) -> int:
        """记一条战绩，返回本次的分数变化。"""
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
            }
        )
        self._apply_score(delta)
        self._update_angle_stats(angle_used, effective=rounds >= 2 or outcome == "对方改口")
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
