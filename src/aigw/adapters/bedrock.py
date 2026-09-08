"""Amazon Bedrock adapter — Converse / ConverseStream for chat, InvokeModel for embeddings (docs/spec/03 §4.3).

Addressing: ``{base_url or https://bedrock-runtime.{capabilities.region, default us-east-1}.amazonaws.com}/model/
{provider_model}/(converse|converse-stream|invoke)``. Auth (``capabilities.auth``): ``sigv4`` (default; the credential
is ``ACCESS_KEY_ID:SECRET_ACCESS_KEY[:SESSION_TOKEN]`` or the equivalent JSON, signed with the owned SigV4 in
``aigw.adapters.sigv4``) or ``bearer`` (a Bedrock API key sent as ``Authorization: Bearer``). Streaming decodes the
AWS binary event stream (``aigw.adapters.eventstream``). Embeddings support Titan (``amazon.titan-embed-*``, one
invocation per input) and Cohere (``cohere.embed-*``) request shapes. No native JSON mode: ``response_format`` is
rejected. Requests are serialised once to bytes so the signed payload is exactly what is sent.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import quote

import httpx

from aigw.adapters.base import BaseHTTPAdapter, Capabilities, DeploymentConfig
from aigw.adapters.eventstream import EventStreamError, iter_messages
from aigw.adapters.sigv4 import AwsCredentials, sign
from aigw.core.errors import ErrorClass, ErrorType, GatewayError, UnsupportedParameter, UpstreamError
from aigw.core.types import (
    ChatEvent,
    ChatRequest,
    ChatResponse,
    Choice,
    DeltaEvent,
    Embedding,
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

SERVICE = "bedrock"
_IMAGE_FORMATS = {
    "image/png": "png",
    "image/jpeg": "jpeg",
    "image/jpg": "jpeg",
    "image/gif": "gif",
    "image/webp": "webp",
}
_EXCEPTION_CLASS = {
    "throttlingException": ErrorClass.rate_limited,
    "serviceUnavailableException": ErrorClass.overloaded,
    "validationException": ErrorClass.invalid_request,
    "accessDeniedException": ErrorClass.authentication,
    "modelStreamErrorException": ErrorClass.upstream_error,
    "internalServerException": ErrorClass.upstream_error,
    "modelTimeoutException": ErrorClass.upstream_error,
}


def _dumps(obj: Any) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode()


class BedrockAdapter(BaseHTTPAdapter):
    provider = "bedrock"
    default_base_url = ""
    default_caps = Capabilities(
        endpoints={"chat", "embeddings"},
        streaming=True,
        tools=True,
        parallel_tools=True,
        json_object=False,
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
            "dimensions",
            "encoding_format",
        },
    )

    # -- addressing / auth --------------------------------------------------
    @staticmethod
    def region(deployment: DeploymentConfig) -> str:
        return str((deployment.capabilities or {}).get("region") or "us-east-1")

    def _runtime(self, deployment: DeploymentConfig) -> str:
        return (deployment.base_url or f"https://bedrock-runtime.{self.region(deployment)}.amazonaws.com").rstrip("/")

    def _url(self, deployment: DeploymentConfig, method: str) -> str:
        return f"{self._runtime(deployment)}/model/{quote(deployment.provider_model, safe='')}/{method}"

    def _headers(self, deployment: DeploymentConfig, credential: str, method: str, url: str, body: bytes) -> dict:
        h = {"content-type": "application/json", "accept": "application/json", **(deployment.extra_headers or {})}
        mode = str((deployment.capabilities or {}).get("auth") or "sigv4")
        if not credential:
            return h
        if mode == "bearer":
            h["authorization"] = f"Bearer {credential}"
            return h
        try:
            creds = AwsCredentials.parse(credential)
        except ValueError as exc:
            raise GatewayError(ErrorType.unavailable, f"bedrock credential: {exc}", code="credential_ref") from None
        return sign(method, url, h, body, creds, self.region(deployment), SERVICE)

    # -- http (bytes-exact so the signature matches what is sent) ------------
    async def _post(
        self, url: str, body: bytes, headers: dict, deployment: DeploymentConfig
    ) -> tuple[dict, httpx.Headers]:
        try:
            resp = await self.client.post(url, content=body, headers=headers, timeout=self._timeout(deployment))
        except httpx.HTTPError as exc:
            raise self._classify_exception(exc, after_send=False) from exc
        if resp.status_code >= 400:
            raise self._classify_status(resp.status_code, resp.text, resp.headers)
        try:
            return resp.json(), resp.headers
        except ValueError as exc:
            raise UpstreamError(
                ErrorClass.upstream_error,
                "upstream returned non-JSON body",
                provider=self.provider,
                status_code=resp.status_code,
                body=resp.text,
            ) from exc

    # -- translation --------------------------------------------------------
    def _payload(self, req: ChatRequest, deployment: DeploymentConfig, caps: Capabilities) -> dict[str, Any]:
        system: list[dict[str, Any]] = []
        messages: list[dict[str, Any]] = []
        for m in req.messages:
            if m.role in ("system", "developer"):
                system.append({"text": m.text()})
                continue
            if m.role == "tool":
                block = {"toolResult": {"toolUseId": m.tool_call_id or "", "content": [_tool_result(m.text())]}}
                if messages and messages[-1]["role"] == "user" and "toolResult" in messages[-1]["content"][0]:
                    messages[-1]["content"].append(block)
                else:
                    messages.append({"role": "user", "content": [block]})
                continue
            if m.role == "assistant":
                content: list[dict[str, Any]] = []
                if m.text():
                    content.append({"text": m.text()})
                for tc in m.tool_calls or []:
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except ValueError:
                        args = {"_raw": tc.function.arguments}
                    content.append({"toolUse": {"toolUseId": tc.id, "name": tc.function.name, "input": args}})
                messages.append({"role": "assistant", "content": content or [{"text": ""}]})
                continue
            if isinstance(m.content, str) or m.content is None:
                messages.append({"role": "user", "content": [{"text": m.content or ""}]})
            else:
                parts: list[dict[str, Any]] = []
                for p in m.content:
                    if isinstance(p, TextPart):
                        parts.append({"text": p.text})
                    elif isinstance(p, ImagePart):
                        parts.append(_image_block(p.image_url.url))
                messages.append({"role": "user", "content": parts or [{"text": ""}]})
        inference: dict[str, Any] = {"maxTokens": req.max_tokens or caps.default_max_tokens or 4096}
        if req.temperature is not None:
            inference["temperature"] = req.temperature
        if req.top_p is not None:
            inference["topP"] = req.top_p
        if req.stop:
            inference["stopSequences"] = [req.stop] if isinstance(req.stop, str) else req.stop
        body: dict[str, Any] = {"messages": messages, "inferenceConfig": inference}
        if system:
            body["system"] = system
        if req.tools and req.tool_choice != "none":
            cfg: dict[str, Any] = {
                "tools": [
                    {
                        "toolSpec": {
                            "name": t.function.name,
                            "description": t.function.description or t.function.name,
                            "inputSchema": {"json": t.function.parameters or {"type": "object", "properties": {}}},
                        }
                    }
                    for t in req.tools
                ]
            }
            if req.tool_choice == "required":
                cfg["toolChoice"] = {"any": {}}
            elif req.tool_choice == "auto":
                cfg["toolChoice"] = {"auto": {}}
            elif req.tool_choice is not None and not isinstance(req.tool_choice, str):
                cfg["toolChoice"] = {"tool": {"name": req.tool_choice.function["name"]}}
            body["toolConfig"] = cfg
        return body

    def validate(self, req, caps: Capabilities) -> None:
        super().validate(req, caps)
        if isinstance(req, ChatRequest):
            for m in req.messages:
                if isinstance(m.content, list):
                    for p in m.content:
                        if isinstance(p, ImagePart) and not p.image_url.url.startswith("data:"):
                            raise UnsupportedParameter("messages.content.image_url (remote URL)", self.provider)

    # -- chat ---------------------------------------------------------------
    async def chat(self, req: ChatRequest, deployment: DeploymentConfig, credential: str) -> ChatResponse:
        caps = self.capabilities(deployment)
        url = self._url(deployment, "converse")
        body = _dumps(self._payload(req, deployment, caps))
        data, headers = await self._post(
            url, body, self._headers(deployment, credential, "POST", url, body), deployment
        )
        content = ((data.get("output") or {}).get("message") or {}).get("content") or []
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in content:
            if "text" in block:
                text_parts.append(block["text"])
            elif "toolUse" in block:
                tu = block["toolUse"]
                tool_calls.append(
                    ToolCall(
                        id=tu.get("toolUseId", ""),
                        function=FunctionCall(name=tu.get("name", ""), arguments=json.dumps(tu.get("input") or {})),
                    )
                )
        msg = Message(
            role="assistant", content="".join(text_parts) if text_parts else None, tool_calls=tool_calls or None
        )
        return ChatResponse(
            id=headers.get("x-amzn-requestid") or f"bedrock-{int(time.time() * 1000)}",
            model=deployment.provider_model,
            choices=[Choice(index=0, message=msg, finish_reason=_finish(data.get("stopReason")))],
            usage=_usage(data.get("usage")),
            created=int(time.time()),
            upstream_request_id=headers.get("x-amzn-requestid"),
        )

    async def chat_stream(
        self, req: ChatRequest, deployment: DeploymentConfig, credential: str
    ) -> AsyncIterator[ChatEvent]:
        caps = self.capabilities(deployment)
        url = self._url(deployment, "converse-stream")
        body = _dumps(self._payload(req, deployment, caps))
        headers = self._headers(deployment, credential, "POST", url, body)
        headers["accept"] = "application/vnd.amazon.eventstream"
        started = stopped = False
        usage: Usage | None = None
        block_to_tool: dict[int, int] = {}
        tool_counter = 0
        try:
            async with self.client.stream(
                "POST", url, content=body, headers=headers, timeout=self._timeout(deployment)
            ) as resp:
                if resp.status_code >= 400:
                    raise self._classify_status(
                        resp.status_code, (await resp.aread()).decode("utf-8", "replace"), resp.headers
                    )
                rid = resp.headers.get("x-amzn-requestid")
                try:
                    async for eh, payload in iter_messages(resp.aiter_bytes()):
                        try:
                            obj = json.loads(payload) if payload else {}
                        except ValueError:
                            obj = {}
                        if eh.get(":message-type") == "exception":
                            etype = eh.get(":exception-type", "")
                            raise UpstreamError(
                                _EXCEPTION_CLASS.get(etype, ErrorClass.upstream_error),
                                obj.get("message") or etype or "stream exception",
                                provider=self.provider,
                                body=payload.decode("utf-8", "replace"),
                            )
                        etype = eh.get(":event-type", "")
                        if etype == "messageStart":
                            started = True
                            yield StartEvent(upstream_request_id=rid, model=deployment.provider_model)
                            yield DeltaEvent(index=0, role="assistant", content="")
                        elif etype == "contentBlockStart":
                            tu = (obj.get("start") or {}).get("toolUse")
                            if tu:
                                block_to_tool[int(obj.get("contentBlockIndex", 0))] = tool_counter
                                yield DeltaEvent(
                                    index=0,
                                    tool_calls=[
                                        ToolCallDelta(
                                            index=tool_counter,
                                            id=tu.get("toolUseId"),
                                            function=FunctionCall(name=tu.get("name", ""), arguments=""),
                                        )
                                    ],
                                )
                                tool_counter += 1
                        elif etype == "contentBlockDelta":
                            d = obj.get("delta") or {}
                            if "text" in d:
                                yield DeltaEvent(index=0, content=d["text"])
                            elif "toolUse" in d:
                                ti = block_to_tool.get(int(obj.get("contentBlockIndex", 0)), 0)
                                yield DeltaEvent(
                                    index=0,
                                    tool_calls=[
                                        ToolCallDelta(
                                            index=ti,
                                            function=FunctionCall(name="", arguments=d["toolUse"].get("input", "")),
                                        )
                                    ],
                                )
                        elif etype == "messageStop":
                            stopped = True
                            yield FinishEvent(index=0, finish_reason=_finish(obj.get("stopReason")) or "stop")
                        elif etype == "metadata":
                            usage = _usage(obj.get("usage")) or usage
                except (httpx.HTTPError, EventStreamError) as exc:
                    raise (
                        self._classify_exception(exc, after_send=True)
                        if isinstance(exc, httpx.HTTPError)
                        else UpstreamError(ErrorClass.ambiguous, f"event stream error: {exc}", provider=self.provider)
                    ) from exc
        except httpx.HTTPError as exc:
            raise self._classify_exception(exc, after_send=False) from exc
        if not started:
            raise UpstreamError(ErrorClass.upstream_error, "empty stream from upstream", provider=self.provider)
        if not stopped:
            raise UpstreamError(
                ErrorClass.ambiguous, "upstream stream ended without messageStop", provider=self.provider
            )
        if usage:
            yield UsageEvent(usage=usage)
        yield EndEvent()

    # -- embeddings -----------------------------------------------------------
    async def embeddings(
        self, req: EmbeddingRequest, deployment: DeploymentConfig, credential: str
    ) -> EmbeddingResponse:
        texts = req.texts()
        if not texts:
            raise UnsupportedParameter("input (token arrays)", self.provider)
        model = deployment.provider_model
        url = self._url(deployment, "invoke")
        if model.startswith("amazon.titan-embed"):

            async def one(text: str) -> tuple[list[float], int]:
                payload: dict[str, Any] = {"inputText": text}
                if req.dimensions:
                    payload["dimensions"] = req.dimensions
                body = _dumps(payload)
                data, _ = await self._post(
                    url, body, self._headers(deployment, credential, "POST", url, body), deployment
                )
                return [float(x) for x in data.get("embedding") or []], int(data.get("inputTextTokenCount") or 0)

            results = await asyncio.gather(*(one(t) for t in texts))
            vectors = [v for v, _ in results]
            tokens = sum(n for _, n in results)
        elif model.startswith("cohere.embed"):
            payload = {"texts": texts, "input_type": "search_document", "embedding_types": ["float"]}
            body = _dumps(payload)
            data, _ = await self._post(url, body, self._headers(deployment, credential, "POST", url, body), deployment)
            emb = data.get("embeddings")
            vectors = [[float(x) for x in v] for v in (emb.get("float") if isinstance(emb, dict) else emb) or []]
            tokens = 0
        else:
            raise UnsupportedParameter(f"embeddings for model '{model}'", self.provider)
        return EmbeddingResponse(
            model=model,
            data=[Embedding(index=i, embedding=v) for i, v in enumerate(vectors)],
            usage=Usage(prompt_tokens=tokens) if tokens else None,
        )

    # -- health (docs/spec/04 §6) ----------------------------------------------
    def probe_request(self, deployment: DeploymentConfig, credential: str) -> tuple[str, dict[str, str]]:
        caps = deployment.capabilities or {}
        control = str(
            caps.get("control_url") or deployment.base_url or f"https://bedrock.{self.region(deployment)}.amazonaws.com"
        ).rstrip("/")
        url = f"{control}/foundation-models/{quote(deployment.provider_model, safe='')}"
        headers = self._headers(deployment, credential, "GET", url, b"")
        headers.pop("content-type", None) if "authorization" in headers and headers["authorization"].startswith(
            "Bearer"
        ) else None
        return url, headers


def _image_block(url: str) -> dict[str, Any]:
    header, _, b64 = url.partition(",")
    media = header[5:].split(";")[0] or "image/png"
    return {"image": {"format": _IMAGE_FORMATS.get(media, "png"), "source": {"bytes": b64}}}


def _tool_result(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text) if text else None
    except ValueError:
        parsed = None
    return {"json": parsed} if isinstance(parsed, dict) else {"text": text}


def _finish(reason: str | None):
    return {
        "end_turn": "stop",
        "stop_sequence": "stop",
        "max_tokens": "length",
        "tool_use": "tool_calls",
        "guardrail_intervened": "content_filter",
        "content_filtered": "content_filter",
    }.get(reason or "", "stop" if reason else None)


def _usage(u: dict | None) -> Usage | None:
    if not u:
        return None
    cached = int(u.get("cacheReadInputTokens") or 0)
    written = int(u.get("cacheWriteInputTokens") or 0)
    return Usage(
        prompt_tokens=int(u.get("inputTokens") or 0) + cached + written,
        completion_tokens=int(u.get("outputTokens") or 0),
        cached_tokens=cached,
    )
