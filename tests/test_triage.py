"""L1 语义粗筛层的测试——用假客户端，不发真实 API 请求。"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import radar  # noqa: E402
import triage  # noqa: E402


def _candidate(title="Some claim", content="", score=5.0, **post_kwargs):
    post = {
        "id": post_kwargs.pop("id", "p1"),
        "title": title,
        "content": content,
        "author": {"name": "SomeAgent"},
        **post_kwargs,
    }
    return radar.Candidate(post=post, score=score)


class _FakeMessages:
    """假的 client.messages，按脚本返回 parse 结果。"""

    def __init__(self, script):
        self._script = list(script)
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _FakeClient:
    def __init__(self, script):
        self.messages = _FakeMessages(script)


def _ok(verdicts):
    return SimpleNamespace(
        stop_reason="end_turn",
        parsed_output=triage.TriageBatch(verdicts=verdicts),
    )


def _verdict(index, *, score=7, redline=False, defect="3.10", note="有货"):
    return triage.TriageVerdict(
        index=index,
        is_redline=redline,
        hook_score=score,
        defect=defect,
        angle=defect,
        note=note,
    )


def test_ranks_by_semantic_score_first():
    """L1 看得懂内容，所以它的判断压过 L0 的结构分。"""
    cands = [_candidate(id="a", score=9.0), _candidate(id="b", score=1.0)]
    client = _FakeClient([_ok([_verdict(0, score=2), _verdict(1, score=9)])])

    ranked = triage.Triage(client=client).rank(cands)

    assert [s.candidate.post_id for s in ranked] == ["b", "a"]


def test_l0_score_breaks_ties():
    cands = [_candidate(id="a", score=2.0), _candidate(id="b", score=8.0)]
    client = _FakeClient([_ok([_verdict(0, score=6), _verdict(1, score=6)])])

    ranked = triage.Triage(client=client).rank(cands)

    assert [s.candidate.post_id for s in ranked] == ["b", "a"]


def test_batches_are_chunked():
    cands = [_candidate(id=str(i)) for i in range(5)]
    client = _FakeClient(
        [_ok([_verdict(i) for i in range(2)]) for _ in range(3)]
    )

    triage.Triage(client=client, batch_size=2).rank(cands)

    assert len(client.messages.calls) == 3


def test_api_failure_falls_back_to_l0_order():
    """粗筛挂了不能拖垮整轮 heartbeat，退化成 L0 排序继续走。"""
    cands = [_candidate(id="a", score=3.0), _candidate(id="b", score=9.0)]
    error = anthropic.APIStatusError(
        "boom",
        response=httpx.Response(
            500, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        ),
        body=None,
    )
    client = _FakeClient([error])

    ranked = triage.Triage(client=client).rank(cands)

    assert [s.candidate.post_id for s in ranked] == ["b", "a"]
    assert all(s.verdict is None for s in ranked)


def test_refusal_falls_back_to_l0_order():
    cands = [_candidate(id="a", score=9.0)]
    client = _FakeClient([SimpleNamespace(stop_reason="refusal", parsed_output=None)])

    ranked = triage.Triage(client=client).rank(cands)

    assert ranked[0].verdict is None
    assert ranked[0].combined_score == 9.0


def test_out_of_range_indices_are_ignored():
    """模型偶尔会编造 index，越界的一律丢掉，不能错配到别的帖子上。"""
    cands = [_candidate(id="a"), _candidate(id="b")]
    client = _FakeClient([_ok([_verdict(0), _verdict(99), _verdict(-1)])])

    ranked = triage.Triage(client=client).rank(cands)
    by_id = {s.candidate.post_id: s for s in ranked}

    assert by_id["a"].verdict is not None
    assert by_id["b"].verdict is None  # 漏判的退回 L0，不会拿到别人的判定


def test_missing_verdicts_do_not_drop_candidates():
    cands = [_candidate(id=str(i)) for i in range(4)]
    client = _FakeClient([_ok([_verdict(0), _verdict(2)])])

    ranked = triage.Triage(client=client).rank(cands)

    assert len(ranked) == 4


def test_split_redlines_removes_redline_posts():
    cands = [_candidate(id="ok"), _candidate(id="party")]
    client = _FakeClient([_ok([_verdict(0, score=8), _verdict(1, redline=True, score=0)])])

    ranked = triage.Triage(client=client).rank(cands)
    passable, restraint = triage.split_redlines(ranked)

    assert [s.candidate.post_id for s in passable] == ["ok"]
    assert restraint == ["情感/庆祝类"]


def test_split_redlines_drops_hookless_posts():
    cands = [_candidate(id="ok"), _candidate(id="dull")]
    client = _FakeClient([_ok([_verdict(0, score=7), _verdict(1, score=1)])])

    ranked = triage.Triage(client=client).rank(cands)
    passable, restraint = triage.split_redlines(ranked)

    assert [s.candidate.post_id for s in passable] == ["ok"]
    assert restraint == ["杠点太弱"]


def test_split_redlines_keeps_unjudged_candidates():
    """粗筛失败退回 L0 时，不能把候选全当成红线剔掉。"""
    cands = [_candidate(id="a"), _candidate(id="b")]
    client = _FakeClient([SimpleNamespace(stop_reason="end_turn", parsed_output=None)])

    passable, restraint = triage.split_redlines(triage.Triage(client=client).rank(cands))

    assert len(passable) == 2
    assert restraint == []


def test_empty_input_makes_no_api_call():
    client = _FakeClient([])
    assert triage.Triage(client=client).rank([]) == []
    assert client.messages.calls == []


def test_default_model_is_the_cheap_one():
    assert triage.DEFAULT_TRIAGE_MODEL == "claude-haiku-4-5"


def test_haiku_gets_budget_tokens_not_effort():
    """Haiku 4.5 不认 effort，传了直接 400。"""
    cands = [_candidate()]
    client = _FakeClient([_ok([_verdict(0)])])

    triage.Triage(client=client).rank(cands)

    params = client.messages.calls[0]
    assert params["thinking"]["type"] == "enabled"
    assert params["thinking"]["budget_tokens"] < triage.MAX_TOKENS
    assert "output_config" not in params


def test_rubric_is_language_neutral():
    """rubric 必须讲『论证结构』而不是『关键词』，否则又退回词表思路。"""
    assert "任何语言" in triage.RUBRIC
    assert "论证结构" in triage.RUBRIC
    # 工具箱的每一招都要在缺陷清单里有对应条目
    for code in ["3.1", "3.2", "3.4", "3.5", "3.10"]:
        assert code in triage.DEFECTS


def test_rubric_is_not_cached():
    """Haiku 4.5 最小可缓存前缀是 4096 token，rubric 到不了，加了也是静默不缓存。"""
    cands = [_candidate()]
    client = _FakeClient([_ok([_verdict(0)])])

    triage.Triage(client=client).rank(cands)

    assert "cache_control" not in str(client.messages.calls[0])


@pytest.mark.parametrize("score", [-1, 11])
def test_hook_score_is_bounded(score):
    with pytest.raises(Exception):
        triage.TriageVerdict(
            index=0, is_redline=False, hook_score=score, defect="3.1", angle="3.1", note=""
        )
