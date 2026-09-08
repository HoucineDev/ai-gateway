"""Deployment selection (docs/spec/04 §5): capability-aware eligibility, priority groups, then — within a group —
weighted random by configured weight (``weighted``) or by weight scaled down for latency, queue pressure and
in-flight load (``adaptive``, §5.1). Every decision is explainable: the diagnostics record keeps the per-candidate
signals and scores."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

from aigw.adapters.base import Capabilities, DeploymentConfig
from aigw.adapters.registry import AdapterRegistry
from aigw.core.errors import ErrorType, GatewayError, UnsupportedParameter
from aigw.core.types import ChatRequest, EmbeddingRequest
from aigw.gateway.ratelimit import CooldownStore
from aigw.gateway.signals import RoutingSignals
from aigw.gateway.snapshot import ModelInfo


@dataclass
class Candidate:
    deployment: DeploymentConfig
    caps: Capabilities | None = None


@dataclass
class RoutingDecision:
    ordered: list[Candidate]
    rejected: dict[str, str] = field(default_factory=dict)  # deployment name -> reason
    signals: dict[str, dict] = field(
        default_factory=dict
    )  # deployment name -> {ttfb_ms, queue_waiting, inflight, score}
    strategy: str = "weighted"

    def explain(self) -> dict:
        out = {
            "candidates": [c.deployment.name for c in self.ordered],
            "rejected": self.rejected,
            "strategy": self.strategy,
        }
        if self.signals:
            out["signals"] = self.signals
        return out


@dataclass
class RoutingPolicy:
    """Knobs for adaptive ordering (``AIGW_ROUTING_*``). References are the load at which a signal halves a
    candidate's draw weight."""

    strategy: str = "adaptive"  # weighted | adaptive
    latency_ref_ms: float = 1000.0
    queue_ref: float = 8.0
    inflight_ref: float = 4.0

    @classmethod
    def from_settings(cls, settings) -> RoutingPolicy:
        return cls(
            strategy=settings.routing_strategy,
            latency_ref_ms=settings.routing_latency_ref_ms,
            queue_ref=settings.routing_queue_ref,
            inflight_ref=settings.routing_inflight_ref,
        )


class Router:
    def __init__(
        self,
        adapters: AdapterRegistry,
        cooldowns: CooldownStore,
        rng: random.Random | None = None,
        signals: RoutingSignals | None = None,
        policy: RoutingPolicy | None = None,
    ):
        self.adapters = adapters
        self.cooldowns = cooldowns
        self.rng = rng or random.Random()
        self.signals = signals or RoutingSignals.build(None)
        self.policy = policy or RoutingPolicy()

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
            max_conc = (d.capabilities or {}).get("max_concurrency")
            if max_conc and self.signals.inflight.get(d.id) >= int(max_conc):
                rejected[d.name] = "saturated"  # local admission control: this replica already has max_concurrency open
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
        ordered, signals = await self._order(eligible)
        return RoutingDecision(ordered=ordered, rejected=rejected, signals=signals, strategy=self.policy.strategy)

    async def _order(self, cands: list[Candidate]) -> tuple[list[Candidate], dict[str, dict]]:
        """Priority groups ascending; inside a group, repeated weighted-random draws so the full fallback order
        still respects the (possibly signal-adjusted) weights."""
        adaptive = self.policy.strategy == "adaptive"
        pressure = await self.signals.pressure.get_many([c.deployment.id for c in cands]) if adaptive else {}
        scores: dict[str, float] = {}
        explain: dict[str, dict] = {}
        for c in cands:
            d = c.deployment
            score = float(max(d.weight, 0))
            if adaptive:
                ewma = self.signals.latency.get(d.id)
                p = pressure.get(d.id)
                inflight = self.signals.inflight.get(d.id)
                ref_inflight = float((d.capabilities or {}).get("max_concurrency") or self.policy.inflight_ref)
                score /= 1 + (ewma * 1000 / self.policy.latency_ref_ms if ewma else 0)
                score /= 1 + (p.waiting / self.policy.queue_ref if p else 0)
                score /= 1 + (inflight / ref_inflight if inflight else 0)
                explain[d.name] = {
                    "ttfb_ms": round(ewma * 1000) if ewma is not None else None,
                    "queue_waiting": p.waiting if p else None,
                    "inflight": inflight,
                    "weight": d.weight,
                    "score": round(score, 4),
                }
            scores[d.id] = score
        out: list[Candidate] = []
        by_priority: dict[int, list[Candidate]] = {}
        for c in cands:
            by_priority.setdefault(c.deployment.priority, []).append(c)
        for prio in sorted(by_priority):
            group = list(by_priority[prio])
            while group:
                weights = [scores[c.deployment.id] for c in group]
                if sum(weights) <= 0:
                    weights = [1.0] * len(group)
                pick = self.rng.choices(range(len(group)), weights=weights, k=1)[0]
                out.append(group.pop(pick))
        return out, explain
