"""Mock upstream servers for contract tests and local development (no external credentials needed).

- OpenAI-compatible: /v1/chat/completions (stream + unary, tools), /v1/embeddings
- Anthropic-compatible: /v1/messages (stream + unary, tools)

Behaviour can be steered per request with the `user` field / metadata.user_id:
  "fail:429" | "fail:500" | "fail:503" | "fail:401" | "fail:400"  → that status
  "timeout"                                                      → hangs (for read-timeout tests)
  "cut"                                                          → stream terminates mid-way
  "nousage"                                                      → no usage block in stream
  "tool"                                                         → returns a tool call
  "slow"                                                         → 400 ms delay (concurrency tests)
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

mock = FastAPI(title="aigw mock upstream")
STATE = {"calls": 0, "last_body": None}


def _steer(body: dict) -> str:
    return body.get("user") or (body.get("metadata") or {}).get("user_id") or ""


async def _maybe_fail(body: dict):
    s = _steer(body)
    if s.startswith("fail:"):
        code = int(s.split(":")[1])
        headers = {"retry-after": "1"} if code == 429 else {}
        return JSONResponse(
            {"error": {"message": f"mock failure {code}", "type": "mock"}}, status_code=code, headers=headers
        )
    if s == "timeout":
        await asyncio.sleep(30)
    if s == "slow":
        await asyncio.sleep(0.4)
    return None


def _reply_text(body: dict) -> str:
    msgs = body.get("messages", [])
    last = msgs[-1] if msgs else {}
    content = last.get("content", "")
    if isinstance(content, list):
        content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return f"Echo: {content}"


@mock.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    STATE["calls"] += 1
    STATE["last_body"] = body
    failed = await _maybe_fail(body)
    if failed:
        return failed
    text = _reply_text(body)
    steer = _steer(body)
    prompt_tokens = sum(len(str(m.get("content", ""))) for m in body.get("messages", [])) // 4 + 5
    completion_tokens = len(text) // 4 + 1
    usage = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }
    rid = f"mock-{uuid.uuid4().hex[:8]}"
    if not body.get("stream"):
        if steer == "tool":
            msg = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city":"Lyon"}'},
                    }
                ],
            }
            fr = "tool_calls"
        else:
            msg, fr = {"role": "assistant", "content": text}, "stop"
        return JSONResponse(
            {
                "id": rid,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": body.get("model") or STATE.get("last_deployment") or "mock",
                "choices": [{"index": 0, "message": msg, "finish_reason": fr}],
                "usage": usage,
            },
            headers={"x-request-id": rid},
        )

    async def gen():
        base = {
            "id": rid,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": body.get("model") or STATE.get("last_deployment") or "mock",
        }
        yield _sse(
            {**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]}
        )
        if steer == "tool":
            yield _sse(
                {
                    **base,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {"name": "get_weather", "arguments": ""},
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ],
                }
            )
            for piece in ['{"city":', '"Lyon"}']:
                yield _sse(
                    {
                        **base,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"tool_calls": [{"index": 0, "function": {"arguments": piece}}]},
                                "finish_reason": None,
                            }
                        ],
                    }
                )
            yield _sse({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
        else:
            words = text.split(" ")
            for i, w in enumerate(words):
                if steer == "cut" and i == 2:
                    return  # abrupt termination without [DONE]
                yield _sse(
                    {
                        **base,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": w + (" " if i < len(words) - 1 else "")},
                                "finish_reason": None,
                            }
                        ],
                    }
                )
                await asyncio.sleep(0.005)
            yield _sse({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        if steer != "nousage":
            yield _sse({**base, "choices": [], "usage": usage})
        yield b"data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"x-request-id": rid})


AZURE_KEY = "azure-test-key"


def _azure_gate(deployment: str, request: Request):
    """Azure addressing: api-version query, api-key (or Entra bearer) header. Records what the adapter sent."""
    STATE["last_deployment"] = deployment
    STATE["last_query"] = dict(request.query_params)
    STATE["last_headers"] = {k: v for k, v in request.headers.items() if k in ("api-key", "authorization")}
    if "api-version" not in request.query_params:
        return JSONResponse({"error": {"code": "400", "message": "api-version is required"}}, status_code=400)
    ok = request.headers.get("api-key") == AZURE_KEY or request.headers.get("authorization", "").startswith("Bearer ")
    if not ok:
        return JSONResponse(
            {"error": {"code": "401", "message": "Access denied due to invalid subscription key"}}, status_code=401
        )
    return None


@mock.post("/openai/deployments/{deployment}/chat/completions")
async def azure_chat(deployment: str, request: Request):
    return _azure_gate(deployment, request) or await chat(request)


@mock.post("/openai/deployments/{deployment}/embeddings")
async def azure_embeddings(deployment: str, request: Request):
    return _azure_gate(deployment, request) or await embeddings(request)


@mock.get("/openai/models")
async def azure_models(request: Request):
    return {"object": "list", "data": [{"id": "gpt-4o", "object": "model"}]}


GEMINI_TOKEN, GEMINI_KEY, SA_TOKEN = "gemini-test-token", "gemini-test-key", "mock-sa-token"


def _gemini_auth(request: Request):
    auth = request.headers.get("authorization", "")
    STATE["last_headers"] = {k: v for k, v in request.headers.items() if k in ("authorization", "x-goog-api-key")}
    if auth in (f"Bearer {GEMINI_TOKEN}", f"Bearer {SA_TOKEN}") or request.headers.get("x-goog-api-key") == GEMINI_KEY:
        return None
    return JSONResponse(
        {
            "error": {
                "code": 401,
                "message": "Request had invalid authentication credentials.",
                "status": "UNAUTHENTICATED",
            }
        },
        status_code=401,
    )


def _gemini_last_text(body: dict) -> str:
    contents = body.get("contents") or []
    last = contents[-1] if contents else {}
    return " ".join(p.get("text", "") for p in last.get("parts", []) if isinstance(p, dict) and "text" in p)


async def _gemini_generate(model: str, request: Request):
    body = await request.json()
    STATE["calls"] += 1
    STATE["last_body"] = body
    STATE["last_url"] = str(request.url)
    STATE["last_model"] = model
    denied = _gemini_auth(request)
    if denied:
        return denied
    prompt = _gemini_last_text(body)
    if "[fail:" in prompt:
        code = int(prompt.split("[fail:")[1].split("]")[0])
        return JSONResponse(
            {"error": {"code": code, "message": f"mock failure {code}", "status": "UNAVAILABLE"}}, status_code=code
        )
    if "[blocked]" in prompt:
        return {
            "promptFeedback": {"blockReason": "SAFETY"},
            "usageMetadata": {"promptTokenCount": 3, "totalTokenCount": 3},
        }
    text = f"Echo: {prompt}"
    usage = {
        "promptTokenCount": 11,
        "candidatesTokenCount": len(text) // 4 + 1,
        "totalTokenCount": 11 + len(text) // 4 + 1,
    }
    rid = f"gem-{uuid.uuid4().hex[:8]}"
    if "[tool]" in prompt:
        parts = [{"functionCall": {"name": "get_weather", "args": {"city": "Lyon"}}}]
    else:
        parts = [{"text": text}]
    if request.query_params.get("alt") != "sse":
        return {
            "candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": "STOP", "index": 0}],
            "usageMetadata": usage,
            "modelVersion": model,
            "responseId": rid,
        }

    async def gen():
        if "[tool]" in prompt:
            yield f"data: {json.dumps({'candidates': [{'content': {'role': 'model', 'parts': parts}, 'finishReason': 'STOP', 'index': 0}], 'usageMetadata': usage, 'modelVersion': model, 'responseId': rid})}\n\n"
            return
        half = len(text) // 2
        yield f"data: {json.dumps({'candidates': [{'content': {'role': 'model', 'parts': [{'text': text[:half]}]}, 'index': 0}], 'modelVersion': model, 'responseId': rid})}\n\n"
        await asyncio.sleep(0.01)
        yield f"data: {json.dumps({'candidates': [{'content': {'role': 'model', 'parts': [{'text': text[half:]}]}, 'finishReason': 'STOP', 'index': 0}], 'usageMetadata': usage, 'modelVersion': model, 'responseId': rid})}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"x-request-id": rid})


@mock.post("/v1/projects/{project}/locations/{location}/publishers/google/models/{model}:generateContent")
async def vertex_generate(project: str, location: str, model: str, request: Request):
    return await _gemini_generate(model, request)


@mock.post("/v1/projects/{project}/locations/{location}/publishers/google/models/{model}:streamGenerateContent")
async def vertex_stream(project: str, location: str, model: str, request: Request):
    return await _gemini_generate(model, request)


@mock.post("/v1beta/models/{model}:generateContent")
async def google_ai_generate(model: str, request: Request):
    return await _gemini_generate(model, request)


@mock.post("/v1beta/models/{model}:streamGenerateContent")
async def google_ai_stream(model: str, request: Request):
    return await _gemini_generate(model, request)


@mock.post("/v1/projects/{project}/locations/{location}/publishers/google/models/{model}:predict")
async def vertex_predict(project: str, location: str, model: str, request: Request):
    body = await request.json()
    STATE["calls"] += 1
    STATE["last_body"] = body
    STATE["last_url"] = str(request.url)
    denied = _gemini_auth(request)
    if denied:
        return denied
    dims = (body.get("parameters") or {}).get("outputDimensionality") or 8
    return {
        "predictions": [
            {
                "embeddings": {
                    "values": [float((i + j) % 7) / 7 for j in range(dims)],
                    "statistics": {"token_count": len(inst["content"]) // 4 + 1},
                }
            }
            for i, inst in enumerate(body["instances"])
        ]
    }


@mock.post("/v1beta/models/{model}:batchEmbedContents")
async def google_ai_embed(model: str, request: Request):
    body = await request.json()
    STATE["calls"] += 1
    STATE["last_body"] = body
    denied = _gemini_auth(request)
    if denied:
        return denied
    return {"embeddings": [{"values": [0.1 * i, 0.2, 0.3]} for i, _ in enumerate(body["requests"])]}


@mock.get("/v1/projects/{project}/locations/{location}/publishers/google/models/{model}")
async def vertex_model(project: str, location: str, model: str, request: Request):
    return _gemini_auth(request) or {"name": f"publishers/google/models/{model}"}


@mock.get("/v1beta/models/{model}")
async def google_ai_model(model: str, request: Request):
    return _gemini_auth(request) or {"name": f"models/{model}"}


@mock.post("/token")
async def token(request: Request):
    """Google OAuth2 token endpoint stand-in for service-account JWT bearer grants."""
    form = await request.form()
    STATE["token_calls"] = STATE.get("token_calls", 0) + 1
    STATE["last_assertion"] = form.get("assertion")
    if form.get("grant_type") != "urn:ietf:params:oauth:grant-type:jwt-bearer" or not form.get("assertion"):
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    return {"access_token": SA_TOKEN, "expires_in": 3600, "token_type": "Bearer"}


@mock.post("/v1/embeddings")
async def embeddings(request: Request):
    body = await request.json()
    STATE["calls"] += 1
    failed = await _maybe_fail(body)
    if failed:
        return failed
    inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
    dims = body.get("dimensions") or 8
    data = [
        {"object": "embedding", "index": i, "embedding": [float((i + j) % 7) / 7 for j in range(dims)]}
        for i in range(len(inputs))
    ]
    tokens = sum(len(str(x)) for x in inputs) // 4 + 1
    return {
        "object": "list",
        "model": body.get("model") or STATE.get("last_deployment") or "mock",
        "data": data,
        "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
    }


@mock.get("/metrics")
async def metrics_endpoint():
    """vLLM-shaped Prometheus text; steer with STATE['queue_waiting'] / ['queue_running'] / ['kv_cache']."""
    from fastapi.responses import PlainTextResponse

    w, r, kv = STATE.get("queue_waiting", 0), STATE.get("queue_running", 0), STATE.get("kv_cache", 0.0)
    body = (
        "# HELP vllm:num_requests_waiting Number of requests waiting to be processed.\n"
        "# TYPE vllm:num_requests_waiting gauge\n"
        f'vllm:num_requests_waiting{{model_name="mock-chat"}} {float(w)}\n'
        f'vllm:num_requests_running{{model_name="mock-chat"}} {float(r)}\n'
        f'vllm:gpu_cache_usage_perc{{model_name="mock-chat"}} {float(kv)}\n'
    )
    return PlainTextResponse(body, media_type="text/plain; version=0.0.4")


@mock.get("/healthz")
async def healthz():
    return {"status": "ok", "role": "mock-upstream"}


@mock.get("/v1/models")
async def models():
    return {"object": "list", "data": [{"id": "mock-model", "object": "model"}]}


# ---- Anthropic-compatible ------------------------------------------------


@mock.post("/v1/messages")
async def messages(request: Request):
    body = await request.json()
    STATE["calls"] += 1
    STATE["last_body"] = body
    if request.headers.get("x-api-key") != "test-anthropic-key":
        return JSONResponse(
            {"type": "error", "error": {"type": "authentication_error", "message": "bad key"}}, status_code=401
        )
    steer = (body.get("metadata") or {}).get("user_id", "")
    if steer.startswith("fail:"):
        code = int(steer.split(":")[1])
        return JSONResponse(
            {
                "type": "error",
                "error": {
                    "type": "overloaded_error" if code == 529 else "api_error",
                    "message": f"mock failure {code}",
                },
            },
            status_code=code,
        )
    text = _reply_text(body)
    rid = f"msg_{uuid.uuid4().hex[:10]}"
    in_tok, out_tok = 12, len(text) // 4 + 1
    if not body.get("stream"):
        content = [{"type": "text", "text": text}]
        stop = "end_turn"
        if steer == "tool":
            content = [{"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {"city": "Lyon"}}]
            stop = "tool_use"
        return JSONResponse(
            {
                "id": rid,
                "type": "message",
                "role": "assistant",
                "model": body.get("model") or STATE.get("last_deployment") or "mock",
                "content": content,
                "stop_reason": stop,
                "usage": {"input_tokens": in_tok, "output_tokens": out_tok},
            },
            headers={"request-id": rid},
        )

    async def gen():
        yield _ev(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": rid,
                    "model": body.get("model") or STATE.get("last_deployment") or "mock",
                    "usage": {"input_tokens": in_tok, "output_tokens": 0},
                },
            },
        )
        if steer == "tool":
            yield _ev(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {}},
                },
            )
            for piece in ['{"city":', ' "Lyon"}']:
                yield _ev(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "input_json_delta", "partial_json": piece},
                    },
                )
            yield _ev("content_block_stop", {"type": "content_block_stop", "index": 0})
            yield _ev(
                "message_delta",
                {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": out_tok}},
            )
        else:
            yield _ev(
                "content_block_start",
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            )
            for w in text.split(" "):
                yield _ev(
                    "content_block_delta",
                    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": w + " "}},
                )
            yield _ev("content_block_stop", {"type": "content_block_stop", "index": 0})
            yield _ev(
                "message_delta",
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": out_tok}},
            )
        yield _ev("message_stop", {"type": "message_stop"})

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"request-id": rid})


def _sse(obj: dict) -> bytes:
    return b"data: " + json.dumps(obj).encode() + b"\n\n"


def _ev(name: str, obj: dict) -> bytes:
    return f"event: {name}\n".encode() + b"data: " + json.dumps(obj).encode() + b"\n\n"


@mock.post("/always429/v1/chat/completions")
async def always_429():
    return JSONResponse(
        {"error": {"message": "slow down", "type": "rate_limit"}}, status_code=429, headers={"retry-after": "5"}
    )


@mock.get("/always500/v1/models")
async def always_500_models():
    return JSONResponse({"error": {"message": "boom", "type": "server_error"}}, status_code=500)


@mock.post("/always500/v1/chat/completions")
async def always_500():
    return JSONResponse({"error": {"message": "boom", "type": "server_error"}}, status_code=500)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(mock, host="0.0.0.0", port=9000)
