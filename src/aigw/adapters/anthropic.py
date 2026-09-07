"""Anthropic Messages API adapter. Translation rules in docs/spec/03 §4 (anthropic column)."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

from aigw.adapters.base import BaseHTTPAdapter, Capabilities, DeploymentConfig
from aigw.core.errors import ErrorClass, UnsupportedParameter, UpstreamError
from aigw.core.types import (
    ChatEvent,
    ChatRequest,
    ChatResponse,
    Choice,
    DeltaEvent,
    EmbeddingRequest,
    EmbeddingResponse,
    EndEvent,
    FinishEvent,
    FunctionCall,
    ImagePart,
    Message,
    StartEvent,
    TextPart,
    ToolCall,
    ToolCallDelta,
    Usage,
    UsageEvent,
)

ANTHROPIC_VERSION = "2023-06-01"


class AnthropicAdapter(BaseHTTPAdapter):
    provider = "anthropic"
    default_base_url = "https://api.anthropic.com"
    default_caps = Capabilities(
        endpoints={"chat"},
        streaming=True,
        tools=True,
        parallel_tools=True,
        json_object=False,  # no native JSON mode; structured output via forced tool is Phase 2
        json_schema=False,
        vision=True,
        system_role="top_level",
        default_max_tokens=4096,
        reports_usage_in_stream=True,
        provider_extensions_passthrough=False,
        supported_params={
            "max_tokens",
            "max_completion_tokens",
            "temperature",
            "top_p",
            "stop",
            "tools",
            "tool_choice",
            "parallel_tool_calls",
            "n",
            "stream_options",
        },
    )

    def _url(self, deployment: DeploymentConfig) -> str:
        return (deployment.base_url or self.default_base_url).rstrip("/") + "/v1/messages"

    def _headers(self, deployment: DeploymentConfig, credential: str) -> dict[str, str]:
        h = {
            "content-type": "application/json",
            "anthropic-version": ANTHROPIC_VERSION,
            **(deployment.extra_headers or {}),
        }
        if credential:
            h["x-api-key"] = credential
        return h

    # -- translation --------------------------------------------------------
    def _payload(self, req: ChatRequest, deployment: DeploymentConfig, caps: Capabilities) -> dict[str, Any]:
        system_parts: list[str] = []
        messages: list[dict[str, Any]] = []
        for m in req.messages:
            if m.role in ("system", "developer"):
                system_parts.append(m.text())
                continue
            if m.role == "tool":
                block = {"type": "tool_result", "tool_use_id": m.tool_call_id or "", "content": m.text()}
                if (
                    messages
                    and messages[-1]["role"] == "user"
                    and isinstance(messages[-1]["content"], list)
                    and messages[-1]["content"]
                    and messages[-1]["content"][0].get("type") == "tool_result"
                ):
                    messages[-1]["content"].append(block)
                else:
                    messages.append({"role": "user", "content": [block]})
                continue
            if m.role == "assistant":
                content: list[dict[str, Any]] = []
                if m.text():
                    content.append({"type": "text", "text": m.text()})
                for tc in m.tool_calls or []:
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except ValueError:
                        args = {"_raw": tc.function.arguments}
                    content.append({"type": "tool_use", "id": tc.id, "name": tc.function.name, "input": args})
                messages.append({"role": "assistant", "content": content or [{"type": "text", "text": ""}]})
                continue
            # user
            if isinstance(m.content, str) or m.content is None:
                messages.append({"role": "user", "content": m.content or ""})
            else:
                parts: list[dict[str, Any]] = []
                for p in m.content:
                    if isinstance(p, TextPart):
                        parts.append({"type": "text", "text": p.text})
                    elif isinstance(p, ImagePart):
                        parts.append(_image_block(p.image_url.url))
                messages.append({"role": "user", "content": parts})

        body: dict[str, Any] = {
            "model": deployment.provider_model,
            "messages": messages,
            "max_tokens": req.max_tokens or caps.default_max_tokens or 4096,
        }
        if system_parts:
            body["system"] = "\n\n".join(system_parts)
        if req.temperature is not None:
            body["temperature"] = req.temperature
        if req.top_p is not None:
            body["top_p"] = req.top_p
        if req.stop:
            body["stop_sequences"] = [req.stop] if isinstance(req.stop, str) else req.stop
        if req.user:
            body["metadata"] = {"user_id": req.user}
        if req.tools and req.tool_choice != "none":
            body["tools"] = [
                {
                    "name": t.function.name,
                    "description": t.function.description or "",
                    "input_schema": t.function.parameters or {"type": "object", "properties": {}},
                }
                for t in req.tools
            ]
            disable_parallel = req.parallel_tool_calls is False
            if req.tool_choice == "required":
                body["tool_choice"] = {"type": "any", "disable_parallel_tool_use": disable_parallel}
            elif req.tool_choice is not None and not isinstance(req.tool_choice, str):
                body["tool_choice"] = {
                    "type": "tool",
                    "name": req.tool_choice.function["name"],
                    "disable_parallel_tool_use": disable_parallel,
                }
            elif disable_parallel:
                body["tool_choice"] = {"type": "auto", "disable_parallel_tool_use": True}
        if req.stream:
            body["stream"] = True
        return body

    def validate(self, req, caps: Capabilities) -> None:
        super().validate(req, caps)
        if isinstance(req, EmbeddingRequest):
            raise UnsupportedParameter("embeddings", self.provider)

    # -- chat ---------------------------------------------------------------
    async def chat(self, req: ChatRequest, deployment: DeploymentConfig, credential: str) -> ChatResponse:
        caps = self.capabilities(deployment)
        data, headers = await self._post_json(
            self._url(deployment),
            self._payload(req, deployment, caps),
            self._headers(deployment, credential),
            deployment,
        )
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block["id"],
                        function=FunctionCall(name=block["name"], arguments=json.dumps(block.get("input", {}))),
                    )
                )
        msg = Message(
            role="assistant", content="".join(text_parts) if text_parts else None, tool_calls=tool_calls or None
        )
        return ChatResponse(
            id=data.get("id", "msg"),
            model=data.get("model", deployment.provider_model),
            choices=[Choice(index=0, message=msg, finish_reason=_finish(data.get("stop_reason")))],
            usage=_usage(data.get("usage")),
            created=int(time.time()),
            upstream_request_id=headers.get("request-id"),
        )

    async def chat_stream(
        self, req: ChatRequest, deployment: DeploymentConfig, credential: str
    ) -> AsyncIterator[ChatEvent]:
        caps = self.capabilities(deployment)
        started = False
        stopped = False
        prompt_tokens = 0
        cached = 0
        block_index_to_tool: dict[int, int] = {}
        tool_counter = 0
        stop_reason = None
        async for event, data, headers in self._sse(
            self._url(deployment),
            self._payload(req, deployment, caps),
            self._headers(deployment, credential),
            deployment,
        ):
            try:
                obj = json.loads(data)
            except ValueError:
                continue
            etype = obj.get("type") or event
            if etype == "message_start":
                u = obj.get("message", {}).get("usage", {})
                prompt_tokens = int(u.get("input_tokens") or 0)
                cached = int(u.get("cache_read_input_tokens") or 0)
                started = True
                yield StartEvent(
                    upstream_request_id=headers.get("request-id"), model=obj.get("message", {}).get("model")
                )
                yield DeltaEvent(index=0, role="assistant", content="")
            elif etype == "content_block_start":
                cb = obj.get("content_block", {})
                if cb.get("type") == "tool_use":
                    block_index_to_tool[obj["index"]] = tool_counter
                    yield DeltaEvent(
                        index=0,
                        tool_calls=[
                            ToolCallDelta(
                                index=tool_counter,
                                id=cb.get("id"),
                                function=FunctionCall(name=cb.get("name", ""), arguments=""),
                            )
                        ],
                    )
                    tool_counter += 1
            elif etype == "content_block_delta":
                d = obj.get("delta", {})
                if d.get("type") == "text_delta":
                    yield DeltaEvent(index=0, content=d.get("text", ""))
                elif d.get("type") == "input_json_delta":
                    ti = block_index_to_tool.get(obj["index"], 0)
                    yield DeltaEvent(
                        index=0,
                        tool_calls=[
                            ToolCallDelta(index=ti, function=FunctionCall(name="", arguments=d.get("partial_json", "")))
                        ],
                    )
            elif etype == "message_delta":
                stop_reason = obj.get("delta", {}).get("stop_reason") or stop_reason
                u = obj.get("usage", {})
                out = int(u.get("output_tokens") or 0)
                if "input_tokens" in u:
                    prompt_tokens = int(u["input_tokens"] or prompt_tokens)
                yield FinishEvent(index=0, finish_reason=_finish(stop_reason) or "stop")
                yield UsageEvent(
                    usage=Usage(prompt_tokens=prompt_tokens + cached, completion_tokens=out, cached_tokens=cached)
                )
            elif etype == "message_stop":
                stopped = True
                break
            elif etype == "error":
                err = obj.get("error", {})
                raise UpstreamError(
                    ErrorClass.overloaded if err.get("type") == "overloaded_error" else ErrorClass.upstream_error,
                    err.get("message", "stream error"),
                    provider=self.provider,
                    body=data,
                )
        if not started:
            raise UpstreamError(ErrorClass.upstream_error, "empty stream from upstream", provider=self.provider)
        if not stopped:
            raise UpstreamError(
                ErrorClass.ambiguous, "upstream stream ended without message_stop", provider=self.provider
            )
        yield EndEvent()

    async def embeddings(
        self, req: EmbeddingRequest, deployment: DeploymentConfig, credential: str
    ) -> EmbeddingResponse:
        raise UnsupportedParameter("embeddings", self.provider)


def _image_block(url: str) -> dict[str, Any]:
    if url.startswith("data:"):
        header, _, b64 = url.partition(",")
        media = header[5:].split(";")[0] or "image/png"
        return {"type": "image", "source": {"type": "base64", "media_type": media, "data": b64}}
    return {"type": "image", "source": {"type": "url", "url": url}}


def _finish(reason: str | None):
    return {
        "end_turn": "stop",
        "stop_sequence": "stop",
        "max_tokens": "length",
        "tool_use": "tool_calls",
        "refusal": "content_filter",
    }.get(reason or "", "stop" if reason else None)


def _usage(u: dict | None) -> Usage | None:
    if not u:
        return None
    cached = int(u.get("cache_read_input_tokens") or 0)
    return Usage(
        prompt_tokens=int(u.get("input_tokens") or 0) + cached,
        completion_tokens=int(u.get("output_tokens") or 0),
        cached_tokens=cached,
    )
