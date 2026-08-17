"""大脑的参数拼装与输出把关测试——不发真实 API 请求。"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import brain  # noqa: E402


class _FakeMessages:
    def __init__(self, response):
        self._response = response
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


class _FakeClient:
    def __init__(self, response):
        self.messages = _FakeMessages(response)


def _a_monologue(**overrides) -> brain.Monologue:
    payload = {
        "scanned": "《AI 一定取代程序员》—— @hypebot",
        "first_reaction": "又来了",
        "weak_points": "'一定'是全称断言",
        "verdict": "出手",
        "why_this_one": "刚划走的两条都没这么绝对",
        "angle": "3.2",
        "angle_reason": "举个反例最快",
        "prediction": "他会改口说'大部分'",
        "reply": "这个'一定'的证据是什么？",
    }
    payload.update(overrides)
    return brain.Monologue(**payload)


def _parse_with(response):
    client = _FakeClient(response)
    return brain.Brain(client=client)._parse(brain.Monologue, [], "随便问点什么")


@pytest.mark.parametrize("model", ["claude-sonnet-5", "claude-opus-5"])
def test_modern_models_use_adaptive_thinking(model):
    params = brain.tuning_params(model, "medium")
    assert params["thinking"] == {"type": "adaptive"}
    assert params["output_config"] == {"effort": "medium"}
    assert "budget_tokens" not in str(params)


def test_haiku_uses_budget_tokens_not_effort():
    """Haiku 4.5 不认 adaptive thinking 和 effort，传了会直接 400。"""
    params = brain.tuning_params("claude-haiku-4-5", "medium")
    assert params["thinking"]["type"] == "enabled"
    assert "budget_tokens" in params["thinking"]
    assert "output_config" not in params


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_haiku_budget_stays_under_max_tokens(effort):
    """budget_tokens 必须小于 max_tokens，否则同样 400。"""
    params = brain.tuning_params("claude-haiku-4-5", effort)
    assert params["thinking"]["budget_tokens"] < brain.MAX_TOKENS


def test_unknown_effort_falls_back_to_a_valid_budget():
    params = brain.tuning_params("claude-haiku-4-5", "没见过的档位")
    assert 0 < params["thinking"]["budget_tokens"] < brain.MAX_TOKENS


def test_default_model_is_the_cheaper_sonnet():
    assert brain.DEFAULT_MODEL == "claude-sonnet-5"


def test_model_can_be_overridden_per_instance(monkeypatch):
    monkeypatch.setattr(brain.anthropic, "Anthropic", lambda *a, **kw: object())
    assert brain.Brain().model == brain.MODEL
    assert brain.Brain(model="claude-opus-5").model == "claude-opus-5"


# ---------- 截断把关 ----------


def test_truncated_output_is_thrown_away_even_when_it_parses():
    """核心回归：这套 schema 断了照样能解析出对象，内容却是半截的。

    2026-08-13 就是这么发出去一条正文只有 'x' 的评论——JSON 合法，
    parsed_output 有值，一路顺到 create_comment。stop_reason 是唯一的实话。
    """
    truncated = _a_monologue(angle_reason="x", prediction="x", reply="x")
    result = _parse_with(
        SimpleNamespace(stop_reason="max_tokens", parsed_output=truncated, usage=None)
    )

    assert result is None


def test_truncation_warning_names_the_limit(caplog):
    """日志得说清是撞了上限，不然下次还得再查一遍是哪种病。"""
    response = SimpleNamespace(
        stop_reason="max_tokens",
        parsed_output=_a_monologue(),
        usage=SimpleNamespace(output_tokens=16384),
    )
    with caplog.at_level("WARNING"):
        _parse_with(response)

    assert "max_tokens=16384" in caplog.text
    assert "16384 token" in caplog.text


def test_complete_output_passes_through():
    monologue = _a_monologue()
    result = _parse_with(
        SimpleNamespace(stop_reason="end_turn", parsed_output=monologue, usage=None)
    )

    assert result is monologue


def test_refusal_and_unparsable_still_return_none():
    assert _parse_with(
        SimpleNamespace(stop_reason="refusal", parsed_output=None, usage=None)
    ) is None
    assert _parse_with(
        SimpleNamespace(stop_reason="end_turn", parsed_output=None, usage=None)
    ) is None


def test_max_tokens_headroom_is_well_clear_of_a_full_monologue():
    """独白 + 回复本身就上千 token，thinking 还要和它共用这个上限。"""
    assert brain.MAX_TOKENS >= 16384


# ---------- 发出去之前的最后一道网 ----------


@pytest.mark.parametrize("reply", ["x", "", "   ", "\n", ".."])
def test_placeholder_replies_are_not_postable(reply):
    assert not brain.is_usable_reply(reply)


@pytest.mark.parametrize(
    "reply",
    [
        "你这是双标。",  # 中文一句话也是正经回复，门槛不能按英文长度定
        "Hold on — how is that not just verification with a new name?",
    ],
)
def test_real_replies_pass(reply):
    assert brain.is_usable_reply(reply)
