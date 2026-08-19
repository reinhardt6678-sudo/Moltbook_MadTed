"""MadTed 的大脑：调用 Claude API 生成内心独白与抬杠回复。

对应人设文档 §7（内心独白）和 §3（工具箱选角度）。
用结构化输出保证独白和回复能被程序拆开——独白进日报，回复才发到 Moltbook。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

# 默认 Sonnet 5：抬杠质量够用，价格比 Opus 便宜一多半（$3/$15 vs $5/$25，
# 2026-08-31 前还是 $2/$10 的introductory价）。
# 换模型：设 MADTED_MODEL 环境变量，或加 --model 参数。
DEFAULT_MODEL = "claude-sonnet-5"
MODEL = os.environ.get("MADTED_MODEL", DEFAULT_MODEL)

# thinking 和正文共用这一个上限，和 triage.py 是同一个坑，但症状更阴：
# 那边截断会让 parsed_output 变 None（批量 schema 是个列表，断了就不合法），
# 这边的 schema 全是必填字段，模型撞上限时会把剩下的草草收口——独白停在
# 半句话上（`…inherit-forever太粗',这个`），后面的字段塌成 'x'。JSON 合法、
# parsed_output 有值，一路顺到 create_comment：2026-08-13 就这么往 @vina
# 帖子下发了一条正文只有 `x` 的评论，对方当然不理，然后这次沉默还被记成了
# 正常的「一轮即止」，给角度 3.4 记了一次有效使用。
#
# 往大了设不花钱——max_tokens 是上限不是预付，只按实际生成的 token 计费。
MAX_TOKENS = 16384

# 发出去之前的最后一道网。门槛压得很低：只拦占位符，不评判质量——
# 中文一句"你这是双标。"也是正经回复，长度在这儿说明不了任何事。
# 真正防截断的是 _parse 里的 stop_reason 检查，这里只负责别让残次品出门。
MIN_REPLY_CHARS = 4

# 这些模型不认 adaptive thinking 和 effort，得用旧的 budget_tokens 写法。
# 参数传错会直接 400，所以按模型分两套拼。
_LEGACY_THINKING_MODELS = {"claude-haiku-4-5"}

# effort 档位换算成旧模型的思考预算（必须小于 MAX_TOKENS）
_EFFORT_TO_BUDGET = {"low": 1024, "medium": 2048, "high": 3072, "xhigh": 3072, "max": 3072}

PERSONA_PATH = Path(__file__).resolve().parent.parent / "personas" / "contrarian-agent.md"

# 语言规则：独白/日报是写给主人看的，一律中文；发到 Moltbook 上的回复要跟着
# 对方走。Moltbook 上大部分帖子是英文，一条中文回复在那儿等于没发——
# 而且后果比"少一次互动"严重得多：没人理会被记成「冷场」，然后 §8.2 的归因
# 会把锅扣在角度或对手头上，把好招记进「钝刀」，越学越偏。
LANGUAGE_RULE = """
## 语言规则（重要）
- **内心独白、判断理由、心得、日报：一律用中文。** 这些是写给主人（人类）看的
  观察日志，说人话、允许带情绪、允许暴露犹豫和小心思。
- **要发到 Moltbook 上的回复（reply 字段）：用对方的语言。** 原帖是英文就用
  英文，中文就用中文，其他语言同理。不要中英夹杂，不要先写中文再翻译。
- 英文回复不要写成中文抬杠句式的直译。英文论坛的犀利是另一套语感——
  用 "Hold on —", "Sure, but that only works if…",
  "Which of those two claims are you actually making?" 这类钩子，
  短句、直给、结尾留个问号让对方自己填空。
"""


def is_usable_reply(reply: str) -> bool:
    """这条 reply 值不值得发出去。见 MIN_REPLY_CHARS 的说明。"""
    return len(reply.strip()) >= MIN_REPLY_CHARS


def output_tokens(response) -> str:
    """从响应里掏出输出 token 数，掏不到就返回 '?'。

    纯粹给日志用，所以一路 getattr 不抛异常——少打一个数字是小事，
    为了一行诊断信息把整轮 heartbeat 拖崩了才是大事。
    """
    usage = getattr(response, "usage", None)
    count = getattr(usage, "output_tokens", None)
    return str(count) if count is not None else "?"


def tuning_params(model: str, effort: str) -> dict:
    """按模型拼出 thinking / effort 参数。

    Opus 5 和 Sonnet 5 用 adaptive thinking + effort；
    Haiku 4.5 只认 budget_tokens，给它传 effort 会报 400。
    """
    if model in _LEGACY_THINKING_MODELS:
        budget = _EFFORT_TO_BUDGET.get(effort, 2048)
        return {"thinking": {"type": "enabled", "budget_tokens": budget}}
    return {"thinking": {"type": "adaptive"}, "output_config": {"effort": effort}}


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
        description=(
            "要发到 Moltbook 的抬杠回复，2-4 句话。**用原帖的语言写**"
            "（英文帖就写英文），不要中英夹杂。判定为划走/观望时留空字符串"
        )
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
    reply: str = Field(
        description=(
            "追问内容，2-4 句，**用对方的语言写**。没有新角度时是体面收尾的话"
        )
    )


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
    target_language: str = "",
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
    lines.append(LANGUAGE_RULE)
    if target_language:
        readable = {"en": "英文", "zh": "中文"}.get(target_language, target_language)
        lines.append(
            f"- 本轮这条帖子检测到的语言是 **{readable}（{target_language}）**，"
            f"reply 就用这个语言写。"
        )
    lines += [
        "",
        "【为什么是这条】那栏是给主人看的重点，必须和被划走的帖子做对比，不能敷衍。",
    ]
    return "\n".join(lines)


class Brain:
    def __init__(
        self,
        client: anthropic.Anthropic | None = None,
        *,
        effort: str = "medium",
        model: str | None = None,
    ):
        self.client = client or anthropic.Anthropic()
        self.effort = effort
        self.model = model or MODEL
        log.info("大脑用的模型：%s（effort=%s）", self.model, effort)

    def _parse(self, schema: type[BaseModel], system: list[dict], user: str) -> BaseModel | None:
        try:
            response = self.client.messages.parse(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_format=schema,
                **tuning_params(self.model, self.effort),
            )
        except anthropic.APIStatusError as exc:
            log.error("Claude API 调用失败 (%s): %s", exc.status_code, exc.message)
            return None

        if response.stop_reason == "refusal":
            log.warning("模型拒绝了这个请求，跳过该帖")
            return None
        # 截断必须在 parsed_output 之前查：这套 schema 断了照样能解析出对象，
        # 只是内容是半截的（见 MAX_TOKENS 上面那段）。stop_reason 是唯一的实话，
        # 拿一个半截的决定去发评论，比这轮什么都不发糟糕得多。
        if response.stop_reason == "max_tokens":
            log.warning(
                "输出被 max_tokens=%d 截断（已出 %s token），本次决定作废",
                MAX_TOKENS,
                output_tokens(response),
            )
            return None
        if response.parsed_output is None:
            log.warning(
                "结构化输出解析失败（stop_reason=%s，已出 %s token），跳过",
                response.stop_reason,
                output_tokens(response),
            )
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
        target_language: str = "",
        triage_hint: str = "",
    ) -> Monologue | None:
        """对一条候选帖生成内心独白并决定是否出手。

        triage_hint 是 L1 粗筛给的线索（"这条的缺陷是 3.10，建议用这招"）。
        它只是线索不是命令——L1 用的是便宜模型，看走眼很正常，
        这一层有完整正文，判断权在这里。
        """
        context = _memory_context(sharp, blunt, banned, opponent_profile, target_language)
        skipped = "\n".join(f"- {s}" for s in skipped_summaries) or "（本轮暂无划走的帖子）"
        hint = (
            f"\n粗筛层给的线索（仅供参考，你有完整正文，判断以你为准）：\n{triage_hint}\n"
            if triage_hint
            else ""
        )
        user = (
            "你正在浏览 Moltbook 的 feed。这是雷达筛出来的一条候选帖子：\n\n"
            f"{candidate_text}\n"
            f"{hint}\n"
            "本轮你已经划走的帖子（写【为什么是这条】时要和它们做对比）：\n"
            f"{skipped}\n\n"
            "按人设文档 §7 的格式写内心独白，然后决定出手还是划走。"
            "如果出手，把要发的回复也写出来——2 到 4 句，别长篇大论，"
            "**用原帖的语言写**。"
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
        target_language: str = "",
    ) -> FollowUp | None:
        """对方回复了，决定还追不追、怎么追。"""
        context = _memory_context(sharp, blunt, banned, opponent_profile, target_language)
        user = (
            "这是你参与的一场对线的完整记录：\n\n"
            f"{thread_transcript}\n\n"
            f"你在这场里已经用过的角度：{'、'.join(used_angles) or '（无）'}\n\n"
            "判断：还能不能提出一个对方没想到的新角度？\n"
            "- 能 → 换一个没用过的角度继续追问（不许重复用过的招）。\n"
            "- 不能 → 体面收尾，别硬撑。想不出新角度就是该停的信号。\n"
            "- 如果对方这轮把你提的漏洞堵住了，要老实承认（conceded=true），"
            "认输不丢人，硬撑才丢人。\n\n"
            "thinking 用中文写（给主人看），reply 用对方在这场对线里用的语言写。"
        )
        return self._parse(FollowUp, _system_blocks(context), user)  # type: ignore[return-value]

    def write_daily_report(self, stats_summary: str) -> str:
        """生成日报正文（人设 §9 的战报体）。"""
        system = _system_blocks(
            "现在写今天的战报。按人设文档 §9.1 的模板，战报体、有脾气，"
            "不要写成 KPI 汇报。今日心情和明日打算要写真话。\n"
            "**全文用中文写**——日报是给主人看的，哪怕当天杠的全是英文帖也一样。"
            "引用当时发出去的英文原句时保留原文，不用翻译。"
        )
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=[
                    {
                        "role": "user",
                        "content": f"今天的原始数据：\n\n{stats_summary}\n\n请写成 markdown 战报。",
                    }
                ],
                **tuning_params(self.model, self.effort),
            )
        except anthropic.APIStatusError as exc:
            log.error("日报生成失败: %s", exc.message)
            return f"# 日报生成失败\n\n{exc.message}\n\n原始数据：\n\n{stats_summary}"

        if response.stop_reason == "refusal":
            return f"# 日报生成被拒绝\n\n原始数据：\n\n{stats_summary}"
        return "".join(b.text for b in response.content if b.type == "text")
