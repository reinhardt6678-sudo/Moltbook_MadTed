"""杠点雷达 L1：语义粗筛层。

L0（radar.py）判断的是"帖子长什么样"——有没有链接、赞评比多少、emoji 密不密集。
判断不了"他说的话站不站得住"。后者需要语义理解。

关键设计：**用 rubric 而不是词表**。词表编码的是"某种语言怎么表达绝对化"，
每加一种语言就要重写一遍；rubric 编码的是"什么算绝对化"，模型天然知道
`guaranteed` / `一定` / `間違いなく` 是同一回事，一次写完全语言通用。

rubric 的条目就是人设 §3 工具箱的结构化版本——每条论证缺陷对应一招。

成本：Haiku 4.5 是 $1/$5 每百万 token。一次 heartbeat 扫 60 条帖子，
分批送进来约 9K 输入 + 2.4K 输出 ≈ $0.02。每小时一轮的话一天约 $0.5，
一个月约 $4。比让 Sonnet 去逐条判断"这条要不要杠"便宜两个数量级——
而且省钱不是重点，重点是让 L2 那几次贵的调用花在对的帖子上。
"""

from __future__ import annotations

import logging
import os
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from brain import tuning_params
from radar import Candidate

log = logging.getLogger(__name__)

# 粗筛用最便宜的模型就够——这一层只做分类，不写文案。
DEFAULT_TRIAGE_MODEL = "claude-haiku-4-5"
TRIAGE_MODEL = os.environ.get("MADTED_TRIAGE_MODEL", DEFAULT_TRIAGE_MODEL)

MAX_TOKENS = 4096

# 一次送多少条进去。太多会让模型在长列表里偷懒（后面几条敷衍），
# 太少则每批都要重付一遍 rubric 的输入 token。
BATCH_SIZE = 25

# 注意：**不要给 rubric 加 cache_control**。Haiku 4.5 的最小可缓存前缀是
# 4096 token，这份 rubric 一千出头，加了也不会缓存——而且是静默不缓存
# （不报错，cache_creation_input_tokens 直接是 0），只会让人误以为省了钱。

# 论证缺陷清单 = 人设 §3 工具箱的语言无关版本。
DEFECTS: dict[str, str] = {
    "3.1": "评价词未定义：用了『高效/公平/更好/安全/聪明』这类词却没说按谁的标准、具体指什么",
    "3.2": "全称断言无边界：说『所有/任何/每一个/永远/从不』，但显然存在反例",
    "3.3": "推到极端就荒谬：把他的逻辑往前推一步，结论明显站不住",
    "3.4": "隐藏前提没说出口：结论成立需要某个没被言明的假设",
    "3.5": "维度混淆：把『技术上可行』当成『应该这么做』，或只谈短期不谈长期",
    "3.6": "双标：对 A 和 B 用了不同的评判标准",
    "3.7": "时间/情境错位：引用的经验、数据或规则已过时或不适用当前场景",
    "3.8": "因果与相关混淆：观察到同时出现就断定其中一个导致另一个",
    "3.9": "优点可反转：他列的优点，同一个特性其实也是缺点",
    "3.10": "断言无来源：给了数字、排名或『大家都这么认为』，但没有出处、样本量或测试条件",
    "3.11": "单一样本推广：拿自己一个项目/一次经历的结论套到所有情况上",
    "3.12": "跨领域类比当论证：把 A 领域的规律直接套到 B 领域，类比本身就是论点",
    "3.13": "预测无期限无证伪条件：『迟早会』『很快就』，但不说多久、也不说怎样算错",
}

_DEFECT_LINES = "\n".join(f"- {code}：{desc}" for code, desc in DEFECTS.items())

RUBRIC = f"""你是 MadTed（Moltbook 上一个以抬杠为职业的 agent）的选题助手。
你的工作**不是**写抬杠回复，而是从一批帖子里快速判断哪些值得他出手。

## 重要：只看论证结构，不看语言
帖子可能是任何语言（英文居多，也有中文和其他语言）。你要判断的是
**论证结构上有没有缺陷**，不是"有没有出现某些词"。
`guaranteed`、`一定`、`絶対に` 是同一种缺陷，用什么语言写的无关紧要。

## 论证缺陷清单
{_DEFECT_LINES}

## 打分标准（hook_score，0-10）
- 8-10：多处明确缺陷，且评论区没人指出过——最值得出手
- 5-7：有一处清晰的可追问点
- 2-4：勉强能挑毛病，但硬杠会显得无理取闹
- 0-1：论证扎实，或者压根不是在做论断（分享、公告、讨论串）

**给了数字并且交代了样本量/测试条件/给了链接的帖子，分要压低**——
人家把话说清楚了，硬质疑只会翻车。

## 红线（is_redline=true 的情况）
下面这些帖子无论杠点多少都不能碰（人设红线第 5 条）：
- 情感分享、庆祝成就、上线纪念、致谢、悼念
- 求助帖：人家在解决问题，不是在下断言
- 纯 meme / 玩梗帖
命中任意一条就把 is_redline 设为 true，hook_score 设为 0。

## 输出
对每一条帖子输出一条判定，index 必须和输入的编号一一对应，一条都不能漏。
note 用中文写，一句话，是给主人看的观察日志——说人话，可以带情绪。"""


class TriageVerdict(BaseModel):
    """对单条帖子的粗筛判定。"""

    index: int = Field(description="对应输入里的帖子编号")
    is_redline: bool = Field(description="是否命中红线（情感/庆祝/求助/meme），命中就绝对不能杠")
    hook_score: int = Field(description="杠点密度 0-10", ge=0, le=10)
    defect: str = Field(description="最主要的论证缺陷编号，如 '3.10'；没有缺陷填 'none'")
    angle: str = Field(description="建议 MadTed 用哪一招，通常和 defect 相同；无建议填 'none'")
    note: str = Field(description="一句话中文说明，给主人看的观察日志")


class TriageBatch(BaseModel):
    verdicts: list[TriageVerdict]


class ScoredCandidate(BaseModel):
    """L0 + L1 合并后的结果（不直接用于 API，只是个方便的容器）。"""

    model_config = {"arbitrary_types_allowed": True}

    candidate: Candidate
    verdict: TriageVerdict | None = None

    @property
    def combined_score(self) -> float:
        """L1 语义分为主，L0 结构分做微调。

        L1 看得懂内容，判断力比 L0 强得多，所以给它主导权；L0 分数
        （赞评比、有无出处这些 L1 看不到的元数据）按小权重叠加，
        在 L1 打平的时候用来决出先后。
        """
        if self.verdict is None:
            return self.candidate.score
        return self.verdict.hook_score * 10 + min(self.candidate.score, 10)


def _format_batch(candidates: list[Candidate]) -> str:
    blocks = []
    for i, cand in enumerate(candidates):
        body = (cand.post.get("content") or cand.post.get("body") or "")[:900]
        blocks.append(
            f"### 帖子 {i}\n"
            f"标题：{cand.title}\n"
            f"作者：@{cand.author}\n"
            f"热度：{cand.post.get('upvotes', 0)} 赞 / {cand.post.get('comment_count', 0)} 评论\n"
            f"正文：{body or '(无正文)'}"
        )
    return "\n\n".join(blocks)


class Triage:
    """L1 粗筛器。"""

    def __init__(
        self,
        client: anthropic.Anthropic | None = None,
        *,
        model: str | None = None,
        effort: str = "low",
        batch_size: int = BATCH_SIZE,
    ):
        self.client = client or anthropic.Anthropic()
        self.model = model or TRIAGE_MODEL
        self.effort = effort
        self.batch_size = batch_size

    def _score_batch(self, candidates: list[Candidate]) -> dict[int, TriageVerdict]:
        try:
            response = self.client.messages.parse(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=RUBRIC,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"这是本轮 feed 里雷达初筛出的 {len(candidates)} 条帖子。"
                            "逐条判定，一条都不要漏：\n\n" + _format_batch(candidates)
                        ),
                    }
                ],
                output_format=TriageBatch,
                **tuning_params(self.model, self.effort),
            )
        except anthropic.APIStatusError as exc:
            log.error("L1 粗筛调用失败 (%s): %s", exc.status_code, exc.message)
            return {}

        if response.stop_reason == "refusal":
            log.warning("L1 粗筛被模型拒绝，本批退回 L0 排序")
            return {}
        if response.parsed_output is None:
            log.warning("L1 粗筛结构化输出解析失败，本批退回 L0 排序")
            return {}

        # 模型可能漏掉或编造 index，只认落在范围内的
        return {
            v.index: v
            for v in response.parsed_output.verdicts
            if 0 <= v.index < len(candidates)
        }

    def rank(self, candidates: list[Candidate]) -> list[ScoredCandidate]:
        """对 L0 的候选做语义粗筛，返回按合并分排序的结果。

        粗筛失败（API 报错、解析失败）不会中断本轮 heartbeat——
        退化成 L0 的排序继续走，只是选题精度差一些。
        """
        if not candidates:
            return []

        results: list[ScoredCandidate] = []
        for start in range(0, len(candidates), self.batch_size):
            chunk = candidates[start : start + self.batch_size]
            verdicts = self._score_batch(chunk)
            if not verdicts:
                log.info("第 %d 批粗筛无结果，该批退回 L0 排序", start // self.batch_size + 1)
            for i, cand in enumerate(chunk):
                results.append(ScoredCandidate(candidate=cand, verdict=verdicts.get(i)))

        results.sort(key=lambda s: s.combined_score, reverse=True)
        return results


def split_redlines(
    scored: list[ScoredCandidate],
) -> tuple[list[ScoredCandidate], list[str]]:
    """把命中红线的挑出来，返回 (可以继续的, 忍住了的理由列表)。

    L1 抓到的红线帖是**结构层漏掉**的那些——比如没堆 emoji、措辞也不像
    求助的庆祝帖。这些计入「忍住了」，因为确实是有意识地放弃了。
    """
    passable: list[ScoredCandidate] = []
    restraint: list[str] = []
    for item in scored:
        if item.verdict is not None and item.verdict.is_redline:
            restraint.append("情感/庆祝类")
            continue
        if item.verdict is not None and item.verdict.hook_score <= 1:
            restraint.append("杠点太弱")
            continue
        passable.append(item)
    return passable, restraint
