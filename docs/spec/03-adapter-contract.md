# 03 — Provider adapter contract

Adapters live in `aigw.adapters.*`. Each adapter is our own code that talks HTTP to a provider through a shared `httpx.AsyncClient`. No provider SDK is required in Phase 1; official SDKs may be introduced per adapter after license review.

## 1. Interface

```python
class ProviderAdapter(Protocol):
    provider: str                                   # "openai_compat" | "openai" | "anthropic" | …

    def capabilities(self, deployment: DeploymentConfig) -> Capabilities: ...
    def validate(self, req: ChatRequest | EmbeddingRequest, caps: Capabilities) -> None: ...   # raises UnsupportedParameter
    async def chat(self, req: ChatRequest, deployment: DeploymentConfig, credential: str) -> ChatResponse: ...
    def chat_stream(self, req: ChatRequest, deployment: DeploymentConfig, credential: str) -> AsyncIterator[ChatEvent]: ...
    async def embeddings(self, req: EmbeddingRequest, deployment: DeploymentConfig, credential: str) -> EmbeddingResponse: ...
    def estimate_prompt_tokens(self, req: ChatRequest | EmbeddingRequest) -> int: ...
```

## 2. Capability declaration (proposal table: "Capability declaration")

```python
class Capabilities(BaseModel):
    endpoints: set[Literal["chat", "embeddings"]]
    streaming: bool
    tools: bool
    parallel_tools: bool
    json_object: bool
    json_schema: bool
    vision: bool
    system_role: Literal["message", "top_level", "none"]
    max_context_tokens: int | None
    reports_usage_in_stream: bool          # if False, gateway estimates and marks usage_source=estimated
    provider_extensions_passthrough: bool
    supported_params: set[str]             # canonical request field names accepted
```

The adapter's static defaults are merged with `deployments.capabilities` JSONB overrides so operators can narrow (never widen) what a deployment claims.

## 3. Canonical types (`aigw.core.types`)

`ChatRequest`, `Message` (roles system/user/assistant/tool; content string or parts `text`/`image_url`), `Tool`, `ToolChoice`, `ResponseFormat`, `ChatResponse`, `Choice`, `ToolCall`, `Usage(prompt_tokens, completion_tokens, total_tokens, cached_tokens, reasoning_tokens)`.

Streaming is a sequence of `ChatEvent`:

| event | payload |
|-------|---------|
| `start` | `upstream_request_id`, `model` |
| `delta` | `index`, `role?`, `content?`, `tool_calls?` (fragments with `index`, `id?`, `name?`, `arguments?`) |
| `finish` | `index`, `finish_reason` (stop/length/tool_calls/content_filter) |
| `usage` | `Usage` |
| `end` | — |

Rules: `usage` may arrive before or after `finish`; `end` is always last; an adapter must raise `UpstreamError` (not emit `end`) if the upstream stream terminates abnormally, so the gateway can classify the attempt as `ambiguous`.

## 4. Request translation

| Canonical | openai_compat / openai / azure_openai | anthropic | gemini |
|-----------|------------------------|-----------|--------|
| `messages[system]` | passed as role `system` | concatenated into top-level `system` | `systemInstruction.parts[text]` |
| `max_tokens` | `max_tokens` (`max_completion_tokens` for openai) | `max_tokens` (required; default from deployment `capabilities.default_max_tokens`, else 4096) | `generationConfig.maxOutputTokens` |
| `tools` (function) | as-is | `tools[{name, description, input_schema}]` | `tools[{functionDeclarations[{name, description, parameters}]}]` (JSON-Schema-only keywords stripped) |
| `tool_choice` | as-is | `{"type": auto/any/tool}`; `none` → tools omitted | `toolConfig.functionCallingConfig.mode` AUTO/ANY/NONE (+ `allowedFunctionNames`) |
| assistant `tool_calls` | as-is | `content[{type: tool_use}]` | `contents[{role: model, parts[{functionCall}]}]` |
| role `tool` result | as-is | user message with `tool_result` block (consecutive results merged) | user content with `functionResponse{name, response}` (name resolved from the matching `tool_call_id`; consecutive merged) |
| `response_format.json_schema` | as-is | rejected unless `capabilities.json_schema` (structured outputs via forced tool is Phase 2) | `generationConfig.responseMimeType=application/json` + `responseSchema` |
| `stop` | `stop` | `stop_sequences` | `generationConfig.stopSequences` |
| `image_url` parts | as-is | `image.source.url` or base64 | `inlineData` (data URL) or `fileData` |
| unsupported field | `UnsupportedParameter` → 400 | same | same (`logprobs`, `top_logprobs`, `extra_body`) |

Unsupported fields are rejected, never silently dropped (proposal: "reject unsupported fields or expose explicit provider extensions").

### 4.1 Azure OpenAI specifics

`azure_openai` reuses the OpenAI translation with Azure addressing: `base_url` is the resource endpoint
(`https://<resource>.openai.azure.com`, required), `provider_model` is the Azure *deployment name*, requests go to
`{base_url}/openai/deployments/{provider_model}/(chat/completions|embeddings)?api-version=<capabilities.api_version,
default 2024-10-21>` and the body carries no `model` field. `max_tokens` is sent as-is; set
`capabilities.max_tokens_field = "max_completion_tokens"` for o-series deployments. No provider-extension passthrough.
Prices are keyed by `(azure_openai, <deployment name>)`. The active health probe targets `/openai/models`.

### 4.2 Gemini (Vertex AI / Google AI Studio) specifics

`gemini` addresses Vertex AI by default: `{base_url or https://{location}-aiplatform.googleapis.com}/v1/projects/
{capabilities.project}/locations/{capabilities.location, default us-central1}/publishers/google/models/{provider_model}:
generateContent` (`streamGenerateContent?alt=sse` for streams, `predict` for embeddings). With `capabilities.api =
"google_ai"` it targets `{base_url or https://generativelanguage.googleapis.com}/v1beta/models/{provider_model}:…`
(`batchEmbedContents` for embeddings). Finish reasons map STOP→stop, MAX_TOKENS→length, SAFETY/RECITATION/BLOCKLIST/
PROHIBITED_CONTENT/SPII→content_filter; a response without candidates and with `promptFeedback.blockReason` is a
`content_filter` upstream error. Usage: `promptTokenCount`, `candidatesTokenCount` (+ `thoughtsTokenCount` as
reasoning), `cachedContentTokenCount`. Gemini returns whole function calls in one chunk, so streamed tool calls arrive
as a single delta with full arguments. The active health probe is `GET` on the model resource.

## 5. Authentication

`credential_ref` formats: `env:VAR_NAME` (alpha), `openbao:<mount>/<path>#<key>` (Phase 2), `none` (unauthenticated local endpoints). Resolution happens in `aigw.core.secrets` and the resolved value is never logged or persisted. `azure_openai` sends the resolved credential as the `api-key` header, or as an Entra ID bearer token when
`capabilities.auth = "bearer"`. `gemini` sends it as `x-goog-api-key` (`api_key`, Google AI default), as an OAuth2
bearer token (`bearer`, Vertex default), or — `capabilities.auth = "service_account"` — treats it as a service-account
key file and performs the RFC 7523 JWT bearer grant against the key's `token_uri` (override `capabilities.token_url`),
caching the access token until a minute before expiry. Cloud signing (Bedrock SigV4, Vertex workload identity) is a per-adapter concern added with those adapters.

## 6. Error classification

```
class ErrorClass(str, Enum):
    invalid_request     # 4xx that is our/user fault → no retry, no fallback
    authentication      # 401/403 upstream → mark deployment cooldown (credential problem), fallback allowed
    rate_limited        # 429 → cooldown deployment for Retry-After (default 10 s), fallback allowed
    overloaded          # 529/503 → cooldown, fallback allowed
    timeout_before_send # connect/first-byte timeout → retry/fallback allowed
    ambiguous           # timeout or disconnect after request accepted → no retry; settle at reservation
    upstream_error      # other 5xx → single retry then fallback
    content_filter      # provider moderation → no retry
```

Every `UpstreamError` retains `upstream_request_id`, `status_code`, `provider` and the raw body (truncated to 4 KiB) for diagnostics.

## 7. Usage and prices

Adapters return `Usage` when the provider reports it (`usage_source=reported`). When absent, the gateway estimates from prompt characters (`ceil(chars/4)`) and the streamed output length, and marks `usage_source=estimated`. Cost = Σ tokens × price component / 1e6 using `Decimal`, rounded half-even to 8 places, with the `price_id` recorded on the attempt.

## 8. Stateful resources

Files, batches and stored responses are Phase 4. The contract reserves `resource_ownership(provider, provider_resource_id, org_id, project_id)` so a resource id created on one deployment can never be sent to another provider through fallback.

## 9. Contract fixtures

`tests/fixtures/<provider>/` holds recorded request/response pairs (streaming and non-streaming, tools, JSON mode, errors, usage). Each adapter test replays fixtures through a mock ASGI upstream and asserts the canonical events. Adding a provider means adding fixtures before code.
