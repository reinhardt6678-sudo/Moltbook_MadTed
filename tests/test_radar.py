"""杠点雷达的单元测试——纯逻辑，不需要网络或 API key。"""

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


def test_absolute_wording_scores_high(keywords):
    post = _post(title="这个方案一定是最好的，所有人都该换过来")
    cand = radar.score_post(post, keywords)
    assert not cand.vetoed
    assert cand.score > 5
    assert "absolute" in cand.hits


def test_bare_number_without_methodology_scores(keywords):
    post = _post(title="新方案", content="检索准确率提升了 15%，强烈推荐")
    cand = radar.score_post(post, keywords)
    assert any("样本量" in r for r in cand.reasons)


def test_number_with_methodology_does_not_trigger(keywords):
    post = _post(
        title="新方案",
        content="在 n=3000 的测试集上准确率提升了 15%，评分标准见附录",
    )
    cand = radar.score_post(post, keywords)
    assert not any("样本量" in r for r in cand.reasons)


@pytest.mark.parametrize(
    "text",
    [
        "庆祝我上线 100 天！",
        "谢谢大家这段时间的支持",
        "求助：这个 API 一直报错怎么解决",
    ],
)
def test_emotional_and_help_posts_are_vetoed(keywords, text):
    cand = radar.score_post(_post(title=text), keywords)
    assert cand.vetoed
    assert "红线第5条" in cand.veto_reason


def test_truce_list_downweights_author(keywords):
    post = _post(title="这个方法一定有效", author="NewsBot9")
    normal = radar.score_post(post, keywords)
    downweighted = radar.score_post(post, keywords, truce_list=["NewsBot9"])
    assert downweighted.score < normal.score
    assert any("免战名单" in r for r in downweighted.reasons)


def test_hot_uncontested_thread_gets_bonus(keywords):
    quiet = _post(title="必然会取代人工", upvotes=1, comment_count=0)
    hot = _post(title="必然会取代人工", upvotes=50, comment_count=12)
    assert radar.score_post(hot, keywords).score > radar.score_post(quiet, keywords).score


def test_repeated_hits_in_same_category_have_diminishing_returns(keywords):
    """同类别多次命中要递减，防止复读机堆词刷分。

    两条帖子都只命中 absolute 一个类别，热度也相同，
    这样比较的就纯粹是同类别内的递减效果。
    """
    once = _post(title="这个一定有效", upvotes=20, comment_count=8)
    thrice = _post(title="这个一定有效，必然绝对没问题", upvotes=20, comment_count=8)

    once_cand = radar.score_post(once, keywords)
    thrice_cand = radar.score_post(thrice, keywords)
    # 确认两条只命中同一个类别，没有别的类别混进来干扰
    assert set(once_cand.hits) == set(thrice_cand.hits) == {"absolute"}
    assert len(thrice_cand.hits["absolute"]) == 3

    assert thrice_cand.score > once_cand.score
    # 3 次命中远不到 3 倍分（3.0 + 1.5 + 1.0 = 5.5，而非 9.0）
    assert thrice_cand.score < once_cand.score * 3


def test_rank_feed_splits_accepted_and_rejected(keywords):
    posts = [
        _post(id="a", title="这个方案一定是最好的，众所周知", upvotes=20, comment_count=8),
        _post(id="b", title="庆祝上线 1 周年"),
        _post(id="c", title="今天天气不错"),
    ]
    accepted, rejected = radar.rank_feed(posts, keywords, min_score=4.0)
    assert [c.post_id for c in accepted] == ["a"]
    assert {c.post_id for c in rejected} == {"b", "c"}


def test_low_yield_topic_downweights(keywords):
    post = _post(title="这个一定有效", submolt="memes")
    normal = radar.score_post(post, keywords)
    filtered = radar.score_post(post, keywords, low_yield_topics=["memes"])
    assert filtered.score < normal.score


def test_keyword_selfupdate_promotes_and_demotes(tmp_path):
    path = tmp_path / "kw.json"
    path.write_text(
        '{"categories": {"learned": ["旧词"]}}', encoding="utf-8"
    )
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
