"""Deployment selection (docs/spec/04 §5): capability-aware eligibility, priority groups, weighted random."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

from aigw.adapters.base import Capabilities, DeploymentConfig
from aigw.adapters.registry import AdapterRegistry
from aigw.core.errors import ErrorType, GatewayError, UnsupportedParameter
from aigw.core.types import ChatRequest, EmbeddingRequest
from aigw.gateway.ratelimit import CooldownStore
from aigw.gateway.snapshot import ModelInfo


@dataclass
class Candidate:
    deployment: DeploymentConfig
    caps: Capabilities


@dataclass
class RoutingDecision:
    ordered: list[Candidate]
    rejected: dict[str, str] = field(default_factory=dict)  # deployment name -> reason

    def explain(self) -> dict:
        return {"candidates": [c.deployment.name for c in self.ordered], "rejected": self.rejected}


class Router:
    def __init__(self, adapters: AdapterRegistry, cooldowns: CooldownStore, rng: random.Random | None = None):
        self.adapters = adapters
        self.cooldowns = cooldowns
        self.rng = rng or random.Random()

    async def route(
        self,
        model: ModelInfo,
        req: ChatRequest | EmbeddingRequest,
        endpoint: str,
        est_prompt_tokens: int,
        region: str | None = None,
    ) -> RoutingDecision:
        now = time.time()
        eligible: list[Candidate] = []
        rejected: dict[str, str] = {}
        for d in model.deployments:
            if d.cooldown_until and d.cooldown_until > now:
                rejected[d.name] = "operator_cooldown"
                continue
            if await self.cooldowns.is_cooling(d.id):
                rejected[d.name] = "cooldown"
                continue
            if region and d.region and d.region != region:
                rejected[d.name] = "region"
                continue
            adapter = self.adapters.get(d.provider)
            caps = adapter.capabilities(d)
            if endpoint not in caps.endpoints:
                rejected[d.name] = f"endpoint:{endpoint}"
                continue
            if caps.max_context_tokens and est_prompt_tokens > caps.max_context_tokens:
                rejected[d.name] = "context_window"
                continue
            try:
                adapter.validate(req, caps)
            except UnsupportedParameter as exc:
                rejected[d.name] = f"unsupported:{exc.param}"
                continue
            eligible.append(Candidate(d, caps))
        if not eligible:
            if rejected and all(r.startswith("unsupported:") for r in rejected.values()):
                param = next(iter(rejected.values())).split(":", 1)[1]
                raise GatewayError(
                    ErrorType.invalid_request,
                    f"No deployment of '{model.name}' supports '{param}'",
                    code="unsupported_parameter",
                    param=param,
                )
            raise GatewayError(
                ErrorType.unavailable,
                f"No eligible deployment for model '{model.name}'",
                code="no_eligible_deployment",
                details={"rejected": rejected},
            )
        return RoutingDecision(ordered=self._order(eligible), rejected=rejected)

    def _order(self, cands: list[Candidate]) -> list[Candidate]:
        out: list[Candidate] = []
        by_priority: dict[int, list[Candidate]] = {}
        for c in cands:
            by_priority.setdefault(c.deployment.priority, []).append(c)
        for prio in sorted(by_priority):
            group = list(by_priority[prio])
            while group:
                weights = [max(c.deployment.weight, 0) for c in group]
                if sum(weights) == 0:
                    weights = [1] * len(group)
                pick = self.rng.choices(range(len(group)), weights=weights, k=1)[0]
                out.append(group.pop(pick))
        return out
