"""Broker image support: OpenAI-style image parts -> Anthropic image blocks."""
from __future__ import annotations

import base64

import pytest
from fastapi import HTTPException

from vis_arena_server.llm import (
    MAX_IMAGE_B64_CHARS,
    _anthropic_body,
    _content_blocks,
    _converse_text,
    _truncate,
)

PNG_B64 = base64.b64encode(b"\x89PNG fake image bytes").decode()
IMAGE_PART = {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG_B64}"}}


def test_plain_string_content_unchanged():
    assert _content_blocks("hello") == [{"type": "text", "text": "hello"}]
    assert _content_blocks(None) == [{"type": "text", "text": ""}]


def test_image_part_becomes_anthropic_block():
    blocks = _content_blocks([{"type": "text", "text": "look:"}, IMAGE_PART])
    assert blocks[0] == {"type": "text", "text": "look:"}
    assert blocks[1]["type"] == "image"
    assert blocks[1]["source"]["media_type"] == "image/png"
    assert blocks[1]["source"]["data"] == PNG_B64


def test_jpg_media_type_normalized():
    part = {"type": "image_url", "image_url": {"url": f"data:image/jpg;base64,{PNG_B64}"}}
    assert _content_blocks([part])[0]["source"]["media_type"] == "image/jpeg"


def test_non_data_url_rejected():
    with pytest.raises(HTTPException) as err:
        _content_blocks([{"type": "image_url", "image_url": {"url": "https://example.com/x.png"}}])
    assert err.value.status_code == 400


def test_oversized_image_rejected():
    huge = {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * (MAX_IMAGE_B64_CHARS + 8)}}
    with pytest.raises(HTTPException) as err:
        _content_blocks([huge])
    assert err.value.status_code == 400


def test_user_message_with_image_reaches_body():
    body = _anthropic_body([{"role": "user", "content": [{"type": "text", "text": "hi"}, IMAGE_PART]}], [], None, 100)
    content = body["messages"][0]["content"]
    assert [b["type"] for b in content] == ["text", "image"]


def test_tool_result_with_image_reaches_body():
    messages = [
        {"role": "user", "content": "go"},
        {"role": "tool", "tool_call_id": "t1", "content": [{"type": "text", "text": "stats"}, IMAGE_PART]},
    ]
    body = _anthropic_body(messages, [], None, 100)
    result = body["messages"][1]["content"][0]
    assert result["type"] == "tool_result"
    assert [b["type"] for b in result["content"]] == ["text", "image"]


def test_tool_result_string_content_unchanged():
    messages = [{"role": "tool", "tool_call_id": "t1", "content": "plain output"}]
    body = _anthropic_body(messages, [], None, 100)
    assert body["messages"][0]["content"][0]["content"] == "plain output"


def test_converse_path_omits_images():
    text = _converse_text([{"type": "text", "text": "a"}, IMAGE_PART])
    assert "image omitted" in text
    assert PNG_B64 not in text


def test_trajectory_never_stores_image_data():
    truncated = _truncate([{"role": "user", "content": [IMAGE_PART]}])
    flat = str(truncated)
    assert PNG_B64 not in flat
    assert "image data omitted" in flat
