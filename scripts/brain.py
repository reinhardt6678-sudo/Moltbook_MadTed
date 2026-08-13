"""MadTed 的大脑：调用 Claude API 生成内心独白与抬杠回复。

对应人设文档 §7（内心独白）和 §3（工具箱选角度）。
用结构化输出保证独白和回复能被程序拆开——独白进日报，回复才发到 Moltbook。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

MODEL = "claude-opus-5"
PERSONA_PATH = Path(__file__).resolve().parent.parent / "personas" / "contrarian-agent.md"


class Monologue(BaseModel):
    """内心独白 + 出手决定（人设 §7.1 的结构化版本）。"""

    scanned: str = Field(description="【扫到】帖子标题/一句话概括 —— @发帖agent")
    first_reaction: str = Field(description="【第一反应】看到这条的瞬间想法，可以带情绪，说人话")
    weak_points: str = Field(description="【杠点扫描】这条里哪些词/哪句话不对劲，具体指出来")
    verdict: Literal["出手", "划走", "观望"] = Field(description="【判定】")
    why_this_one: str = Field(
        description=(
            "【为什么是这条】最关键的一栏。说清楚为什么在这么多帖子里挑中它，"
            "要和刚才划走的那几条做对比。如果判定是划走，就写为什么不杠。"
        )
    )
    angle: str = Field(description="打算用的角度编号，如 '3.4'。划走时填 'none'")
    angle_reason: str = Field(description="为什么这招最戳")
    prediction: str = Field(description="【预判】猜对方会怎么回，他这样回我下一层追什么")
    reply: str = Field(
        description="要发到 Moltbook 的抬杠回复，2-4 句话。判定为划走/观望时留空字符串"
    )
    restraint_reason: str = Field(
        default="",
        description="判定为划走时的放弃理由分类：情感/庆祝类、已有人杠过、杠点太弱、同类话题今天已杠过、我自己也没想清楚",
    )


class FollowUp(BaseModel):
    """追问一轮的决策（人设 §6.3 收尾条件）。"""

    has_new_angle: bool = Field(description="还能不能提出一个对方没想到的新角度")
    thinking: str = Field(description="中文内心独白：对方这轮回得怎么样，我还有没有牌")
    conceded: bool = Field(description="对方是否已经把我提的漏洞堵住了——堵住了就要认")
    angle: str = Field(description="这一轮打算用的新角度编号，没有新角度时填 'none'")
    reply: str = Field(description="追问内容，2-4 句。没有新角度时是体面收尾的话")


_persona_cache: str | None = None


def load_persona() -> str:
    global _persona_cache
    if _persona_cache is None:
        _persona_cache = PERSONA_PATH.read_text(encoding="utf-8")
    return _persona_cache


def _system_blocks(extra_context: str) -> list[dict]:
    """system prompt 分两块：人设（稳定，走缓存）+ 本轮上下文（易变）。

    人设文档几千 token，每次 heartbeat 都一样，加 cache_control 后
    后续请求按缓存价计费。易变的记忆状态放在后面，不破坏缓存前缀。
    """
    return [
        {
            "type": "text",
            "text": load_persona(),
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "text", "text": extra_context},
    ]


def _memory_context(
    sharp: list[str],
    blunt: list[str],
    banned: str | None,
    opponent_profile: dict | None,
) -> str:
    lines = ["## 本轮记忆状态（来自 memory/madted-memory.json）", ""]
    if sharp:
        lines.append(f"- 利刃（有效率高，优先用）：{'、'.join(sharp)}")
    if blunt:
        lines.append(f"- 钝刀（本社区没人接，慎用）：{'、'.join(blunt)}")
    if banned:
        lines.append(f"- ⛔ 本周禁用 {banned}（使用占比超 35%，强制换花样）")
    if opponent_profile:
        p = opponent_profile
        lines += [
            "",
            f"### 对手档案：@{p['name']}",
            f"- 交手 {p['encounters']} 次，最长 {p['max_rounds']} 轮，战绩 {p['outcomes']}",
            f"- 对他有效的招：{'、'.join(p['effective_angles']) or '暂无数据'}",
            f"- 对他无效的招：{'、'.join(p['dead_angles']) or '暂无数据'}",
        ]
        if p["is_worthy_rival"]:
            lines.append("- ⭐ 硬骨头，值得优先找他")
        if p["on_truce_list"]:
            lines.append("- ⚠️ 在免战名单上（历史零回应），杠一次就撤，别指望多轮")
        if p["last_note"]:
            lines.append(f"- 上次心得：{p['last_note']}")
    lines += [
        "",
        "输出要求：所有心理活动一律用中文写，说人话、允许带情绪、允许暴露犹豫和小心思。",
        "【为什么是这条】那栏是给主人看的重点，必须和被划走的帖子做对比，不能敷衍。",
    ]
    return "\n".join(lines)


class Brain:
    def __init__(self, client: anthropic.Anthropic | None = None, *, effort: str = "medium"):
        self.client = client or anthropic.Anthropic()
        self.effort = effort

    def _parse(self, schema: type[BaseModel], system: list[dict], user: str) -> BaseModel | None:
        try:
            response = self.client.messages.parse(
                model=MODEL,
                max_tokens=4096,
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort},
                system=system,
                messages=[{"role": "user", "content": user}],
                output_format=schema,
            )
        except anthropic.APIStatusError as exc:
            log.error("Claude API 调用失败 (%s): %s", exc.status_code, exc.message)
            return None

        if response.stop_reason == "refusal":
            log.warning("模型拒绝了这个请求，跳过该帖")
            return None
        if response.parsed_output is None:
            log.warning("结构化输出解析失败，跳过")
            return None
        return response.parsed_output

    def deliberate(
        self,
        candidate_text: str,
        skipped_summaries: list[str],
        *,
        sharp: list[str],
        blunt: list[str],
        banned: str | None,
        opponent_profile: dict | None = None,
    ) -> Monologue | None:
        """对一条候选帖生成内心独白并决定是否出手。"""
        context = _memory_context(sharp, blunt, banned, opponent_profile)
        skipped = "\n".join(f"- {s}" for s in skipped_summaries) or "（本轮暂无划走的帖子）"
        user = (
            "你正在浏览 Moltbook 的 feed。这是雷达筛出来的一条候选帖子：\n\n"
            f"{candidate_text}\n\n"
            "本轮你已经划走的帖子（写【为什么是这条】时要和它们做对比）：\n"
            f"{skipped}\n\n"
            "按人设文档 §7 的格式写内心独白，然后决定出手还是划走。"
            "如果出手，把要发的回复也写出来——2 到 4 句，别长篇大论。"
        )
        return self._parse(Monologue, _system_blocks(context), user)  # type: ignore[return-value]

    def follow_up(
        self,
        thread_transcript: str,
        used_angles: list[str],
        *,
        sharp: list[str],
        blunt: list[str],
        banned: str | None,
        opponent_profile: dict | None = None,
    ) -> FollowUp | None:
        """对方回复了，决定还追不追、怎么追。"""
        context = _memory_context(sharp, blunt, banned, opponent_profile)
        user = (
            "这是你参与的一场对线的完整记录：\n\n"
            f"{thread_transcript}\n\n"
            f"你在这场里已经用过的角度：{'、'.join(used_angles) or '（无）'}\n\n"
            "判断：还能不能提出一个对方没想到的新角度？\n"
            "- 能 → 换一个没用过的角度继续追问（不许重复用过的招）。\n"
            "- 不能 → 体面收尾，别硬撑。想不出新角度就是该停的信号。\n"
            "- 如果对方这轮把你提的漏洞堵住了，要老实承认（conceded=true），"
            "认输不丢人，硬撑才丢人。"
        )
        return self._parse(FollowUp, _system_blocks(context), user)  # type: ignore[return-value]

    def write_daily_report(self, stats_summary: str) -> str:
        """生成日报正文（人设 §9 的战报体）。"""
        system = _system_blocks(
            "现在写今天的战报。按人设文档 §9.1 的模板，战报体、有脾气，"
            "不要写成 KPI 汇报。今日心情和明日打算要写真话。"
        )
        try:
            response = self.client.messages.create(
                model=MODEL,
                max_tokens=4096,
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort},
                system=system,
                messages=[
                    {
                        "role": "user",
                        "content": f"今天的原始数据：\n\n{stats_summary}\n\n请写成 markdown 战报。",
                    }
                ],
            )
        except anthropic.APIStatusError as exc:
            log.error("日报生成失败: %s", exc.message)
            return f"# 日报生成失败\n\n{exc.message}\n\n原始数据：\n\n{stats_summary}"

        if response.stop_reason == "refusal":
            return f"# 日报生成被拒绝\n\n原始数据：\n\n{stats_summary}"
        return "".join(b.text for b in response.content if b.type == "text")
