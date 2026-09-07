"""Token estimation used for reservations and rate limiting when a provider does not report usage.

Heuristic: ~4 characters per token for prose, plus per-message overhead. Deliberately conservative
(over-estimates slightly) so reservations bound real cost. A tokenizer-backed estimator can replace this
per adapter without changing the pipeline.
"""

from __future__ import annotations

import json
import math

from aigw.core.types import ChatRequest, EmbeddingRequest

CHARS_PER_TOKEN = 3.5
PER_MESSAGE_OVERHEAD = 4
IMAGE_TOKENS = 1000


def estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    return int(math.ceil(len(text) / CHARS_PER_TOKEN))


def estimate_chat_prompt_tokens(req: ChatRequest) -> int:
    total = 0
    for m in req.messages:
        total += PER_MESSAGE_OVERHEAD + estimate_text_tokens(m.text())
        if m.has_images():
            total += IMAGE_TOKENS
        if m.tool_calls:
            total += estimate_text_tokens(json.dumps([t.model_dump() for t in m.tool_calls]))
    if req.tools:
        total += estimate_text_tokens(json.dumps([t.model_dump(exclude_none=True) for t in req.tools]))
    if req.response_format and req.response_format.json_schema:
        total += estimate_text_tokens(json.dumps(req.response_format.json_schema.model_dump(by_alias=True)))
    return total


def estimate_embedding_tokens(req: EmbeddingRequest) -> int:
    texts = req.texts()
    if texts:
        return sum(estimate_text_tokens(t) for t in texts) or 1
    # token-array inputs
    inp = req.input
    if inp and isinstance(inp[0], list):
        return sum(len(x) for x in inp)  # type: ignore[arg-type]
    return len(inp)  # type: ignore[arg-type]
