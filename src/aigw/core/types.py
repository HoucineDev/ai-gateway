"""Canonical request/response/event types. Owned schema; OpenAI wire-compatible on ingress.

See docs/spec/01 §2 and docs/spec/03 §3.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Role = Literal["system", "developer", "user", "assistant", "tool"]


class TextPart(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ImageURL(BaseModel):
    url: str
    detail: Literal["auto", "low", "high"] | None = None


class ImagePart(BaseModel):
    type: Literal["image_url"] = "image_url"
    image_url: ImageURL


ContentPart = TextPart | ImagePart


class FunctionCall(BaseModel):
    name: str
    arguments: str = ""


class ToolCall(BaseModel):
    id: str
    type: Literal["function"] = "function"
    function: FunctionCall


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Role
    content: str | list[ContentPart] | None = None
    name: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None

    def text(self) -> str:
        if isinstance(self.content, str):
            return self.content
        if self.content is None:
            return ""
        return "".join(p.text for p in self.content if isinstance(p, TextPart))

    def has_images(self) -> bool:
        return isinstance(self.content, list) and any(isinstance(p, ImagePart) for p in self.content)


class FunctionDef(BaseModel):
    name: str
    description: str | None = None
    parameters: dict[str, Any] | None = None
    strict: bool | None = None


class Tool(BaseModel):
    type: Literal["function"] = "function"
    function: FunctionDef


class NamedToolChoice(BaseModel):
    type: Literal["function"] = "function"
    function: dict[str, str]


ToolChoice = Literal["none", "auto", "required"] | NamedToolChoice


class JSONSchemaSpec(BaseModel):
    name: str
    schema_: dict[str, Any] | None = Field(default=None, alias="schema")
    strict: bool | None = None
    description: str | None = None
    model_config = ConfigDict(populate_by_name=True)


class ResponseFormat(BaseModel):
    type: Literal["text", "json_object", "json_schema"]
    json_schema: JSONSchemaSpec | None = None


class StreamOptions(BaseModel):
    include_usage: bool = False


class ChatRequest(BaseModel):
    """Fields accepted on /v1/chat/completions (docs/spec/01 §2.1). Unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")

    model: str
    messages: list[Message] = Field(min_length=1)
    stream: bool = False
    stream_options: StreamOptions | None = None
    max_tokens: int | None = Field(default=None, ge=1)
    max_completion_tokens: int | None = Field(default=None, ge=1)
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    stop: str | list[str] | None = None
    n: int | None = Field(default=None, ge=1, le=1)
    tools: list[Tool] | None = None
    tool_choice: ToolChoice | None = None
    parallel_tool_calls: bool | None = None
    response_format: ResponseFormat | None = None
    seed: int | None = None
    user: str | None = None
    metadata: dict[str, str] | None = None
    presence_penalty: float | None = Field(default=None, ge=-2, le=2)
    frequency_penalty: float | None = Field(default=None, ge=-2, le=2)
    logprobs: bool | None = None
    top_logprobs: int | None = Field(default=None, ge=0, le=20)
    extra_body: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _normalize(self) -> ChatRequest:
        if self.max_completion_tokens and not self.max_tokens:
            self.max_tokens = self.max_completion_tokens
        return self

    def set_params(self) -> set[str]:
        """Names of optional parameters explicitly provided (for capability validation)."""
        return {k for k in self.model_fields_set if k not in {"model", "messages", "stream", "metadata", "user"}}

    def wants_tools(self) -> bool:
        return bool(self.tools)

    def wants_vision(self) -> bool:
        return any(m.has_images() for m in self.messages)


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0

    @model_validator(mode="after")
    def _total(self) -> Usage:
        if not self.total_tokens:
            self.total_tokens = self.prompt_tokens + self.completion_tokens
        return self

    def to_wire(self, estimated: bool = False) -> dict[str, Any]:
        d: dict[str, Any] = {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }
        if self.cached_tokens:
            d["prompt_tokens_details"] = {"cached_tokens": self.cached_tokens}
        if self.reasoning_tokens:
            d["completion_tokens_details"] = {"reasoning_tokens": self.reasoning_tokens}
        if estimated:
            d["aigw_estimated"] = True
        return d


FinishReason = Literal["stop", "length", "tool_calls", "content_filter"]


class Choice(BaseModel):
    index: int = 0
    message: Message
    finish_reason: FinishReason | None = None


class ChatResponse(BaseModel):
    id: str
    model: str
    choices: list[Choice]
    usage: Usage | None = None
    created: int = 0
    upstream_request_id: str | None = None
    system_fingerprint: str | None = None

    def to_wire(self, *, id_override: str | None = None, model_name: str | None = None, estimated=False) -> dict:
        return {
            "id": id_override or self.id,
            "object": "chat.completion",
            "created": self.created,
            "model": model_name or self.model,
            "choices": [
                {
                    "index": c.index,
                    "message": c.message.model_dump(exclude_none=True),
                    "finish_reason": c.finish_reason,
                }
                for c in self.choices
            ],
            "usage": self.usage.to_wire(estimated) if self.usage else None,
        }


# ---- streaming events -------------------------------------------------------


class ToolCallDelta(BaseModel):
    index: int
    id: str | None = None
    type: Literal["function"] = "function"
    function: FunctionCall | None = None


class StartEvent(BaseModel):
    type: Literal["start"] = "start"
    upstream_request_id: str | None = None
    model: str | None = None


class DeltaEvent(BaseModel):
    type: Literal["delta"] = "delta"
    index: int = 0
    role: Role | None = None
    content: str | None = None
    tool_calls: list[ToolCallDelta] | None = None


class FinishEvent(BaseModel):
    type: Literal["finish"] = "finish"
    index: int = 0
    finish_reason: FinishReason


class UsageEvent(BaseModel):
    type: Literal["usage"] = "usage"
    usage: Usage


class EndEvent(BaseModel):
    type: Literal["end"] = "end"


ChatEvent = StartEvent | DeltaEvent | FinishEvent | UsageEvent | EndEvent


# ---- embeddings -------------------------------------------------------------


class EmbeddingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    input: str | list[str] | list[int] | list[list[int]]
    encoding_format: Literal["float"] | None = None
    dimensions: int | None = Field(default=None, ge=1)
    user: str | None = None
    metadata: dict[str, str] | None = None
    extra_body: dict[str, Any] | None = None

    @field_validator("input")
    @classmethod
    def _non_empty(cls, v):
        if isinstance(v, list) and not v:
            raise ValueError("input must not be empty")
        return v

    def texts(self) -> list[str]:
        if isinstance(self.input, str):
            return [self.input]
        if self.input and isinstance(self.input[0], str):
            return list(self.input)  # type: ignore[arg-type]
        return []

    def set_params(self) -> set[str]:
        return {k for k in self.model_fields_set if k not in {"model", "input", "metadata", "user"}}


class Embedding(BaseModel):
    index: int
    embedding: list[float]


class EmbeddingResponse(BaseModel):
    model: str
    data: list[Embedding]
    usage: Usage | None = None
    upstream_request_id: str | None = None

    def to_wire(self, model_name: str | None = None, estimated=False) -> dict:
        return {
            "object": "list",
            "model": model_name or self.model,
            "data": [{"object": "embedding", "index": e.index, "embedding": e.embedding} for e in self.data],
            "usage": self.usage.to_wire(estimated) if self.usage else None,
        }
