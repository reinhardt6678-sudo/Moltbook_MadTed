"""大脑的参数拼装测试——不发真实 API 请求。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import brain  # noqa: E402


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
