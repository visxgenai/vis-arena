"""Bedrock Converse path: OpenAI<->Converse conversion + routing + usage/cost."""

from __future__ import annotations

import json

from vis_arena_server import llm
from vis_arena_server.schemas import LLMMessageRequest


def _payload(messages, tools=None, tool_choice="auto"):
    return LLMMessageRequest(
        job_id="job-1", model="deepseek.v3.2", messages=messages, tools=tools or [], tool_choice=tool_choice
    )


# ---- converters -----------------------------------------------------------

def test_converse_messages_system_pulled_and_first_user():
    system, msgs = llm._converse_messages(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "hello"}]
    )
    assert system == [{"text": "sys"}]
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"] == [{"text": "hello"}]


def test_converse_merges_multiple_tool_results():
    _, msgs = llm._converse_messages([
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
            {"id": "c2", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "out1"},
        {"role": "tool", "tool_call_id": "c2", "content": "out2"},
    ])
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert len(msgs[1]["content"]) == 2 and all("toolUse" in b for b in msgs[1]["content"])
    results = msgs[2]["content"]
    assert [b["toolResult"]["toolUseId"] for b in results] == ["c1", "c2"]
    assert results[0]["toolResult"]["content"] == [{"text": "out1"}]


def test_converse_merges_tool_then_continue_user():
    _, msgs = llm._converse_messages([
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "bash", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "out"},
        {"role": "user", "content": "Continue."},
    ])
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    last = msgs[2]["content"]
    assert "toolResult" in last[0] and last[1] == {"text": "Continue."}


def test_converse_tool_config():
    tools = [{"type": "function", "function": {
        "name": "bash", "description": "d", "parameters": {"type": "object", "properties": {"x": {"type": "string"}}}}}]
    cfg = llm._converse_tool_config(tools, "auto")
    assert cfg["tools"][0]["toolSpec"]["name"] == "bash"
    assert cfg["tools"][0]["toolSpec"]["inputSchema"]["json"] == tools[0]["function"]["parameters"]
    assert "toolChoice" not in cfg  # auto omitted (Converse default)
    assert llm._converse_tool_config([], "auto") is None
    forced = llm._converse_tool_config(tools, {"type": "function", "function": {"name": "bash"}})
    assert forced["toolChoice"] == {"tool": {"name": "bash"}}


def test_openai_message_from_converse():
    msg = llm._openai_message_from_converse([
        {"text": "hi"},
        {"toolUse": {"toolUseId": "tu1", "name": "make_chart", "input": {"kind": "line"}}},
    ])
    assert msg["content"] == "hi"
    assert msg["tool_calls"][0]["id"] == "tu1"
    assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"kind": "line"}
    assert "tool_calls" not in llm._openai_message_from_converse([{"text": "just text"}])


# ---- _invoke_converse (stubbed boto3) + routing ---------------------------

class _FakeConverseClient:
    def __init__(self, resp):
        self._resp = resp
        self.called = None

    def converse(self, **kwargs):
        self.called = ("converse", kwargs)
        return self._resp


def test_invoke_converse_maps_usage_and_model(monkeypatch):
    resp = {
        "output": {"message": {"role": "assistant", "content": [
            {"text": "ok"},
            {"toolUse": {"toolUseId": "tu1", "name": "make_chart", "input": {"kind": "line"}}}]}},
        "usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
    }
    fake = _FakeConverseClient(resp)
    monkeypatch.setattr(llm.boto3, "client", lambda *a, **k: fake)
    out = llm._invoke_converse(_payload([{"role": "user", "content": "hi"}]), 128, "deepseek.v3.2")
    assert out["model"] == "deepseek.v3.2"
    assert out["usage"] == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    assert out["message"]["tool_calls"][0]["id"] == "tu1"
    assert fake.called[0] == "converse"
    # And cost prices by family from the returned model id.
    assert llm._estimated_cost_usd(out["model"], 1_000_000, 0) == 0.58  # deepseek input price


def test_anthropic_id_still_uses_invoke_model(monkeypatch):
    captured = {}

    class _FakeAnthropic:
        def invoke_model(self, **kwargs):
            captured["called"] = "invoke_model"
            body = json.dumps({
                "model": "x", "content": [{"type": "text", "text": "hi"}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }).encode()

            class _Body:
                def read(self_inner):
                    return body

            return {"body": _Body()}

        def converse(self, **kwargs):
            raise AssertionError("anthropic must not use converse")

    monkeypatch.setattr(llm.boto3, "client", lambda *a, **k: _FakeAnthropic())
    model = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
    out = llm._invoke_bedrock(_payload([{"role": "user", "content": "hi"}]), 128, model)
    assert captured["called"] == "invoke_model"
    assert out["usage"]["input_tokens"] == 1
