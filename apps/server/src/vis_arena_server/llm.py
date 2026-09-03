from __future__ import annotations

import json
import os
import re
import time
import uuid
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException

from .db import connect, now_iso, row_to_dict
from .schemas import LLMMessageRequest
from .settings import settings
from .trajectory import append_broker_event, stable_event_key

# Per-field cap on trajectory string values. Generous so prompts / generated code / reasoning
# aren't clipped, but bounded so a runaway tool output can't blow up the trace.
MAX_TRAJECTORY_VALUE_CHARS = 50000



def create_llm_message(payload: LLMMessageRequest, user_id: str) -> dict[str, Any]:
    if not settings.cloud_llm_enabled:
        raise HTTPException(
            status_code=403,
            detail="Cloud LLM brokerage is disabled in this deployment. Use your own provider keys for local testing.",
        )
    if settings.llm_provider != "bedrock":
        raise HTTPException(status_code=400, detail="Cloud message brokerage is only configured for Bedrock")

    context = _job_context(payload.job_id, user_id)
    # Budget is per job (= per dataset run): a submission fans out to one generation
    # job per public dataset, so each dataset gets its own full token budget.
    budget = budget_for_job(context)
    used_tokens = _job_token_total(payload.job_id)
    if used_tokens >= budget:
        raise HTTPException(status_code=429, detail="Job LLM token budget exhausted")

    remaining_tokens = budget - used_tokens
    max_tokens = max(1, min(payload.max_tokens, remaining_tokens))
    model_id = _resolve_bedrock_model(payload.model)
    _record_llm_request_trajectory(payload, context, model_id, max_tokens, remaining_tokens)
    started = time.monotonic()
    try:
        bedrock = _invoke_bedrock(payload, max_tokens, model_id)
    except HTTPException as exc:
        append_broker_event(
            payload.job_id,
            payload.purpose,
            {
                "type": "llm_error",
                "provider": "bedrock",
                "model": model_id,
                "detail": str(exc.detail),
            },
        )
        raise
    latency_ms = int((time.monotonic() - started) * 1000)

    usage = bedrock["usage"]
    total_tokens = usage["input_tokens"] + usage["output_tokens"]
    estimated_cost = _estimated_cost_usd(bedrock["model"], usage["input_tokens"], usage["output_tokens"])
    _record_usage(
        job_id=payload.job_id,
        submission_id=context["submission_id"],
        user_id=user_id,
        provider="bedrock",
        model_id=bedrock["model"],
        purpose=payload.purpose,
        input_tokens=usage["input_tokens"],
        output_tokens=usage["output_tokens"],
        total_tokens=total_tokens,
        estimated_cost_usd=estimated_cost,
        latency_ms=latency_ms,
    )
    _record_llm_response_trajectory(payload, bedrock, latency_ms)

    return {
        "provider": "bedrock",
        "model": bedrock["model"],
        "message": bedrock["message"],
        "usage": usage,
        # Field name kept for SDK/agent compatibility; value is the per-job remaining budget.
        "remaining_submission_tokens": max(0, remaining_tokens - total_tokens),
    }


def budget_for_job(context: dict[str, Any]) -> int:
    """Token budget for this job. Central-judge evaluations may have their own,
    larger allowance; everything else uses the participant budget."""
    if (context.get("job_type") or "generation") == "central_evaluation":
        return settings.llm_max_tokens_per_central_eval or settings.llm_max_tokens_per_job
    return settings.llm_max_tokens_per_job


def _job_context(job_id: str, user_id: str) -> dict[str, Any]:
    with connect() as db:
        row = db.execute(
            """
            select jobs.id, jobs.submission_id, jobs.status, jobs.job_type, submissions.owner_id
            from jobs join submissions on submissions.id = jobs.submission_id
            where jobs.id = ?
            """,
            (job_id,),
        ).fetchone()
    context = row_to_dict(row)
    if not context or context["owner_id"] != user_id:
        raise HTTPException(status_code=404, detail="Job not found")
    if context["status"] != "running":
        raise HTTPException(status_code=400, detail="LLM calls are only allowed while the job is running")
    return context


def _job_token_total(job_id: str) -> int:
    with connect() as db:
        row = db.execute("select coalesce(sum(total_tokens), 0) as total from llm_usage where job_id = ?", (job_id,)).fetchone()
    return int(row["total"] or 0)


def _invoke_bedrock(payload: LLMMessageRequest, max_tokens: int, model_id: str) -> dict[str, Any]:
    # Anthropic models use the native invoke_model + messages body (below). Non-Anthropic Bedrock
    # models (DeepSeek, Kimi, …) use the provider-agnostic Converse API instead. Routing by id
    # keeps the Anthropic path untouched.
    if "anthropic" not in model_id:
        return _invoke_converse(payload, max_tokens, model_id)
    body = _anthropic_body(payload.messages, payload.tools, payload.tool_choice, max_tokens)
    client = boto3.client(
        "bedrock-runtime",
        region_name=settings.bedrock_region,
        config=Config(read_timeout=settings.bedrock_read_timeout_seconds),
    )
    try:
        response = client.invoke_model(
            modelId=model_id,
            contentType="application/json",
            accept="application/json",
            body=json.dumps(body),
        )
    except ClientError as exc:
        message = exc.response.get("Error", {}).get("Message") or str(exc)
        raise HTTPException(status_code=502, detail=f"Bedrock invoke failed: {message}") from exc
    except BotoCoreError as exc:
        raise HTTPException(status_code=502, detail=f"Bedrock invoke failed: {exc}") from exc
    raw = json.loads(response["body"].read())
    usage = raw.get("usage") or {}
    return {
        "model": raw.get("model") or model_id,
        "message": _openai_message_from_anthropic(raw.get("content") or []),
        "usage": {
            "input_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
            "total_tokens": int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0),
        },
    }


def _invoke_converse(payload: LLMMessageRequest, max_tokens: int, model_id: str) -> dict[str, Any]:
    system, messages = _converse_messages(payload.messages)
    tool_config = _converse_tool_config(payload.tools, payload.tool_choice)
    client = boto3.client(
        "bedrock-runtime",
        region_name=settings.bedrock_region,
        config=Config(read_timeout=settings.bedrock_read_timeout_seconds),
    )
    kwargs: dict[str, Any] = {
        "modelId": model_id,
        "messages": messages,
        "inferenceConfig": {"maxTokens": max_tokens},
    }
    if system:
        kwargs["system"] = system
    if tool_config is not None:
        kwargs["toolConfig"] = tool_config
    try:
        response = client.converse(**kwargs)
    except ClientError as exc:
        message = exc.response.get("Error", {}).get("Message") or str(exc)
        raise HTTPException(status_code=502, detail=f"Bedrock converse failed: {message}") from exc
    except BotoCoreError as exc:
        raise HTTPException(status_code=502, detail=f"Bedrock converse failed: {exc}") from exc
    content = (((response.get("output") or {}).get("message") or {}).get("content")) or []
    usage = response.get("usage") or {}
    input_tokens = int(usage.get("inputTokens") or 0)
    output_tokens = int(usage.get("outputTokens") or 0)
    return {
        "model": model_id,
        "message": _openai_message_from_converse(content),
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": int(usage.get("totalTokens") or (input_tokens + output_tokens)),
        },
    }


def _converse_text(value: Any) -> str:
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict) and item.get("type") in ("image", "image_url"):
                parts.append("[image omitted: this model has no vision support]")
            else:
                parts.append(str(item.get("text") if isinstance(item, dict) else item))
        return "\n".join(parts)
    return str(value or "")


def _converse_messages(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Convert OpenAI-style messages to (system, Converse messages).

    Bedrock Converse requires strict user/assistant alternation, so consecutive same-role
    messages are merged into one — in particular multiple `tool` results become one user
    message with several toolResult blocks. The template always starts system,user, so the
    first emitted message is `user`.
    """
    system_parts: list[str] = []
    grouped: list[dict[str, Any]] = []

    def push(role: str, blocks: list[dict[str, Any]]) -> None:
        if not blocks:
            return
        if grouped and grouped[-1]["role"] == role:
            grouped[-1]["content"].extend(blocks)
        else:
            grouped.append({"role": role, "content": list(blocks)})

    for message in messages:
        role = message.get("role")
        if role == "system":
            content = message.get("content")
            if content:
                system_parts.append(str(content))
            continue
        if role == "tool":
            push("user", [{"toolResult": {
                "toolUseId": message.get("tool_call_id"),
                "content": [{"text": str(message.get("content") or "")}],
            }}])
            continue
        if role == "assistant":
            blocks: list[dict[str, Any]] = []
            text = _converse_text(message.get("content"))
            if text:
                blocks.append({"text": text})
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                blocks.append({"toolUse": {"toolUseId": call.get("id"), "name": function.get("name"), "input": arguments}})
            push("assistant", blocks or [{"text": " "}])  # Converse rejects empty assistant content
            continue
        # user (default)
        text = _converse_text(message.get("content"))
        if text:
            push("user", [{"text": text}])

    system_list = [{"text": "\n\n".join(system_parts)}] if system_parts else []
    return system_list, grouped


def _converse_tool_config(tools: list[dict[str, Any]], tool_choice: Any) -> dict[str, Any] | None:
    if not tools:
        return None
    specs = []
    for tool in tools:
        function = tool.get("function") or tool
        specs.append({"toolSpec": {
            "name": function["name"],
            "description": function.get("description", ""),
            "inputSchema": {"json": function.get("parameters", {"type": "object", "properties": {}})},
        }})
    choice = _normalize_tool_choice(tool_choice)
    if choice == "none":
        # Converse has no "none" choice — omit the tool config entirely so no tools are offered.
        return None
    config: dict[str, Any] = {"tools": specs}
    # "auto" is the Converse default; only set toolChoice when forcing (and some models reject an
    # explicit auto choice), so omit it for the common auto/None case.
    if isinstance(choice, tuple):
        config["toolChoice"] = {"tool": {"name": choice[1]}}
    elif choice == "any":
        config["toolChoice"] = {"any": {}}
    return config


def _openai_message_from_converse(content: list[dict[str, Any]]) -> dict[str, Any]:
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for item in content:
        if "text" in item:
            text_parts.append(item.get("text") or "")
        elif "toolUse" in item:
            tool_use = item["toolUse"]
            tool_calls.append({
                "id": tool_use.get("toolUseId"),
                "type": "function",
                "function": {"name": tool_use.get("name"), "arguments": json.dumps(tool_use.get("input") or {})},
            })
    message: dict[str, Any] = {"role": "assistant", "content": "\n".join(text_parts) or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


def _resolve_bedrock_model(requested_model: str | None) -> str:
    model_id = requested_model or settings.bedrock_default_model_id
    if not model_id:
        raise HTTPException(status_code=500, detail="No Bedrock models are configured for this arena")
    if model_id not in settings.bedrock_model_ids:
        raise HTTPException(status_code=400, detail=f"Model is not enabled for this arena: {model_id}")
    return model_id


def _anthropic_body(messages: list[dict[str, Any]], tools: list[dict[str, Any]], tool_choice: str | dict[str, Any] | None, max_tokens: int) -> dict[str, Any]:
    system_parts: list[str] = []
    bedrock_messages: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "system":
            content = message.get("content")
            if content:
                system_parts.append(str(content))
            continue
        if role == "tool":
            bedrock_messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": message.get("tool_call_id"),
                            "content": (
                                _content_blocks(message["content"])
                                if isinstance(message.get("content"), list)
                                else str(message.get("content") or "")
                            ),
                        }
                    ],
                }
            )
            continue
        if role == "assistant":
            bedrock_messages.append({"role": "assistant", "content": _assistant_content(message)})
            continue
        bedrock_messages.append({"role": "user", "content": _content_blocks(message.get("content"))})

    body: dict[str, Any] = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
        "messages": bedrock_messages,
    }
    if system_parts:
        body["system"] = "\n\n".join(system_parts)
    converted_tools = [_anthropic_tool(tool) for tool in tools]
    if converted_tools:
        body["tools"] = converted_tools
        choice = _normalize_tool_choice(tool_choice)
        if choice == "auto":
            body["tool_choice"] = {"type": "auto"}
        elif choice in ("any", "none"):
            body["tool_choice"] = {"type": choice}
        elif isinstance(choice, tuple):
            body["tool_choice"] = {"type": "tool", "name": choice[1]}
        # None: omit — Bedrock defaults to auto.
    return body


_IMAGE_DATA_URL = re.compile(r"^data:(image/(?:png|jpeg|jpg|gif|webp));base64,([A-Za-z0-9+/=\s]+)$")
MAX_IMAGE_B64_CHARS = 7_000_000  # ~5MB decoded, Anthropic's per-image limit


def _content_blocks(value: Any) -> list[dict[str, Any]]:
    """OpenAI-style message content (string or parts list) -> Anthropic content blocks.

    Text parts pass through; image_url parts (data: URLs) become base64 image
    blocks so vision-capable judges can actually SEE what they screenshot."""
    if not isinstance(value, list):
        return [{"type": "text", "text": str(value or "")}]
    blocks: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict) and item.get("type") == "image_url":
            url = str((item.get("image_url") or {}).get("url") or "")
            match = _IMAGE_DATA_URL.match(url)
            if not match:
                raise HTTPException(status_code=400, detail="image_url must be a data:image/...;base64 URL")
            data = re.sub(r"\s", "", match.group(2))
            if len(data) > MAX_IMAGE_B64_CHARS:
                raise HTTPException(status_code=400, detail="image too large (max ~5MB decoded)")
            media = "image/jpeg" if match.group(1) == "image/jpg" else match.group(1)
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": media, "data": data}})
        elif isinstance(item, dict) and item.get("type") == "image":
            blocks.append(item)  # Anthropic-native block passes through
        else:
            blocks.append({"type": "text", "text": str(item.get("text") if isinstance(item, dict) else item)})
    return blocks or [{"type": "text", "text": ""}]


def _text_content(value: Any) -> list[dict[str, str]]:
    if isinstance(value, list):
        text = "\n".join(str(item.get("text") if isinstance(item, dict) else item) for item in value)
    else:
        text = str(value or "")
    return [{"type": "text", "text": text}]


def _assistant_content(message: dict[str, Any]) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = []
    if message.get("content"):
        content.extend(_text_content(message["content"]))
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        raw_arguments = function.get("arguments") or "{}"
        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError:
            arguments = {}
        content.append(
            {
                "type": "tool_use",
                "id": call.get("id"),
                "name": function.get("name"),
                "input": arguments,
            }
        )
    return content or [{"type": "text", "text": ""}]


_TOOL_CHOICE_HELP = (
    'Unsupported tool_choice. Accepted forms: "auto", "none", "required"/"any", '
    '{"type": "tool", "name": "<tool>"} (Anthropic style), or '
    '{"type": "function", "function": {"name": "<tool>"}} (OpenAI style).'
)


def _normalize_tool_choice(tool_choice: Any) -> Any:
    """Map every OpenAI- and Anthropic-style tool_choice to one canonical form.

    Returns None (no preference), "auto", "any", "none", or ("tool", name).
    Participant agents are written against the OpenAI client shape locally, so the
    broker must accept both dialects; unrecognizable input gets an explicit 400
    instead of an opaque Bedrock 502.
    """
    if tool_choice is None or tool_choice == "":
        return None
    if isinstance(tool_choice, str):
        value = tool_choice.lower()
        if value == "auto":
            return "auto"
        if value == "none":
            return "none"
        if value in ("required", "any"):
            return "any"
        raise HTTPException(status_code=400, detail=_TOOL_CHOICE_HELP)
    if isinstance(tool_choice, dict):
        function = tool_choice.get("function")
        name = function.get("name") if isinstance(function, dict) else None
        name = name or tool_choice.get("name")
        if isinstance(name, str) and name:
            return ("tool", name)
        ctype = tool_choice.get("type")
        if ctype in ("auto", "any", "none"):
            return ctype
        if ctype == "required":
            return "any"
        raise HTTPException(status_code=400, detail=_TOOL_CHOICE_HELP)
    raise HTTPException(status_code=400, detail=_TOOL_CHOICE_HELP)


def _anthropic_tool(tool: dict[str, Any]) -> dict[str, Any]:
    function = tool.get("function") or tool
    return {
        "name": function["name"],
        "description": function.get("description", ""),
        "input_schema": function.get("parameters", {"type": "object", "properties": {}}),
    }


def _openai_message_from_anthropic(content: list[dict[str, Any]]) -> dict[str, Any]:
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for item in content:
        if item.get("type") == "text":
            text_parts.append(item.get("text") or "")
        elif item.get("type") == "tool_use":
            tool_calls.append(
                {
                    "id": item.get("id"),
                    "type": "function",
                    "function": {
                        "name": item.get("name"),
                        "arguments": json.dumps(item.get("input") or {}),
                    },
                }
            )
    message: dict[str, Any] = {"role": "assistant", "content": "\n".join(text_parts) or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


def _record_usage(
    *,
    job_id: str,
    submission_id: str,
    user_id: str,
    provider: str,
    model_id: str,
    purpose: str,
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
    estimated_cost_usd: float | None,
    latency_ms: int,
) -> None:
    with connect() as db:
        db.execute(
            """
            insert into llm_usage (
              id, job_id, submission_id, user_id, provider, model_id, purpose,
              input_tokens, output_tokens, total_tokens, estimated_cost_usd,
              latency_ms, created_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                job_id,
                submission_id,
                user_id,
                provider,
                model_id,
                purpose,
                input_tokens,
                output_tokens,
                total_tokens,
                estimated_cost_usd,
                latency_ms,
                now_iso(),
            ),
        )


# Bedrock list price per 1M tokens (input, output), matched by family substring against the
# (lowercased) model id — robust to id variants (global./us. prefixes, date stamps). Approximate;
# verify on the AWS Bedrock pricing page, or override at runtime with VIS_ARENA_MODEL_PRICES
# (JSON: {"family": [input_per_1m, output_per_1m]}). Unknown families fall back to the legacy
# global price pair (which defaults to 0 -> cost recorded as null).
_DEFAULT_MODEL_PRICES_USD_PER_1M: dict[str, tuple[float, float]] = {
    "opus": (15.0, 75.0),
    "sonnet": (3.0, 15.0),
    "haiku": (1.0, 5.0),
    "deepseek": (0.58, 1.68),  # DeepSeek-V3.x on Bedrock (us regions)
    "kimi": (0.60, 3.00),      # Kimi K2.5 on Bedrock (moonshotai.kimi-*)
    "moonshot": (0.60, 3.00),
}


def _load_model_prices() -> dict[str, tuple[float, float]]:
    prices = dict(_DEFAULT_MODEL_PRICES_USD_PER_1M)
    raw = os.environ.get("VIS_ARENA_MODEL_PRICES")
    if raw:
        try:
            for family, pair in json.loads(raw).items():
                prices[str(family).lower()] = (float(pair[0]), float(pair[1]))
        except (ValueError, TypeError, KeyError, IndexError):
            pass  # malformed override -> keep defaults
    return prices


_MODEL_PRICES_USD_PER_1M = _load_model_prices()


def _model_prices(model_id: str | None) -> tuple[float, float]:
    mid = (model_id or "").lower()
    for family, price in _MODEL_PRICES_USD_PER_1M.items():
        if family in mid:
            return price
    return (settings.llm_input_usd_per_1m, settings.llm_output_usd_per_1m)


def _estimated_cost_usd(model_id: str | None, input_tokens: int, output_tokens: int) -> float | None:
    input_price, output_price = _model_prices(model_id)
    if input_price <= 0 and output_price <= 0:
        return None
    return (input_tokens / 1_000_000 * input_price) + (output_tokens / 1_000_000 * output_price)


def _record_llm_request_trajectory(payload: LLMMessageRequest, context: dict[str, Any], model_id: str, max_tokens: int, remaining_tokens: int) -> None:
    tool_names = [_tool_name(tool) for tool in payload.tools]
    event: dict[str, Any] = {
        "type": "llm_request",
        "provider": "bedrock",
        "model": model_id,
        "submission_id": context["submission_id"],
        "purpose": payload.purpose,
        "message_count": len(payload.messages),
        "tool_names": [name for name in tool_names if name],
        "tool_choice": _truncate(payload.tool_choice),
        "max_tokens": max_tokens,
        "remaining_submission_tokens_before": remaining_tokens,
        # Full prompt/context (incl. agent-injected user turns) the incremental events can't
        # reconstruct. Each field is capped by _truncate (MAX_TRAJECTORY_VALUE_CHARS).
        "messages": _truncate(payload.messages),
    }
    append_broker_event(payload.job_id, payload.purpose, event)

    call_names = _tool_call_names_by_id(payload.messages)
    for message in payload.messages:
        if message.get("role") != "tool":
            continue
        tool_call_id = str(message.get("tool_call_id") or "")
        content = str(message.get("content") or "")
        append_broker_event(
            payload.job_id,
            payload.purpose,
            {
                "type": "tool_response",
                "tool_call_id": tool_call_id or None,
                "tool": call_names.get(tool_call_id),
                "content_preview": _preview(content),
                "content_chars": len(content),
            },
            dedupe_key=stable_event_key("tool_response", payload.job_id, payload.purpose, tool_call_id, content),
        )


def _record_llm_response_trajectory(payload: LLMMessageRequest, bedrock: dict[str, Any], latency_ms: int) -> None:
    message = bedrock["message"]
    usage = bedrock["usage"]
    content = message.get("content") or ""
    append_broker_event(
        payload.job_id,
        payload.purpose,
        {
            "type": "llm_response",
            "provider": "bedrock",
            "model": bedrock["model"],
            "content_preview": _preview(str(content)),
            "content_chars": len(str(content)),
            "tool_call_count": len(message.get("tool_calls") or []),
            "usage": usage,
            "latency_ms": latency_ms,
        },
    )
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        raw_arguments = function.get("arguments") or "{}"
        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError:
            arguments = raw_arguments
        append_broker_event(
            payload.job_id,
            payload.purpose,
            {
                "type": "tool_call",
                "tool_call_id": call.get("id"),
                "tool": function.get("name"),
                "arguments": _truncate(arguments),
            },
        )


def _tool_call_names_by_id(messages: list[dict[str, Any]]) -> dict[str, str]:
    names: dict[str, str] = {}
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            call_id = call.get("id")
            function = call.get("function") or {}
            if call_id and function.get("name"):
                names[str(call_id)] = str(function["name"])
    return names


def _tool_name(tool: dict[str, Any]) -> str | None:
    function = tool.get("function") or tool
    name = function.get("name")
    return str(name) if name else None


def _preview(value: str) -> str:
    if len(value) <= MAX_TRAJECTORY_VALUE_CHARS:
        return value
    return value[:MAX_TRAJECTORY_VALUE_CHARS] + "...[truncated]"


def _truncate(value: Any) -> Any:
    if isinstance(value, dict) and value.get("type") in ("image", "image_url"):
        return {"type": "image", "note": "[image data omitted from trajectory]"}
    if isinstance(value, str):
        return _preview(value)
    if isinstance(value, list):
        return [_truncate(item) for item in value[:50]]
    if isinstance(value, dict):
        return {str(key): _truncate(item) for key, item in list(value.items())[:50]}
    return value
