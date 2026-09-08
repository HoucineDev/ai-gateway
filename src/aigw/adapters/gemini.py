"""Gemini adapter: Vertex AI (default) or Google AI Studio (docs/spec/03 §4.2).

Addressing (``capabilities.api``):

* ``vertex`` — ``{base_url or https://{location}-aiplatform.googleapis.com}/v1/projects/{project}/locations/{location}/
  publishers/google/models/{provider_model}:{method}``; ``capabilities.project`` required, ``location`` default
  ``us-central1``. Auth ``capabilities.auth``: ``bearer`` (credential is an OAuth2 access token, default) or
  ``service_account`` (credential is the service-account JSON; the adapter signs a JWT assertion and exchanges it at
  the account's ``token_uri`` for an access token, cached until shortly before expiry).
* ``google_ai`` — ``{base_url or https://generativelanguage.googleapis.com}/v1beta/models/{provider_model}:{method}``
  with the credential as ``x-goog-api-key``.

Translation: OpenAI-style messages → ``systemInstruction`` + ``contents`` (roles ``user``/``model``, parts ``text`` /
``inlineData`` / ``fileData`` / ``functionCall`` / ``functionResponse``); tools → ``functionDeclarations``;
``tool_choice`` → ``toolConfig.functionCallingConfig``; sampling → ``generationConfig``; JSON modes →
``responseMimeType`` (+ ``responseSchema``). Streaming uses ``streamGenerateContent?alt=sse``. Embeddings use
``:predict`` (Vertex) or ``:batchEmbedContents`` (Google AI).
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import quote

import httpx

from aigw.adapters.base import BaseHTTPAdapter, Capabilities, DeploymentConfig
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

CLOUD_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
_SCHEMA_DROP = {"$schema", "additionalProperties", "strict", "$id", "$defs", "definitions"}


class GeminiAdapter(BaseHTTPAdapter):
    provider = "gemini"
    default_base_url = ""
    default_caps = Capabilities(
        endpoints={"chat", "embeddings"},
        streaming=True,
        tools=True,
        parallel_tools=True,
        json_object=True,
        json_schema=True,
        vision=True,
        system_role="top_level",
        reports_usage_in_stream=True,
        provider_extensions_passthrough=False,
        supported_params={
            "max_tokens",
            "max_completion_tokens",
            "temperature",
            "top_p",
            "stop",
            "n",
            "tools",
            "tool_choice",
            "parallel_tool_calls",
            "response_format",
            "seed",
            "presence_penalty",
            "frequency_penalty",
            "stream_options",
            "dimensions",
            "encoding_format",
        },
    )

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._tokens: dict[str, tuple[str, float]] = {}  # sha256(credential) -> (access_token, expires_at)

    # -- addressing ---------------------------------------------------------
    @staticmethod
    def _api(deployment: DeploymentConfig) -> str:
        return str((deployment.capabilities or {}).get("api") or "vertex")

    def _model_path(self, deployment: DeploymentConfig) -> str:
        caps = deployment.capabilities or {}
        model = quote(deployment.provider_model, safe="")
        if self._api(deployment) == "google_ai":
            base = (deployment.base_url or "https://generativelanguage.googleapis.com").rstrip("/")
            return f"{base}/v1beta/models/{model}"
        project = caps.get("project")
        if not project:
            raise GatewayError(
                ErrorType.unavailable,
                f"gemini deployment '{deployment.name}' needs capabilities.project (Vertex AI)",
                code="gemini_project_required",
            )
        location = str(caps.get("location") or "us-central1")
        base = (deployment.base_url or f"https://{location}-aiplatform.googleapis.com").rstrip("/")
        return (
            f"{base}/v1/projects/{quote(str(project), safe='')}/locations/{location}/publishers/google/models/{model}"
        )

    def _url(self, deployment: DeploymentConfig, method: str) -> str:
        return f"{self._model_path(deployment)}:{method}"

    # -- auth ---------------------------------------------------------------
    async def _headers(self, deployment: DeploymentConfig, credential: str) -> dict[str, str]:
        h = {"content-type": "application/json", **(deployment.extra_headers or {})}
        caps = deployment.capabilities or {}
        api = self._api(deployment)
        mode = str(caps.get("auth") or ("api_key" if api == "google_ai" else "bearer"))
        if not credential:
            return h
        if mode == "api_key":
            h["x-goog-api-key"] = credential
        elif mode == "service_account":
            h["authorization"] = f"Bearer {await self._service_account_token(credential, caps)}"
        else:
            h["authorization"] = f"Bearer {credential}"
        return h

    async def _service_account_token(self, credential: str, caps: dict) -> str:
        """Exchange a service-account key (JSON) for an access token (RFC 7523 JWT bearer grant)."""
        import jwt  # PyJWT with cryptography: RS256 signing

        cache_key = hashlib.sha256(credential.encode()).hexdigest()
        cached = self._tokens.get(cache_key)
        if cached and cached[1] - 60 > time.time():
            return cached[0]
        try:
            sa = json.loads(credential)
            email, private_key = sa["client_email"], sa["private_key"]
        except (ValueError, KeyError, TypeError) as exc:
            raise GatewayError(
                ErrorType.unavailable,
                f"gemini service-account credential is not a key file: {exc}",
                code="credential_ref",
            ) from None
        token_uri = str(caps.get("token_url") or sa.get("token_uri") or DEFAULT_TOKEN_URI)
        now = int(time.time())
        assertion = jwt.encode(
            {"iss": email, "scope": CLOUD_SCOPE, "aud": token_uri, "iat": now, "exp": now + 3600},
            private_key,
            algorithm="RS256",
        )
        try:
            resp = await self.client.post(
                token_uri,
                data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion},
                timeout=httpx.Timeout(10.0),
            )
        except httpx.HTTPError as exc:
            raise UpstreamError(
                ErrorClass.authentication, f"token exchange failed: {exc}", provider=self.provider
            ) from exc
        if resp.status_code >= 400:
            raise UpstreamError(
                ErrorClass.authentication,
                f"token exchange rejected (HTTP {resp.status_code})",
                provider=self.provider,
                status_code=resp.status_code,
                body=resp.text,
            )
        data = resp.json()
        token, ttl = str(data["access_token"]), float(data.get("expires_in") or 3600)
        self._tokens[cache_key] = (token, time.time() + ttl)
        return token

    # -- translation --------------------------------------------------------
    def _payload(self, req: ChatRequest, deployment: DeploymentConfig, caps: Capabilities) -> dict[str, Any]:
        system_parts: list[str] = []
        contents: list[dict[str, Any]] = []
        call_names: dict[str, str] = {}  # tool_call_id -> function name (Gemini needs the name on the response)
        for m in req.messages:
            if m.role in ("system", "developer"):
                system_parts.append(m.text())
                continue
            if m.role == "tool":
                name = call_names.get(m.tool_call_id or "", m.name or m.tool_call_id or "tool")
                part = {"functionResponse": {"name": name, "response": _tool_response(m.text())}}
                if contents and contents[-1]["role"] == "user" and "functionResponse" in contents[-1]["parts"][0]:
                    contents[-1]["parts"].append(part)
                else:
                    contents.append({"role": "user", "parts": [part]})
                continue
            if m.role == "assistant":
                parts: list[dict[str, Any]] = []
                if m.text():
                    parts.append({"text": m.text()})
                for tc in m.tool_calls or []:
                    call_names[tc.id] = tc.function.name
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except ValueError:
                        args = {"_raw": tc.function.arguments}
                    parts.append({"functionCall": {"name": tc.function.name, "args": args}})
                contents.append({"role": "model", "parts": parts or [{"text": ""}]})
                continue
            # user
            if isinstance(m.content, str) or m.content is None:
                contents.append({"role": "user", "parts": [{"text": m.content or ""}]})
            else:
                parts = []
                for p in m.content:
                    if isinstance(p, TextPart):
                        parts.append({"text": p.text})
                    elif isinstance(p, ImagePart):
                        parts.append(_image_part(p.image_url.url))
                contents.append({"role": "user", "parts": parts or [{"text": ""}]})

        body: dict[str, Any] = {"contents": contents}
        if system_parts:
            body["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}
        gen: dict[str, Any] = {}
        max_tokens = req.max_tokens or caps.default_max_tokens
        if max_tokens:
            gen["maxOutputTokens"] = max_tokens
        if req.temperature is not None:
            gen["temperature"] = req.temperature
        if req.top_p is not None:
            gen["topP"] = req.top_p
        if req.stop:
            gen["stopSequences"] = [req.stop] if isinstance(req.stop, str) else req.stop
        if req.n:
            gen["candidateCount"] = req.n
        if req.seed is not None:
            gen["seed"] = req.seed
        if req.presence_penalty is not None:
            gen["presencePenalty"] = req.presence_penalty
        if req.frequency_penalty is not None:
            gen["frequencyPenalty"] = req.frequency_penalty
        if req.response_format and req.response_format.type in ("json_object", "json_schema"):
            gen["responseMimeType"] = "application/json"
            if req.response_format.type == "json_schema" and req.response_format.json_schema:
                schema = req.response_format.json_schema.schema_
                if schema:
                    gen["responseSchema"] = _clean_schema(schema)
        if gen:
            body["generationConfig"] = gen
        if req.tools and req.tool_choice != "none":
            body["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": t.function.name,
                            "description": t.function.description or "",
                            "parameters": _clean_schema(t.function.parameters or {"type": "object", "properties": {}}),
                        }
                        for t in req.tools
                    ]
                }
            ]
            if req.tool_choice == "required":
                body["toolConfig"] = {"functionCallingConfig": {"mode": "ANY"}}
            elif req.tool_choice is not None and not isinstance(req.tool_choice, str):
                body["toolConfig"] = {
                    "functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": [req.tool_choice.function["name"]]}
                }
        elif req.tools and req.tool_choice == "none":
            body["toolConfig"] = {"functionCallingConfig": {"mode": "NONE"}}
        return body

    # -- chat ---------------------------------------------------------------
    async def chat(self, req: ChatRequest, deployment: DeploymentConfig, credential: str) -> ChatResponse:
        caps = self.capabilities(deployment)
        data, headers = await self._post_json(
            self._url(deployment, "generateContent"),
            self._payload(req, deployment, caps),
            await self._headers(deployment, credential),
            deployment,
        )
        return self._parse_response(data, deployment, headers)

    def _parse_response(self, data: dict, deployment: DeploymentConfig, headers: httpx.Headers) -> ChatResponse:
        candidates = data.get("candidates") or []
        if not candidates:
            block = (data.get("promptFeedback") or {}).get("blockReason")
            raise UpstreamError(
                ErrorClass.content_filter if block else ErrorClass.upstream_error,
                f"Gemini returned no candidates{f' (blocked: {block})' if block else ''}",
                provider=self.provider,
                body=json.dumps(data)[:2000],
            )
        choices = []
        for i, cand in enumerate(candidates):
            text_parts: list[str] = []
            tool_calls: list[ToolCall] = []
            for part in (cand.get("content") or {}).get("parts") or []:
                if "text" in part and not part.get("thought"):
                    text_parts.append(part["text"])
                elif "functionCall" in part:
                    fc = part["functionCall"]
                    tool_calls.append(
                        ToolCall(
                            id=f"call_{uuid.uuid4().hex[:12]}",
                            function=FunctionCall(name=fc.get("name", ""), arguments=json.dumps(fc.get("args") or {})),
                        )
                    )
            finish = _finish(cand.get("finishReason"), bool(tool_calls))
            msg = Message(
                role="assistant", content="".join(text_parts) if text_parts else None, tool_calls=tool_calls or None
            )
            choices.append(Choice(index=cand.get("index", i), message=msg, finish_reason=finish))
        return ChatResponse(
            id=data.get("responseId") or f"gemini-{uuid.uuid4().hex[:12]}",
            model=data.get("modelVersion") or deployment.provider_model,
            choices=choices,
            usage=_usage(data.get("usageMetadata")),
            created=int(time.time()),
            upstream_request_id=headers.get("x-request-id") or data.get("responseId"),
        )

    async def chat_stream(
        self, req: ChatRequest, deployment: DeploymentConfig, credential: str
    ) -> AsyncIterator[ChatEvent]:
        caps = self.capabilities(deployment)
        started = False
        finished = False
        usage: Usage | None = None
        tool_counter = 0
        async for _event, data, headers in self._sse(
            self._url(deployment, "streamGenerateContent") + "?alt=sse",
            self._payload(req, deployment, caps),
            await self._headers(deployment, credential),
            deployment,
        ):
            try:
                obj = json.loads(data)
            except ValueError:
                continue
            if not started:
                started = True
                yield StartEvent(upstream_request_id=headers.get("x-request-id"), model=obj.get("modelVersion"))
                yield DeltaEvent(index=0, role="assistant", content="")
            if obj.get("usageMetadata"):
                usage = _usage(obj["usageMetadata"]) or usage
            if obj.get("error"):
                err = obj["error"]
                raise UpstreamError(
                    ErrorClass.overloaded if err.get("code") == 503 else ErrorClass.upstream_error,
                    err.get("message", "stream error"),
                    provider=self.provider,
                    body=data,
                )
            cands = obj.get("candidates") or []
            if not cands:
                block = (obj.get("promptFeedback") or {}).get("blockReason")
                if block:
                    yield FinishEvent(index=0, finish_reason="content_filter")
                    finished = True
                continue
            cand = cands[0]
            saw_tool = False
            for part in (cand.get("content") or {}).get("parts") or []:
                if "text" in part and not part.get("thought"):
                    if part["text"]:
                        yield DeltaEvent(index=0, content=part["text"])
                elif "functionCall" in part:
                    fc = part["functionCall"]
                    saw_tool = True
                    yield DeltaEvent(
                        index=0,
                        tool_calls=[
                            ToolCallDelta(
                                index=tool_counter,
                                id=f"call_{uuid.uuid4().hex[:12]}",
                                function=FunctionCall(
                                    name=fc.get("name", ""), arguments=json.dumps(fc.get("args") or {})
                                ),
                            )
                        ],
                    )
                    tool_counter += 1
            if cand.get("finishReason"):
                finished = True
                yield FinishEvent(
                    index=0, finish_reason=_finish(cand["finishReason"], saw_tool or tool_counter > 0) or "stop"
                )
        if not started:
            raise UpstreamError(ErrorClass.upstream_error, "empty stream from upstream", provider=self.provider)
        if not finished:
            raise UpstreamError(
                ErrorClass.ambiguous, "upstream stream ended without finishReason", provider=self.provider
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
        headers = await self._headers(deployment, credential)
        if self._api(deployment) == "google_ai":
            body: dict[str, Any] = {
                "requests": [
                    {
                        "model": f"models/{deployment.provider_model}",
                        "content": {"parts": [{"text": t}]},
                        **({"outputDimensionality": req.dimensions} if req.dimensions else {}),
                    }
                    for t in texts
                ]
            }
            data, resp_headers = await self._post_json(
                self._url(deployment, "batchEmbedContents"), body, headers, deployment
            )
            vectors = [e.get("values") or [] for e in data.get("embeddings") or []]
            usage = None
        else:
            body = {"instances": [{"content": t} for t in texts]}
            if req.dimensions:
                body["parameters"] = {"outputDimensionality": req.dimensions}
            data, resp_headers = await self._post_json(self._url(deployment, "predict"), body, headers, deployment)
            preds = data.get("predictions") or []
            vectors = [(p.get("embeddings") or {}).get("values") or [] for p in preds]
            tokens = sum(
                int(((p.get("embeddings") or {}).get("statistics") or {}).get("token_count") or 0) for p in preds
            )
            usage = Usage(prompt_tokens=tokens) if tokens else None
        return EmbeddingResponse(
            model=deployment.provider_model,
            data=[Embedding(index=i, embedding=[float(x) for x in v]) for i, v in enumerate(vectors)],
            usage=usage,
            upstream_request_id=resp_headers.get("x-request-id"),
        )

    # -- health (docs/spec/04 §6) ----------------------------------------------
    async def probe_request(self, deployment: DeploymentConfig, credential: str) -> tuple[str, dict[str, str]]:
        return self._model_path(deployment), await self._headers(deployment, credential)


def _image_part(url: str) -> dict[str, Any]:
    if url.startswith("data:"):
        header, _, b64 = url.partition(",")
        media = header[5:].split(";")[0] or "image/png"
        return {"inlineData": {"mimeType": media, "data": b64}}
    ext = url.rsplit(".", 1)[-1].lower().split("?")[0]
    mime = {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "gif": "image/gif",
        "webp": "image/webp",
    }.get(ext, "image/jpeg")
    return {"fileData": {"mimeType": mime, "fileUri": url}}


def _tool_response(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text) if text else {}
    except ValueError:
        parsed = text
    return parsed if isinstance(parsed, dict) else {"result": parsed}


def _clean_schema(schema: Any) -> Any:
    """Gemini accepts an OpenAPI-style subset: drop JSON-Schema-only keywords, recurse into properties/items."""
    if isinstance(schema, dict):
        return {k: _clean_schema(v) for k, v in schema.items() if k not in _SCHEMA_DROP}
    if isinstance(schema, list):
        return [_clean_schema(v) for v in schema]
    return schema


def _finish(reason: str | None, has_tool_calls: bool):
    if reason is None:
        return None
    if has_tool_calls and reason in ("STOP", "FINISH_REASON_UNSPECIFIED"):
        return "tool_calls"
    return {
        "STOP": "stop",
        "MAX_TOKENS": "length",
        "SAFETY": "content_filter",
        "RECITATION": "content_filter",
        "BLOCKLIST": "content_filter",
        "PROHIBITED_CONTENT": "content_filter",
        "SPII": "content_filter",
        "MALFORMED_FUNCTION_CALL": "stop",
    }.get(reason, "stop")


def _usage(u: dict | None) -> Usage | None:
    if not u:
        return None
    cached = int(u.get("cachedContentTokenCount") or 0)
    thoughts = int(u.get("thoughtsTokenCount") or 0)
    return Usage(
        prompt_tokens=int(u.get("promptTokenCount") or 0),
        completion_tokens=int(u.get("candidatesTokenCount") or 0) + thoughts,
        cached_tokens=cached,
        reasoning_tokens=thoughts,
    )
