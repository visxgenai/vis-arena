"""Broker tool_choice compatibility: OpenAI and Anthropic dialects.

Participant agents are written against the OpenAI client shape locally, then run
against the Bedrock broker in the cloud. These tests pin the translation for every
shape observed in real traffic (see trajectory scan, Jul 22) plus the OpenAI forms
the local path accepts — so currently-working bundles stay byte-identical and the
OpenAI forms that used to 502 now translate.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from vis_arena_server.llm import _anthropic_body, _converse_tool_config, _normalize_tool_choice

TOOL = {
    "type": "function",
    "function": {"name": "finish", "description": "done", "parameters": {"type": "object", "properties": {}}},
}
MESSAGES = [{"role": "user", "content": "hi"}]


def anthropic_choice(tool_choice):
    return _anthropic_body(MESSAGES, [TOOL], tool_choice, 100).get("tool_choice")


# --- InvokeModel (Anthropic) path: observed-working shapes stay byte-identical ---

def test_auto_unchanged():
    assert anthropic_choice("auto") == {"type": "auto"}


def test_none_value_omitted():
    assert anthropic_choice(None) is None


def test_anthropic_tool_shape_unchanged():
    # The pre-fix workaround (wink's + hliang's patched bundles) must keep working as-is.
    assert anthropic_choice({"type": "tool", "name": "finish"}) == {"type": "tool", "name": "finish"}


# --- InvokeModel path: OpenAI shapes that used to 502 now translate ---

def test_openai_function_shape_translates():
    assert anthropic_choice({"type": "function", "function": {"name": "finish"}}) == {
        "type": "tool",
        "name": "finish",
    }


def test_openai_required_translates():
    assert anthropic_choice("required") == {"type": "any"}


def test_openai_none_string_translates():
    assert anthropic_choice("none") == {"type": "none"}


def test_bare_name_translates():
    assert anthropic_choice({"name": "finish"}) == {"type": "tool", "name": "finish"}


def test_anthropic_auto_dict_translates():
    assert anthropic_choice({"type": "auto"}) == {"type": "auto"}


# --- garbage: explicit 400, but only when tools are in play ---

def test_garbage_raises_400():
    with pytest.raises(HTTPException) as err:
        anthropic_choice({"type": "bogus"})
    assert err.value.status_code == 400
    assert "tool_choice" in err.value.detail


def test_garbage_without_tools_ignored():
    body = _anthropic_body(MESSAGES, [], {"type": "bogus"}, 100)
    assert "tools" not in body and "tool_choice" not in body


# --- Converse (non-Anthropic) path ---

def converse_choice(tool_choice):
    config = _converse_tool_config([TOOL], tool_choice)
    return None if config is None else config.get("toolChoice")


def test_converse_auto_omitted_unchanged():
    assert converse_choice("auto") is None
    assert converse_choice(None) is None


def test_converse_tool_shapes_translate():
    expected = {"tool": {"name": "finish"}}
    assert converse_choice({"type": "tool", "name": "finish"}) == expected
    assert converse_choice({"type": "function", "function": {"name": "finish"}}) == expected


def test_converse_required_translates():
    assert converse_choice("required") == {"any": {}}


def test_converse_none_drops_tools():
    assert _converse_tool_config([TOOL], "none") is None


def test_converse_no_tools_ignores_garbage():
    assert _converse_tool_config([], {"type": "bogus"}) is None


# --- normalizer edge coverage ---

def test_normalizer_empty_string_is_none():
    assert _normalize_tool_choice("") is None


def test_normalizer_rejects_non_dict_non_string():
    with pytest.raises(HTTPException):
        _normalize_tool_choice(123)
