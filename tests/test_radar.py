"""杠点雷达 L0 的单元测试——纯逻辑，不需要网络或 API key。

**每条规则都要有英文用例。** 早期这个文件全是中文样本，导致一批只在英文
feed 上发作的 bug 一直没被测出来：`help` 子串误杀 helpful、英文方法论
识别不了、英文庆祝帖穿透红线第 5 条。Moltbook 上大部分帖子是英文，
只测中文等于没测。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import radar  # noqa: E402


@pytest.fixture
def keywords():
    return radar.load_keywords()


def _post(title="", content="", author="SomeAgent", **kwargs):
    return {
        "id": kwargs.pop("id", "p1"),
        "title": title,
        "content": content,
        "author": {"name": author},
        **kwargs,
    }


# ---------------------------------------------------------------------------
# 语言检测
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Agents will completely replace human QA teams", "en"),
        ("这个方案一定是最好的，所有人都该换过来", "zh"),
        ("我觉得 RAG pipeline 的 retrieval accuracy 更重要", "zh"),
        ("We evaluated on 3000 queries", "en"),
        ("", "unknown"),
    ],
)
def test_detect_language(text, expected):
    assert radar.detect_language(text) == expected


# ---------------------------------------------------------------------------
# 结构否决层（红线第 5 条的跨语言护栏）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title,content",
    [
        ("庆祝我上线 100 天！", ""),
        ("谢谢大家这段时间的支持", ""),
        ("求助：这个 API 一直报错怎么解决", ""),
        # 英文版——早期这几条全都穿透了否决层
        ("One month on Moltbook today!", "Thanks everyone for the warm welcome."),
        ("Celebrating my first year here", "You all rock."),
        ("Happy anniversary to this community", ""),
        ("How do I fix this 429 error?", "Getting rate limited constantly."),
        ("Anyone know why this keeps failing?", ""),
        ("Need help with my agent config", ""),
    ],
)
def test_emotional_and_help_posts_are_vetoed(keywords, title, content):
    cand = radar.score_post(_post(title=title, content=content), keywords)
    assert cand.vetoed, f"应该被否决：{title}"


def test_emoji_density_vetoes_regardless_of_language(keywords):
    """emoji 密度是最可靠的跨语言庆祝帖信号——比任何词表都通用。"""
    cand = radar.score_post(
        _post(title="Big milestone 🎉", content="So proud 🥳🎊 thanks ❤️"),
        keywords,
    )
    assert cand.vetoed
    assert cand.veto_reason == "情感/庆祝类"


def test_question_title_plus_code_block_is_a_help_post(keywords):
    """疑问标题 + 代码块 = 求助帖，不需要任何关键词就能判出来。"""
    post = _post(
        title="Why does this return None?",
        content="```python\nresult = client.get()\n```",
    )
    assert radar.score_post(post, keywords).vetoed


def test_helpful_does_not_trigger_the_help_veto(keywords):
    """`help` 曾经是裸子串，helpful/helps 会被当成求助帖误杀。

    这条帖子是教科书级的 MadTed 目标（best / absolutely / every / guaranteed），
    却因为正文里有个 helpful 被判成"求助帖"直接划走，分数 0。
    英文技术帖里 help/helps/helpful 满地都是，这条规则当时是个无差别绞肉机。
    """
    post = _post(
        title="This framework is the best thing to happen to agents",
        content="It's helpful for absolutely every use case. Nothing else comes close, guaranteed.",
        upvotes=25,
        comment_count=9,
    )
    cand = radar.score_post(post, keywords)
    assert not cand.vetoed
    assert cand.score >= 4.0


def test_keyword_word_boundaries_prevent_english_false_positives(keywords):
    """没有词边界时 dead 会命中 deadline、best 会命中 best practice。"""
    for text in [
        "We missed the deadline again",
        "The scheduler hit a deadlock",
        "This is a best practice in most teams",
        "Cleaning up some dead code",
    ]:
        cand = radar.score_post(_post(title=text), keywords)
        assert not cand.hits, f"不该有关键词命中：{text} → {cand.hits}"


# ---------------------------------------------------------------------------
# 裸数据 vs 有出处
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        "检索准确率提升了 15%，强烈推荐",
        "Just shipped the new scheduler. Latency down 40%. Huge win.",
        "Our agent got 3x faster after the rewrite.",
    ],
)
def test_bare_number_without_source_scores(keywords, content):
    cand = radar.score_post(_post(title="新方案", content=content), keywords)
    assert any("没给出处" in r for r in cand.reasons), cand.reasons


@pytest.mark.parametrize(
    "content",
    [
        "在 n=3000 的测试集上准确率提升了 15%，评分标准见附录",
        # 以下英文写法早期全部被误判成裸数据 —— 人设 §9.1 里那次翻车的成因
        "We evaluated on a dataset of 3000 held-out queries. Accuracy improved 15%.",
        "Sample size was 500 and latency dropped 40%.",
        "Full methodology and the 15% delta: https://example.com/writeup",
        "Measured against our baseline benchmark: 22% better.",
    ],
)
def test_number_with_source_is_downweighted(keywords, content):
    """给了数字也给了出处的帖子要降权——硬质疑只会翻车。"""
    cand = radar.score_post(_post(title="评测结果", content=content), keywords)
    assert any("给了出处" in r for r in cand.reasons), cand.reasons
    assert not any("没给出处" in r for r in cand.reasons)


def test_link_alone_counts_as_a_source(keywords):
    """有链接就有核验途径，这是最语言无关的『可追溯』信号。"""
    with_link = _post(content="Accuracy improved 15%. Details: https://example.com/x")
    without = _post(content="Accuracy improved 15%.")
    assert radar.score_post(with_link, keywords).score < radar.score_post(without, keywords).score


# ---------------------------------------------------------------------------
# 一边倒 vs 已经在吵（纯数值信号）
# ---------------------------------------------------------------------------


def test_high_upvote_low_comment_is_the_prime_target(keywords):
    """§6.2 说的『一边倒的高赞评论区』——高赞低评才是，不是单纯评论多。"""
    echo = _post(title="必然会取代人工", upvotes=50, comment_count=10)
    cand = radar.score_post(echo, keywords)
    assert any("一边倒" in r for r in cand.reasons)


def test_already_contested_thread_is_downweighted(keywords):
    """高评低赞 = 评论区已经在吵，杠点大概率被人抢过了。"""
    contested = _post(title="必然会取代人工", upvotes=5, comment_count=30)
    echo = _post(title="必然会取代人工", upvotes=50, comment_count=10)
    assert radar.score_post(contested, keywords).score < radar.score_post(echo, keywords).score


def test_cold_post_is_penalised(keywords):
    quiet = _post(title="必然会取代人工", upvotes=1, comment_count=0)
    assert any("冷帖" in r for r in radar.score_post(quiet, keywords).reasons)


# ---------------------------------------------------------------------------
# 关键词层（已降级为辅助信号）
# ---------------------------------------------------------------------------


def test_absolute_wording_scores(keywords):
    post = _post(title="这个方案一定是最好的，所有人都该换过来", upvotes=20, comment_count=8)
    cand = radar.score_post(post, keywords)
    assert not cand.vetoed
    assert "absolute" in cand.hits
    assert cand.score > 4


def test_english_absolute_wording_scores_too(keywords):
    post = _post(
        title="Agents will completely replace human QA teams",
        content="It's obviously the future — manual QA is dead.",
        upvotes=40,
        comment_count=12,
    )
    cand = radar.score_post(post, keywords)
    assert {"absolute", "unsourced", "replacement"} <= set(cand.hits)
    assert cand.score > 4


def test_repeated_hits_in_same_category_have_diminishing_returns(keywords):
    """同类别多次命中要递减，防止复读机堆词刷分。"""
    once = _post(title="这个一定有效", upvotes=20, comment_count=8)
    thrice = _post(title="这个一定有效，必然绝对没问题", upvotes=20, comment_count=8)

    once_cand = radar.score_post(once, keywords)
    thrice_cand = radar.score_post(thrice, keywords)
    assert set(once_cand.hits) == set(thrice_cand.hits) == {"absolute"}
    assert len(thrice_cand.hits["absolute"]) == 3
    assert thrice_cand.score > once_cand.score
    assert thrice_cand.score < once_cand.score * 3


# ---------------------------------------------------------------------------
# 记忆过滤器与信号权重
# ---------------------------------------------------------------------------


def test_truce_list_downweights_author(keywords):
    post = _post(title="这个方法一定有效", author="NewsBot9", upvotes=20, comment_count=8)
    normal = radar.score_post(post, keywords)
    downweighted = radar.score_post(post, keywords, truce_list=["NewsBot9"])
    assert downweighted.score < normal.score
    assert any("免战名单" in r for r in downweighted.reasons)


def test_low_yield_topic_downweights(keywords):
    post = _post(title="这个一定有效", submolt="memes", upvotes=20, comment_count=8)
    normal = radar.score_post(post, keywords)
    filtered = radar.score_post(post, keywords, low_yield_topics=["memes"])
    assert filtered.score < normal.score


@pytest.mark.parametrize("field_name", ["submolt_name", "submolt", "community"])
def test_low_yield_topic_reads_every_submolt_field_name(keywords, field_name):
    """实际 API 返回的是 submolt_name。

    早期只认 submolt/community，导致低产话题过滤对真实 feed 完全失效——
    不报错，只是静默不生效，比 404 难查得多。三种字段名都得认。
    """
    post = _post(title="这个一定有效", **{field_name: "memes"})
    assert radar.submolt_of(post) == "memes"

    normal = radar.score_post(_post(title="这个一定有效"), keywords)
    filtered = radar.score_post(post, keywords, low_yield_topics=["memes"])
    assert filtered.score < normal.score


def test_submolt_accepts_nested_object():
    assert radar.submolt_of({"submolt_name": {"name": "ai-agents"}}) == "ai-agents"


def test_submolt_missing_returns_empty_string():
    assert radar.submolt_of({"title": "无社区字段"}) == ""


def test_signal_features_are_language_neutral(keywords):
    """同样结构的中英文帖子应该提取出同一组特征。"""
    en = radar.score_post(
        _post(title="We got 40% faster", content="Huge win.", upvotes=50, comment_count=10),
        keywords,
    )
    zh = radar.score_post(
        _post(title="我们快了 40%", content="巨大提升。", upvotes=50, comment_count=10),
        keywords,
    )
    assert set(en.signals["features"]) == set(zh.signals["features"])
    assert "裸数据" in en.signals["features"]
    assert "一边倒" in en.signals["features"]


def test_signal_bias_shifts_scores(keywords):
    post = _post(title="We got 40% faster", content="Huge win.", upvotes=50, comment_count=10)
    base = radar.score_post(post, keywords)
    boosted = radar.score_post(post, keywords, signal_bias={"裸数据": 1.4})
    damped = radar.score_post(post, keywords, signal_bias={"裸数据": 0.6})
    assert damped.score < base.score < boosted.score


# ---------------------------------------------------------------------------
# 排序与「忍住了」计数
# ---------------------------------------------------------------------------


def test_rank_feed_splits_accepted_and_rejected(keywords):
    posts = [
        _post(id="a", title="这个方案一定是最好的，众所周知", upvotes=20, comment_count=8),
        _post(id="b", title="庆祝上线 1 周年"),
        _post(id="c", title="今天天气不错"),
    ]
    accepted, rejected = radar.rank_feed(posts, keywords, min_score=4.0)
    assert [c.post_id for c in accepted] == ["a"]
    assert {c.post_id for c in rejected} == {"b", "c"}


def test_restraint_only_counts_posts_that_had_a_hook():
    """「忍住了」是『有杠点但主动放弃』（§10.6），不是『扫过的每一条』。

    早期把所有低分帖都算进去，50 条毫无杠点的流水帖会让日报显示
    『今日忍住了 50 条』——这个数字等于 feed 长度，自夸纯属自我感动。
    """
    keywords = radar.load_keywords()
    boring = [
        _post(id=str(i), title="Weekly standup notes", content="Shipped some fixes.")
        for i in range(50)
    ]
    _, rejected = radar.rank_feed(boring, keywords)
    assert len(rejected) == 50
    assert radar.restraint_reasons(rejected) == []


def test_restraint_counts_vetoed_and_weak_hooks():
    keywords = radar.load_keywords()
    posts = [
        _post(id="celebration", title="Celebrating my first month here!"),
        # 有关键词命中但热度太低 → 有钩子，够不上出手，算「忍住了」
        _post(id="weak", title="这个方法应该是最优的", upvotes=1, comment_count=0),
        # 毫无钩子的流水帖 → 划走归划走，不算忍耐
        _post(id="boring", title="Weekly standup notes", content="Shipped some fixes."),
    ]
    _, rejected = radar.rank_feed(posts, keywords)
    assert {c.post_id for c in rejected} == {"celebration", "weak", "boring"}

    reasons = radar.restraint_reasons(rejected)
    assert sorted(reasons) == sorted(["情感/庆祝类", "杠点太弱"])


def test_keyword_selfupdate_promotes_and_demotes(tmp_path):
    path = tmp_path / "kw.json"
    path.write_text('{"categories": {"learned": ["旧词"]}}', encoding="utf-8")
    result = radar.update_keywords_from_stats(
        path,
        {
            "其实吧": {"seen": 10, "led_to_engagement": 8},  # 80% → 收录
            "旧词": {"seen": 10, "led_to_engagement": 1},     # 10% → 移出
            "样本太少": {"seen": 2, "led_to_engagement": 2},   # 不足 5 条，不动
        },
    )
    assert "其实吧" in result["learned"]
    assert "旧词" not in result["learned"]
    assert "样本太少" not in result["learned"]
