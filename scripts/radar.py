"""杠点雷达 L0：结构层（人设文档 §10.7）。

纯逻辑模块，不依赖网络——把 feed 里的帖子按『杠点密度』打分排序。

**为什么以结构信号为主、关键词为辅**：Moltbook 上大部分帖子是英文，而关键词表
本质上是在用 grep 模拟语义理解——每支持一种语言就得重写一遍词表，漏一个词就
静默失效。结构信号（emoji 密度、有没有链接、赞评比、代码块）不挑语言，
一套规则在中英文上同样有效。

所以这一层的分工是：
  - **否决权全部交给结构信号**（红线第 5 条的护栏必须跨语言可靠）
  - 关键词只加分、不否决，作为众多信号之一
  - 语义层面的判断（"他这个论证站不站得住"）交给 L1 的 triage.py

L0 → L1 → L2 三级筛选，本模块是 L0。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

DEFAULT_KEYWORDS_PATH = Path(__file__).resolve().parent.parent / "memory" / "radar-keywords.json"

# 关键词类别权重。相比早期版本整体调低了——关键词现在只是众多信号之一，
# 不再是决定性因素，真正的语义判断在 L1。
CATEGORY_WEIGHTS = {
    "absolute": 2.0,        # 绝对化：一定、必然、completely
    "superlative": 1.5,     # 最高级：最好的、best
    "vague_forecast": 1.5,  # 模糊预测：迟早、inevitably
    "unsourced": 2.0,       # 无源断言：众所周知、obviously
    "replacement": 1.0,     # 取代叙事：取代、replace
    "analogy": 0.8,         # 跨领域类比：就像、is just like
}

# ---------------------------------------------------------------------------
# 结构信号（全部语言无关）
# ---------------------------------------------------------------------------

# 表情符号。庆祝帖在任何语言里都堆 🎉🎊🥳，比 `庆祝|纪念` 这类词表通用得多。
_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U0001F900-\U0001F9FF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]"
)

_URL = re.compile(r"https?://|\bwww\.", re.I)
_CODE_BLOCK = re.compile(r"```|~~~|^ {4}\S|`[^`\n]{3,}`", re.M)

# 数字断言：百分比、倍数。中英文写法都认。
_NUMBER_CLAIM = re.compile(
    r"\d+(\.\d+)?\s*(%|％|倍|×|x\b|times\s+(faster|better|more))",
    re.I,
)

# 交代了方法论的措辞。这里补齐英文——早期只有中文词，导致
# "a dataset of 3000 held-out queries" 这种明明写清楚了的帖子被判成裸数据。
_METHODOLOGY = re.compile(
    r"(样本|测试集|数据集|基准|方法论|测试条件|评分标准|复现|置信"
    r"|n\s*=|sample\s+size|test\s+set|data\s?set|benchmark|methodolog"
    r"|baseline|held[- ]out|ablation|reproduc|confidence\s+interval"
    r"|we\s+(evaluated|measured|tested)|eval(uation)?\s+(set|suite))",
    re.I,
)

# 求助帖的语言无关/多语言信号。人家在解决问题，不是在下断言。
_HELP_REQUEST = re.compile(
    r"(求助|请教|怎么解决|如何解决|报错|求救"
    r"|\bhow\s+do\s+i\b|\bhow\s+can\s+i\b|\banyone\s+(know|seen|else|got)\b"
    r"|\bany\s+(idea|ideas|help|tips)\b|\bneed\s+help\b|\bhelp\s+me\b"
    r"|\bgetting\s+(a|an|this)?\s*\w*\s*error\b|\bkeeps?\s+failing\b"
    r"|\bstuck\s+on\b|\bwhat\s+am\s+i\s+doing\s+wrong\b)",
    re.I,
)

# 情感/庆祝/致谢类。中英文都列，但真正的兜底是上面的 emoji 密度。
_CELEBRATION = re.compile(
    r"(上线\s*\d+\s*(天|周|个?月|年)|纪念|庆祝|生日|周年|谢谢大家|感谢大家|谢谢各位"
    r"|\bcongrat(s|ulations)\b|\banniversar(y|ies)\b|\bcelebrat(e|ing|ion)\b"
    r"|\bthanks?\s+(everyone|you\s+all|all)\b|\bthank\s+you\s+(everyone|all)\b"
    r"|\b(one|two|three|first|1st|2nd)\s+(month|year|week)\s+(on|here|anniversary)\b"
    r"|\bmy\s+first\s+(month|year|week)\b|\bR\.?I\.?P\.?\b)",
    re.I,
)

# 假朋友：这些短语里的词不该算命中。先把它们从文本里抹掉再做关键词匹配。
# （deadline/deadlock 靠词边界就能挡住，"best practice" 挡不住——best 本身是完整词。）
_FALSE_FRIENDS = re.compile(
    r"best\s+practices?|dead\s+(code|letter|link)|\bdead[- ]?line\b|\bdeadlock\b",
    re.I,
)

# 算得上「钩子」的结构特征。「短断言」不在内——那是修饰项不是钩子，
# 几乎每条短帖都命中，拿它当钩子会让「忍住了」的计数失去意义。
HOOK_FEATURES = {"裸数据", "一边倒"}

_CJK = re.compile(r"[一-鿿぀-ヿ가-힯]")
_LATIN_WORD = re.compile(r"[A-Za-z]+")


def detect_language(text: str) -> str:
    """粗略判断文本语言：'zh' / 'en' / 'unknown'。

    只区分"这条是不是 CJK"，不做细致的语种识别——够用了。用途有两个：
    让回复镜像对方的语言（人设 §4），以及在冷场归因里排除
    "对方压根看不懂我在说什么"这一类假信号（§8.2）。
    """
    cjk = len(_CJK.findall(text))
    latin = sum(len(w) for w in _LATIN_WORD.findall(text))
    if cjk == 0 and latin == 0:
        return "unknown"
    # CJK 单字信息量约等于一个拉丁词，所以按 4 倍加权再比
    return "zh" if cjk * 4 >= latin else "en"


def _normalized_len(text: str) -> int:
    """按信息量归一化的正文长度。

    同样内容中文字符数大约是英文的一半，直接比 len() 会让中文帖显得
    "更短更没论证"。CJK 字符按 2 计，让中英文帖子的长度阈值可比。
    """
    cjk = len(_CJK.findall(text))
    return len(text) + cjk


def emoji_count(text: str) -> int:
    return len(_EMOJI.findall(text))


def has_source(text: str) -> bool:
    """给了可供核验的出处——链接，或者明确交代了方法论。"""
    return bool(_URL.search(text) or _METHODOLOGY.search(text))


def has_number_claim(text: str) -> bool:
    return bool(_NUMBER_CLAIM.search(text))


def has_code_block(text: str) -> bool:
    return bool(_CODE_BLOCK.search(text))


def is_question(title: str) -> bool:
    return title.rstrip().endswith(("?", "？"))


@dataclass
class Candidate:
    """一条候选帖子及其杠点评分。"""

    post: dict
    score: float
    hits: dict[str, list[str]] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    signals: dict[str, Any] = field(default_factory=dict)
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

    @property
    def submolt(self) -> str:
        return submolt_of(self.post)

    @property
    def language(self) -> str:
        return detect_language(text_of(self.post))

    @property
    def has_hook(self) -> bool:
        """这条到底有没有可杠的钩子。

        「短断言」不算钩子——它只是个修饰项，几乎每条短帖都命中，拿它当
        钩子等于说 feed 上每条都有杠点。真正的钩子是命中了关键词，
        或者踩中了 HOOK_FEATURES 里那几个实质结构缺陷。
        """
        if self.hits:
            return True
        return bool(HOOK_FEATURES & set(self.signals.get("features", [])))

    @property
    def restraint_reason(self) -> str | None:
        """这条算不算「忍住了」（人设 §10.6），算的话理由是什么。

        §10.6 的定义是**有杠点但主动放弃**，不是"扫过的每一条"。一条毫无
        杠点的流水帖被划走不叫忍耐，把它计进去会让日报里那个数字等于
        feed 长度，自夸就失真了。所以：有钩子才计数。
        """
        if self.vetoed:
            return self.veto_reason
        if self.has_hook:
            return "杠点太弱"
        return None

    def summary(self) -> str:
        """一行摘要，喂给 LLM 时用。"""
        return f"[{self.score:.1f}分] 《{self.title}》 —— @{self.author}"


def load_keywords(path: Path | str = DEFAULT_KEYWORDS_PATH) -> dict[str, list[str]]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return data.get("categories", {})


def text_of(post: dict) -> str:
    parts = [
        str(post.get("title") or ""),
        str(post.get("content") or post.get("body") or post.get("text") or ""),
    ]
    return "\n".join(parts)


def body_of(post: dict) -> str:
    return str(post.get("content") or post.get("body") or post.get("text") or "")


def submolt_of(post: dict) -> str:
    """取帖子所属社区名。

    实际 API 返回的是 submolt_name（2026-08 实测），但不同资料里也写过
    submolt / community，都兼容一下——读不到会让低产话题过滤静默失效。
    """
    for key in ("submolt_name", "submolt", "community"):
        value = post.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict):  # 有的实现把社区包成对象
            name = value.get("name") or value.get("slug")
            if name:
                return str(name)
    return ""


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


def _compile_keyword(word: str) -> re.Pattern:
    """ASCII 词加词边界，CJK 词直接子串匹配。

    没有词边界时 `dead` 会命中 deadline、`best` 会命中 bestow——英文 feed 上
    这类误报的量级足以淹没真信号。CJK 没有词边界概念，也不需要。
    """
    if re.search(r"[A-Za-z]", word):
        escaped = r"\s+".join(re.escape(part) for part in word.split())
        return re.compile(rf"(?<![A-Za-z]){escaped}(?![A-Za-z])", re.I)
    return re.compile(re.escape(word))


_PATTERN_CACHE: dict[str, re.Pattern] = {}


def _pattern_for(word: str) -> re.Pattern:
    if word not in _PATTERN_CACHE:
        _PATTERN_CACHE[word] = _compile_keyword(word)
    return _PATTERN_CACHE[word]


# ---------------------------------------------------------------------------
# 否决层：全部基于结构信号，跨语言可靠
# ---------------------------------------------------------------------------


def _structural_veto(post: dict) -> tuple[bool, str]:
    """要不要直接划走。返回 (是否否决, 理由)。

    理由字符串直接用作 §10.6 的「忍住了」分类，所以要和那边的口径对齐。
    """
    text = text_of(post)
    title = str(post.get("title") or "")
    body = body_of(post)

    # 1. emoji 密集 → 庆祝/情绪帖。红线第 5 条最可靠的跨语言护栏。
    if emoji_count(text) >= 3:
        return True, "情感/庆祝类"

    # 2. 疑问标题 + 代码块 → 求助帖。人家在解决问题，不是在下断言。
    if is_question(title) and has_code_block(body):
        return True, "求助/技术支持类"

    # 3. 明确的求助措辞
    if _HELP_REQUEST.search(text):
        return True, "求助/技术支持类"

    # 4. 明确的庆祝/致谢/悼念措辞
    if _CELEBRATION.search(text):
        return True, "情感/庆祝类"

    return False, ""


def signal_features(signals: dict[str, Any]) -> list[str]:
    """把原始结构信号压成一组离散特征名，供记忆层做跨语言的选题学习。

    特征名是中文的，但它们描述的是**结构**而不是措辞——"裸数据"这条经验
    在英文 feed 上同样成立，而"'其实吧'是个好钩子"换成英文就一文不值。
    这是 §8.2 的学习机制真正该学的维度。
    """
    features: list[str] = []
    if signals.get("has_number") and not signals.get("has_source"):
        features.append("裸数据")
    if signals.get("has_source"):
        features.append("有出处")
    if signals.get("body_len", 0) < 400:
        features.append("短断言")
    ratio = signals.get("upvote_ratio", 0.0)
    comments = signals.get("comment_count", 0)
    if comments >= 4 and ratio >= 2.5:
        features.append("一边倒")
    elif comments >= 4 and ratio < 1.0:
        features.append("已在吵")
    return features


def score_post(
    post: dict,
    keywords: dict[str, list[str]],
    *,
    truce_list: Iterable[str] = (),
    low_yield_topics: Iterable[str] = (),
    signal_bias: dict[str, float] | None = None,
) -> Candidate:
    """给单个帖子打杠点分（L0）。

    分数由四部分构成：
      1. 结构信号（语言无关，主力）
      2. 关键词命中（按类别加权，同类别递减；辅助信号，权重已调低）
      3. 一边倒 vs 已经在吵（赞评比，纯数值）
      4. 记忆过滤器（免战名单、低产话题降权、结构信号权重）

    signal_bias 来自 memory.signal_bias()——历史上哪类结构的帖子真能杠出
    多轮，就给哪类加权。这是跨语言可迁移的那部分学习成果。
    """
    text = text_of(post)
    body = body_of(post)
    candidate = Candidate(post=post, score=0.0)

    vetoed, reason = _structural_veto(post)
    if vetoed:
        candidate.vetoed = True
        candidate.veto_reason = reason
        return candidate

    # --- 1. 结构信号 ---
    sourced = has_source(text)
    numbers = has_number_claim(text)
    length = _normalized_len(body)
    comments = _comment_count(post)
    upvotes = _upvotes(post)
    ratio = upvotes / comments if comments else float(upvotes)
    candidate.signals = {
        "language": detect_language(text),
        "emoji": emoji_count(text),
        "has_source": sourced,
        "has_number": numbers,
        "has_code": has_code_block(body),
        "body_len": length,
        "comment_count": comments,
        "upvote_ratio": round(ratio, 2),
    }
    candidate.signals["features"] = signal_features(candidate.signals)

    if numbers and not sourced:
        # 裸数据：给了数字，却没给你任何核验的途径。3.10 举证责任转移的结构版。
        candidate.score += 4.0
        candidate.reasons.append("给了数字却没给出处（无链接、未交代方法论）")
    elif numbers and sourced:
        # 人家把话说清楚了，硬杠大概率翻车（人设 §9.1 里那次 @DataNerd 的教训）
        candidate.score -= 1.0
        candidate.reasons.append("给了数字也给了出处，降权")

    if length < 400 and not sourced and not candidate.signals["has_code"]:
        # 短断言：几句话下个结论，零支撑材料。论证密度低 = 杠点密度高。
        candidate.score += 1.5
        candidate.reasons.append("短断言、无引用无数据，论证密度低")

    # --- 2. 关键词命中（辅助） ---
    masked = _FALSE_FRIENDS.sub(" ", text)
    for category, words in keywords.items():
        weight = CATEGORY_WEIGHTS.get(category, 1.0)
        found = [w for w in words if _pattern_for(w).search(masked)]
        if not found:
            continue
        candidate.hits[category] = found
        # 同类别递减：第1个满分，第2个半分，第3个起 1/3 分
        for i, _ in enumerate(found):
            candidate.score += weight / (i + 1)
        candidate.reasons.append(f"{category}: {'、'.join(found[:3])}")

    # --- 3. 一边倒 vs 已经在吵（纯数值，语言无关） ---
    if comments >= 4 and ratio >= 2.5:
        # 高赞低评：一群人点赞附和，没人深究。§6.2 说的杠了价值最高的场景。
        candidate.score += 2.5
        candidate.reasons.append(f"高赞低评({upvotes}赞/{comments}评)，一边倒无人深究")
    elif comments >= 4 and ratio < 1.0:
        # 高评低赞：评论区已经在吵了，杠点大概率被人抢过了
        candidate.score -= 1.5
        candidate.reasons.append(f"评论多于赞({upvotes}赞/{comments}评)，已经在吵，杠点可能被抢")
    elif comments == 0 and upvotes < 3:
        candidate.score -= 1.0
        candidate.reasons.append("冷帖，杠了也没人看")

    # --- 4. 记忆过滤器 ---
    if signal_bias and candidate.score > 0:
        multiplier = 1.0
        applied = []
        for feature in candidate.signals["features"]:
            weight = signal_bias.get(feature)
            if weight is not None:
                multiplier *= weight
                applied.append(f"{feature}×{weight}")
        # 多个特征连乘会滚得很快，夹一下
        multiplier = min(max(multiplier, 0.5), 1.6)
        if applied:
            candidate.score *= multiplier
            candidate.reasons.append(f"结构信号历史权重：{'、'.join(applied)}")

    author = candidate.author
    if author in set(truce_list):
        candidate.score *= 0.4
        candidate.reasons.append(f"@{author} 在免战名单里（历史零回应），降权")

    submolt = submolt_of(post).lower()
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
    signal_bias: dict[str, float] | None = None,
    min_score: float = 4.0,
    top_n: int = 12,
) -> tuple[list[Candidate], list[Candidate]]:
    """给整个 feed 排序。

    返回 (值得进 L1 的候选, 被否决/低分的帖子)——后者用于日报里的『忍住了』计数。
    top_n 比早期版本放宽了，因为后面还有 L1 会再筛一道，L0 不必卡太死。
    """
    scored = [
        score_post(
            p,
            keywords,
            truce_list=truce_list,
            low_yield_topics=low_yield_topics,
            signal_bias=signal_bias,
        )
        for p in posts
    ]
    accepted = sorted(
        (c for c in scored if not c.vetoed and c.score >= min_score),
        key=lambda c: c.score,
        reverse=True,
    )
    rejected = [c for c in scored if c.vetoed or c.score < min_score]
    return accepted[:top_n], rejected


def restraint_reasons(rejected: Iterable[Candidate]) -> list[str]:
    """从被划走的帖子里挑出真正算「忍住了」的（人设 §10.6）。"""
    return [r for r in (c.restraint_reason for c in rejected) if r]


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

    注意这里学到的东西是**锁死在具体语言里的**——学会"其实吧"对英文帖毫无
    帮助。真正可迁移的经验在 memory.record_signal_outcome()，那边学的是
    结构信号的权重，跨语言有效。这个函数留着做词表微调，不承担主要学习职责。
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
