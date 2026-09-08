"""Reservation / settlement ledger (docs/spec/04 §4).

reserve():  lock applicable budgets (fixed id order), verify headroom, add reservation, insert pending attempt.
settle():   lock the same budgets, move reservation → spend, finalize the attempt, insert usage event,
            enqueue outbox messages. Idempotent per attempt.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from aigw.adapters.base import DeploymentConfig
from aigw.core.errors import ErrorType, GatewayError
from aigw.core.pricing import PriceCard
from aigw.core.types import Usage
from aigw.db.models import Budget, Outbox, RequestAttempt, UsageEvent
from aigw.db.session import Database
from aigw.gateway.snapshot import KeyScope

log = logging.getLogger(__name__)


@dataclass
class Reservation:
    attempt_id: uuid.UUID
    request_id: uuid.UUID
    attempt_no: int
    amount: Decimal
    budget_ids: list[uuid.UUID]
    price: PriceCard
    est_prompt_tokens: int
    est_output_tokens: int


def _now() -> datetime:
    return datetime.now(UTC)


def _roll_period(b: Budget, now: datetime) -> None:
    """Reset the budget window if its period elapsed (inside the locking transaction)."""
    if b.period == "total":
        return
    start = b.period_start
    if b.period == "daily":
        nxt = start + timedelta(days=1)
        while nxt <= now:
            start, nxt = nxt, nxt + timedelta(days=1)
    elif b.period == "monthly":
        nxt = _add_month(start)
        while nxt <= now:
            start, nxt = nxt, _add_month(nxt)
    if start != b.period_start:
        b.period_start = start
        b.spent_amount = Decimal(0)
        b.soft_alert_sent_period = None
        # reserved_amount is left alone: in-flight reservations settle against the new window


def _add_month(d: datetime) -> datetime:
    y, m = (d.year + (d.month // 12), (d.month % 12) + 1)
    return d.replace(year=y, month=m)


class Ledger:
    def __init__(self, db: Database):
        self.db = db

    def _scope_filters(self, scope: KeyScope):
        return [
            (Budget.scope_type == "key") & (Budget.scope_id == uuid.UUID(scope.key_id)),
            (Budget.scope_type == "project") & (Budget.scope_id == uuid.UUID(scope.project_id)),
            (Budget.scope_type == "team") & (Budget.scope_id == uuid.UUID(scope.team_id)),
            (Budget.scope_type == "organization") & (Budget.scope_id == uuid.UUID(scope.org_id)),
        ]

    async def _lock_budgets(self, s: AsyncSession, scope: KeyScope, ids: list[uuid.UUID] | None = None) -> list[Budget]:
        from sqlalchemy import or_

        q = select(Budget).where(Budget.status == "active")
        q = q.where(Budget.id.in_(ids)) if ids is not None else q.where(or_(*self._scope_filters(scope)))
        q = q.order_by(Budget.id).with_for_update()
        return list((await s.execute(q)).scalars())

    async def reserve(
        self,
        *,
        scope: KeyScope,
        request_id: uuid.UUID,
        attempt_no: int,
        model_id: str,
        model_name: str,
        deployment: DeploymentConfig,
        endpoint: str,
        price: PriceCard,
        est_prompt_tokens: int,
        est_output_tokens: int,
        routing: dict[str, Any],
    ) -> Reservation:
        amount = price.estimate_max(est_prompt_tokens, est_output_tokens)
        now = _now()
        async with self.db.tx() as s:
            budgets = await self._lock_budgets(s, scope)
            for b in budgets:
                _roll_period(b, now)
                limit = Decimal(b.limit_amount)
                if b.temporary_until and b.temporary_until > now:
                    limit += Decimal(b.temporary_increase or 0)
                available = limit - Decimal(b.spent_amount) - Decimal(b.reserved_amount)
                if available < amount:
                    raise GatewayError(
                        ErrorType.budget_exceeded,
                        f"Budget exceeded for {b.scope_type} (available {available:.6f}, required {amount:.6f})",
                        code="budget_exceeded",
                        details={"scope_type": b.scope_type, "scope_id": str(b.scope_id)},
                    )
            for b in budgets:
                b.reserved_amount = Decimal(b.reserved_amount) + amount
            attempt = RequestAttempt(
                request_id=request_id,
                attempt_no=attempt_no,
                org_id=uuid.UUID(scope.org_id),
                team_id=uuid.UUID(scope.team_id),
                project_id=uuid.UUID(scope.project_id),
                key_id=uuid.UUID(scope.key_id),
                model_id=uuid.UUID(model_id),
                model_name=model_name,
                deployment_id=uuid.UUID(deployment.id),
                provider=deployment.provider,
                provider_model=deployment.provider_model,
                endpoint=endpoint,
                status="pending",
                reserved_amount=amount,
                price_id=uuid.UUID(price.id) if price.id else None,
                routing=routing,
                dedup_key=f"{request_id}:{attempt_no}",
            )
            s.add(attempt)
            try:
                await s.flush()
            except IntegrityError:
                raise GatewayError(ErrorType.internal, "duplicate attempt", code="duplicate_attempt") from None
            return Reservation(
                attempt_id=attempt.id,
                request_id=request_id,
                attempt_no=attempt_no,
                amount=amount,
                budget_ids=[b.id for b in budgets],
                price=price,
                est_prompt_tokens=est_prompt_tokens,
                est_output_tokens=est_output_tokens,
            )

    async def settle(
        self,
        res: Reservation,
        *,
        scope: KeyScope,
        status: str,
        usage: Usage | None,
        usage_source: str,
        deployment: DeploymentConfig,
        model_name: str,
        endpoint: str,
        stream: bool,
        latency_ms: int | None,
        ttfb_ms: int | None,
        upstream_request_id: str | None = None,
        error_class: str | None = None,
        error_message: str | None = None,
        tags: dict[str, str] | None = None,
        first_byte_at: datetime | None = None,
    ) -> Decimal:
        """Finalize an attempt. Returns the settled amount. Idempotent: a second call is a no-op."""
        if usage is not None and status in ("succeeded", "ambiguous", "cancelled", "failed"):
            actual = res.price.cost(usage)
            if status in ("ambiguous",) and usage_source == "estimated":
                actual = max(actual, res.amount)  # conservative when usage is only estimated after an ambiguous end
        elif status in ("ambiguous", "cancelled"):
            actual = res.amount  # conservative: no usage known
        else:
            actual = Decimal(0)  # failed before any upstream cost
        now = _now()
        async with self.db.tx() as s:
            attempt = (
                await s.execute(select(RequestAttempt).where(RequestAttempt.id == res.attempt_id).with_for_update())
            ).scalar_one()
            if attempt.status != "pending":
                return Decimal(attempt.settled_amount or 0)
            budgets = await self._lock_budgets(s, scope, res.budget_ids)
            for b in budgets:
                b.reserved_amount = max(Decimal(b.reserved_amount) - res.amount, Decimal(0))
                b.spent_amount = Decimal(b.spent_amount) + actual
                if b.soft_alert_pct and b.limit_amount and b.soft_alert_sent_period is None:
                    if b.spent_amount >= Decimal(b.limit_amount) * Decimal(b.soft_alert_pct) / Decimal(100):
                        b.soft_alert_sent_period = b.period_start
                        s.add(
                            Outbox(
                                topic="budget.soft_alert",
                                payload={
                                    "budget_id": str(b.id),
                                    "scope_type": b.scope_type,
                                    "scope_id": str(b.scope_id),
                                    "spent": str(b.spent_amount),
                                    "limit": str(b.limit_amount),
                                    "pct": b.soft_alert_pct,
                                },
                            )
                        )
            attempt.status = status
            attempt.settled_amount = actual
            attempt.usage_source = usage_source if usage else None
            if usage:
                attempt.prompt_tokens = usage.prompt_tokens
                attempt.completion_tokens = usage.completion_tokens
                attempt.cached_tokens = usage.cached_tokens
                attempt.reasoning_tokens = usage.reasoning_tokens
            attempt.upstream_request_id = upstream_request_id
            attempt.error_class = error_class
            attempt.error_message = (error_message or None) and error_message[:2000]
            attempt.first_byte_at = first_byte_at
            attempt.ended_at = now
            ev = UsageEvent(
                request_id=res.request_id,
                attempt_id=res.attempt_id,
                org_id=attempt.org_id,
                team_id=attempt.team_id,
                project_id=attempt.project_id,
                key_id=attempt.key_id,
                model_name=model_name,
                deployment_id=attempt.deployment_id,
                provider=deployment.provider,
                endpoint=endpoint,
                prompt_tokens=usage.prompt_tokens if usage else 0,
                completion_tokens=usage.completion_tokens if usage else 0,
                cached_tokens=usage.cached_tokens if usage else 0,
                reasoning_tokens=usage.reasoning_tokens if usage else 0,
                cost=actual,
                usage_source=usage_source if usage else "none",
                status=status,
                latency_ms=latency_ms,
                ttfb_ms=ttfb_ms,
                stream=stream,
                tags=tags or {},
            )
            s.add(ev)
            s.add(
                Outbox(
                    topic="usage.settled",
                    payload={
                        "request_id": str(res.request_id),
                        "attempt_id": str(res.attempt_id),
                        "status": status,
                        "cost": str(actual),
                    },
                )
            )
            return actual

    async def reconcile_pending(self, older_than_seconds: int) -> int:
        """Worker job: settle attempts stuck in pending as ambiguous at their reservation."""
        cutoff = _now() - timedelta(seconds=older_than_seconds)
        count = 0
        async with self.db.tx() as s:
            stuck = list(
                (
                    await s.execute(
                        select(RequestAttempt)
                        .where(RequestAttempt.status == "pending", RequestAttempt.started_at < cutoff)
                        .with_for_update(skip_locked=True)
                    )
                ).scalars()
            )
            for a in stuck:
                budgets = list(
                    (
                        await s.execute(
                            select(Budget)
                            .where(Budget.status == "active", Budget.org_id == a.org_id)
                            .order_by(Budget.id)
                            .with_for_update()
                        )
                    ).scalars()
                )
                for b in budgets:
                    if (b.scope_type, b.scope_id) in {
                        ("key", a.key_id),
                        ("project", a.project_id),
                        ("team", a.team_id),
                        ("organization", a.org_id),
                    }:
                        b.reserved_amount = max(Decimal(b.reserved_amount) - Decimal(a.reserved_amount), Decimal(0))
                        b.spent_amount = Decimal(b.spent_amount) + Decimal(a.reserved_amount)
                a.status = "ambiguous"
                a.settled_amount = a.reserved_amount
                a.error_class = a.error_class or "reconciled_timeout"
                a.ended_at = _now()
                s.add(
                    UsageEvent(
                        request_id=a.request_id,
                        attempt_id=a.id,
                        org_id=a.org_id,
                        team_id=a.team_id,
                        project_id=a.project_id,
                        key_id=a.key_id,
                        model_name=a.model_name,
                        deployment_id=a.deployment_id,
                        provider=a.provider,
                        endpoint=a.endpoint,
                        cost=Decimal(a.reserved_amount),
                        usage_source="none",
                        status="ambiguous",
                        stream=False,
                    )
                )
                count += 1
        return count

    async def record_cache_hit(
        self,
        *,
        request_id: uuid.UUID,
        scope,
        model_id: str,
        model_name: str,
        deployment,
        endpoint: str,
        usage,
        stream: bool,
        tags: dict | None,
        routing: dict,
        latency_ms: int,
    ) -> None:
        """A served cache entry (docs/spec/04 §9): one `cached` attempt and usage event, no reservation, cost 0."""
        now = _now()
        async with self.db.tx() as s:
            attempt = RequestAttempt(
                request_id=request_id,
                attempt_no=1,
                org_id=uuid.UUID(scope.org_id),
                team_id=uuid.UUID(scope.team_id),
                project_id=uuid.UUID(scope.project_id),
                key_id=uuid.UUID(scope.key_id),
                model_id=uuid.UUID(model_id),
                model_name=model_name,
                deployment_id=uuid.UUID(deployment.id),
                provider=deployment.provider,
                provider_model=deployment.provider_model,
                endpoint=endpoint,
                status="cached",
                reserved_amount=Decimal(0),
                settled_amount=Decimal(0),
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                usage_source="cache",
                routing=routing,
                dedup_key=f"{request_id}:1",
                ended_at=now,
            )
            s.add(attempt)
            await s.flush()
            s.add(
                UsageEvent(
                    request_id=request_id,
                    attempt_id=attempt.id,
                    org_id=attempt.org_id,
                    team_id=attempt.team_id,
                    project_id=attempt.project_id,
                    key_id=attempt.key_id,
                    model_name=model_name,
                    deployment_id=attempt.deployment_id,
                    provider=deployment.provider,
                    endpoint=endpoint,
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                    cost=Decimal(0),
                    usage_source="cache",
                    status="cached",
                    latency_ms=latency_ms,
                    stream=stream,
                    tags=tags or {},
                )
            )

    async def mark_first_byte(self, attempt_id: uuid.UUID, at: datetime) -> None:
        async with self.db.tx() as s:
            await s.execute(update(RequestAttempt).where(RequestAttempt.id == attempt_id).values(first_byte_at=at))
