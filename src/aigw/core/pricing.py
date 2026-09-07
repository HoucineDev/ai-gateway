"""Owned pricing registry helpers (docs/spec/03 §7). Fixed-point Decimal arithmetic only."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal

from aigw.core.types import Usage

MILLION = Decimal(1_000_000)
Q = Decimal("0.00000001")


@dataclass(frozen=True)
class PriceCard:
    id: str | None
    provider: str
    provider_model: str
    version: int
    input_per_million: Decimal
    output_per_million: Decimal
    cached_input_per_million: Decimal | None = None
    reasoning_per_million: Decimal | None = None

    @classmethod
    def zero(cls, provider: str, provider_model: str) -> PriceCard:
        return cls(None, provider, provider_model, 0, Decimal(0), Decimal(0))

    def cost(self, usage: Usage) -> Decimal:
        cached = usage.cached_tokens or 0
        uncached_prompt = max(usage.prompt_tokens - cached, 0)
        total = Decimal(uncached_prompt) * self.input_per_million
        if cached:
            rate = (
                self.cached_input_per_million if self.cached_input_per_million is not None else self.input_per_million
            )
            total += Decimal(cached) * rate
        total += Decimal(usage.completion_tokens) * self.output_per_million
        if usage.reasoning_tokens and self.reasoning_per_million is not None:
            # reasoning tokens are normally included in completion_tokens; only apply a premium delta
            total += Decimal(usage.reasoning_tokens) * (self.reasoning_per_million - self.output_per_million)
        return (total / MILLION).quantize(Q, rounding=ROUND_HALF_EVEN)

    def estimate_max(self, est_prompt_tokens: int, max_output_tokens: int) -> Decimal:
        total = (
            Decimal(est_prompt_tokens) * self.input_per_million + Decimal(max_output_tokens) * self.output_per_million
        )
        return (total / MILLION).quantize(Q, rounding=ROUND_HALF_EVEN)
