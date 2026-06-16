"""Model-aware per-call cost estimation."""

from __future__ import annotations

from vis_arena_server import llm


def test_model_prices_by_family() -> None:
    assert llm._model_prices("global.anthropic.claude-opus-4-8") == (15.0, 75.0)
    assert llm._model_prices("global.anthropic.claude-haiku-4-5-20251001-v1:0") == (1.0, 5.0)
    assert llm._model_prices("global.anthropic.claude-sonnet-4-5-20250929-v1:0") == (3.0, 15.0)
    assert llm._model_prices("deepseek.v3.2") == (0.58, 1.68)
    assert llm._model_prices("moonshotai.kimi-k2.5") == (0.60, 3.00)


def test_model_prices_unknown_falls_back_to_global() -> None:
    assert llm._model_prices("some.unknown.model") == (
        llm.settings.llm_input_usd_per_1m,
        llm.settings.llm_output_usd_per_1m,
    )


def test_estimated_cost_math() -> None:
    # 1M input tokens on haiku ($1/1M in), no output -> $1.00
    assert llm._estimated_cost_usd("global.anthropic.claude-haiku-4-5-20251001-v1:0", 1_000_000, 0) == 1.0
    # 1M in + 1M out on opus -> $15 + $75
    assert llm._estimated_cost_usd("global.anthropic.claude-opus-4-8", 1_000_000, 1_000_000) == 90.0


def test_estimated_cost_none_when_unpriced() -> None:
    # Unknown model + default-zero global prices -> None (not 0.0).
    assert llm._estimated_cost_usd("unknown.model", 1000, 1000) is None


def test_env_override_merges(monkeypatch) -> None:
    monkeypatch.setenv("VIS_ARENA_MODEL_PRICES", '{"haiku": [2.0, 6.0], "newfam": [0.1, 0.2]}')
    prices = llm._load_model_prices()
    assert prices["haiku"] == (2.0, 6.0)
    assert prices["newfam"] == (0.1, 0.2)
    assert prices["opus"] == (15.0, 75.0)  # untouched default
