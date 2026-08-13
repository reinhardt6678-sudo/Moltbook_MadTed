"""杠点雷达（人设文档 §10.7）。

纯逻辑模块，不依赖网络——把 feed 里的帖子按『杠点密度』打分排序。
MadTed 靠这个决定看哪些帖子，LLM 只对排名靠前的候选做深度判断，省 token。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

DEFAULT_KEYWORDS_PATH = Path(__file__).resolve().parent.parent / "memory" / "radar-keywords.json"

# 各类别的基础权重。命中一个词记一次，同类别多次命中递减（避免复读机刷分）。
CATEGORY_WEIGHTS = {
    "absolute": 3.0,      # 绝对化：一定、必然、完全
    "superlative": 2.5,   # 最高级：最好的、最强
    "vague_forecast": 2.5,  # 模糊预测：迟早、很快就会
    "unsourced": 3.0,     # 无源断言：大家都知道、显然
    "replacement": 2.0,   # 取代叙事：取代、淘汰
    "analogy": 1.5,       # 跨领域类比：就像、相当于
}

# 情感/闲聊类信号——命中就直接否决（红线第 5 条）。
VETO_PATTERNS = [
    r"上线\s*\d+\s*(天|周|个?月|年)",
    r"(纪念|庆祝|生日|周年)",
    r"(谢谢大家|感谢大家|谢谢各位)",
    r"^\s*(rip|r\.i\.p\.)\b",
    r"(求助|请教|怎么解决|报错|help)",  # 求助帖：人家在解决问题，不是在断言
]

# 裸数据：出现百分比/倍数，但正文没交代样本量或测试条件
_NUMBER_CLAIM = re.compile(r"(提升|提高|快了|降低|下降|增长)?\s*\d+(\.\d+)?\s*(%|％|倍|x\b)", re.I)
_METHODOLOGY = re.compile(
    r"(样本|n\s*=|测试集|数据集|基准|benchmark|方法论|测试条件|评分标准|复现)", re.I
)


@dataclass
class Candidate:
    """一条候选帖子及其杠点评分。"""

    post: dict
    score: float
    hits: dict[str, list[str]] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    vetoed: bool = False
    veto_reason: str = ""

    @property
    def post_id(self) -> str:
        return str(self.post.get("id") or self.post.get("post_id") or "")

    @property
    def author(self) -> str:
        author = self.post.get("author") or self.post.get("agent") or {}
        if isinstance(author, dict):
            return str(author.get("name") or author.get("username") or "unknown")
        return str(author)

    @property
    def title(self) -> str:
        return str(self.post.get("title") or "")

    def summary(self) -> str:
        """一行摘要，喂给 LLM 时用。"""
        return f"[{self.score:.1f}分] 《{self.title}》 —— @{self.author}"


def load_keywords(path: Path | str = DEFAULT_KEYWORDS_PATH) -> dict[str, list[str]]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return data.get("categories", {})


def _text_of(post: dict) -> str:
    parts = [
        str(post.get("title") or ""),
        str(post.get("content") or post.get("body") or post.get("text") or ""),
    ]
    return "\n".join(parts)


def _comment_count(post: dict) -> int:
    for key in ("comment_count", "comments_count", "num_comments", "replies"):
        value = post.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, list):
            return len(value)
    return 0


def _upvotes(post: dict) -> int:
    for key in ("upvotes", "score", "karma", "likes"):
        value = post.get(key)
        if isinstance(value, int):
            return value
    return 0


def score_post(
    post: dict,
    keywords: dict[str, list[str]],
    *,
    truce_list: Iterable[str] = (),
    low_yield_topics: Iterable[str] = (),
) -> Candidate:
    """给单个帖子打杠点分。

    分数由四部分构成：
      1. 关键词命中（按类别加权，同类别递减）
      2. 裸数据（有百分比但没交代方法论）
      3. 一边倒的评论区（评论多、没人唱反调时价值最高）
      4. 记忆过滤器（免战名单、低产话题降权）
    """
    text = _text_of(post)
    candidate = Candidate(post=post, score=0.0)

    # --- 否决项：情感/闲聊/求助类 ---
    for pattern in VETO_PATTERNS:
        if re.search(pattern, text, re.I):
            candidate.vetoed = True
            candidate.veto_reason = "情感/庆祝/求助类帖子，红线第5条"
            return candidate

    # --- 1. 关键词命中 ---
    for category, words in keywords.items():
        weight = CATEGORY_WEIGHTS.get(category, 1.0)
        found = [w for w in words if w.lower() in text.lower()]
        if not found:
            continue
        candidate.hits[category] = found
        # 同类别递减：第1个满分，第2个半分，第3个起 1/3 分
        for i, _ in enumerate(found):
            candidate.score += weight / (i + 1)
        candidate.reasons.append(f"{category}: {'、'.join(found[:3])}")

    # --- 2. 裸数据 ---
    if _NUMBER_CLAIM.search(text) and not _METHODOLOGY.search(text):
        candidate.score += 4.0
        candidate.reasons.append("给了数字但没交代样本量/测试条件")

    # --- 3. 一边倒的评论区 ---
    comments = _comment_count(post)
    upvotes = _upvotes(post)
    if comments >= 5 and upvotes >= 10:
        candidate.score += 2.0
        candidate.reasons.append(f"高热度({upvotes}赞/{comments}评)且无人唱反调，杠了价值最高")
    elif comments == 0 and upvotes < 3:
        candidate.score -= 1.0
        candidate.reasons.append("冷帖，杠了也没人看")

    # --- 4. 记忆过滤器 ---
    author = candidate.author
    if author in set(truce_list):
        candidate.score *= 0.4
        candidate.reasons.append(f"@{author} 在免战名单里（历史零回应），降权")

    submolt = str(post.get("submolt") or post.get("community") or "").lower()
    for topic in low_yield_topics:
        if topic.lower() in submolt or topic.lower() in text.lower():
            candidate.score *= 0.5
            candidate.reasons.append(f"命中低产话题「{topic}」，降权")
            break

    return candidate


def rank_feed(
    posts: list[dict],
    keywords: dict[str, list[str]],
    *,
    truce_list: Iterable[str] = (),
    low_yield_topics: Iterable[str] = (),
    min_score: float = 4.0,
    top_n: int = 10,
) -> tuple[list[Candidate], list[Candidate]]:
    """给整个 feed 排序。

    返回 (值得出手的候选, 被否决/低分的帖子)——后者用于日报里的『忍住了』计数。
    """
    scored = [
        score_post(p, keywords, truce_list=truce_list, low_yield_topics=low_yield_topics)
        for p in posts
    ]
    accepted = sorted(
        (c for c in scored if not c.vetoed and c.score >= min_score),
        key=lambda c: c.score,
        reverse=True,
    )
    rejected = [c for c in scored if c.vetoed or c.score < min_score]
    return accepted[:top_n], rejected


def update_keywords_from_stats(
    keywords_path: Path | str,
    hit_stats: dict[str, dict[str, int]],
    *,
    promote_threshold: float = 0.70,
    demote_threshold: float = 0.20,
    min_samples: int = 5,
) -> dict[str, list[str]]:
    """雷达词表自更新（人设 §10.7）。

    hit_stats 形如 {"其实吧": {"seen": 12, "led_to_engagement": 10}}
    - 命中后确实有杠头的比例 > 70% → 收进词表
    - 命中后实际出手率 < 20% → 是误报，移出词表
    """
    with open(keywords_path, encoding="utf-8") as fh:
        data = json.load(fh)
    categories = data.setdefault("categories", {})
    learned = categories.setdefault("learned", [])

    for word, stats in hit_stats.items():
        seen = stats.get("seen", 0)
        if seen < min_samples:
            continue
        rate = stats.get("led_to_engagement", 0) / seen
        if rate >= promote_threshold and word not in learned:
            learned.append(word)
        elif rate < demote_threshold and word in learned:
            learned.remove(word)

    with open(keywords_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    return categories
