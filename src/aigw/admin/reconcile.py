"""Provider invoice reconciliation (docs/spec/04 §10): compare a provider's billed lines (provider model × day:
tokens and amount) with what the ledger settled for the same provider in the same period.

Ledger side: `usage_events` joined to `request_attempts` for the provider model, restricted to attempts that
actually hit the provider (status `succeeded` or `ambiguous`; cached hits never reach the provider) and to the
invoice's period, aggregated per (provider_model, day). Ambiguous attempts settled at the reserved maximum are
counted and reported separately because they inflate the ledger by design (conservative uncertainty).
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aigw.db.models import Invoice, InvoiceLine, RequestAttempt, UsageEvent

MATCHED, AMOUNT_MISMATCH, TOKEN_MISMATCH, UNMATCHED = "matched", "amount_mismatch", "token_mismatch", "unmatched"


@dataclass(frozen=True)
class LedgerLine:
    provider_model: str
    day: date
    amount: Decimal
    prompt_tokens: int
    completion_tokens: int
    requests: int
    ambiguous: int


def parse_csv(text: str) -> list[dict]:
    """Columns: provider_model, day (YYYY-MM-DD), amount, optional prompt_tokens / completion_tokens.
    Extra columns are kept in `meta`. Raises ValueError with the offending line number."""
    reader = csv.DictReader(io.StringIO(text))
    required = {"provider_model", "day", "amount"}
    if not reader.fieldnames or not required <= {f.strip() for f in reader.fieldnames}:
        raise ValueError(f"CSV header must contain {sorted(required)}; got {reader.fieldnames}")
    out: list[dict] = []
    for n, row in enumerate(reader, start=2):
        row = {k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in row.items() if k}
        try:
            line = {
                "provider_model": row["provider_model"],
                "day": date.fromisoformat(row["day"]),
                "amount": Decimal(row["amount"]),
                "prompt_tokens": int(row["prompt_tokens"]) if row.get("prompt_tokens") else None,
                "completion_tokens": int(row["completion_tokens"]) if row.get("completion_tokens") else None,
                "meta": {k: v for k, v in row.items() if k not in required | {"prompt_tokens", "completion_tokens"}},
            }
        except (KeyError, ValueError, InvalidOperation) as exc:
            raise ValueError(f"CSV line {n}: {exc}") from None
        if not line["provider_model"]:
            raise ValueError(f"CSV line {n}: empty provider_model")
        out.append(line)
    if not out:
        raise ValueError("CSV has no data rows")
    return out


async def ledger_lines(
    s: AsyncSession, provider: str, period_start: date, period_end: date
) -> dict[tuple[str, date], LedgerLine]:
    start = datetime.combine(period_start, time.min, tzinfo=UTC)
    end = datetime.combine(period_end + timedelta(days=1), time.min, tzinfo=UTC)
    day = func.date_trunc("day", UsageEvent.created_at).label("day")
    q = (
        select(
            RequestAttempt.provider_model,
            day,
            func.sum(UsageEvent.cost),
            func.sum(UsageEvent.prompt_tokens),
            func.sum(UsageEvent.completion_tokens),
            func.count(),
            func.sum(case((UsageEvent.status == "ambiguous", 1), else_=0)),
        )
        .join(RequestAttempt, RequestAttempt.id == UsageEvent.attempt_id)
        .where(
            UsageEvent.provider == provider,
            UsageEvent.status.in_(("succeeded", "ambiguous")),
            UsageEvent.created_at >= start,
            UsageEvent.created_at < end,
        )
        .group_by(RequestAttempt.provider_model, day)
    )
    out: dict[tuple[str, date], LedgerLine] = {}
    for pm, d, cost, pt, ct, n, amb in (await s.execute(q)).all():
        key = (pm, d.date() if isinstance(d, datetime) else d)
        out[key] = LedgerLine(pm, key[1], Decimal(cost or 0), int(pt or 0), int(ct or 0), int(n), int(amb or 0))
    return out


def _within(a: Decimal, b: Decimal, tolerance_pct: Decimal, tolerance_abs: Decimal) -> bool:
    diff = abs(a - b)
    base = max(abs(a), abs(b))
    return diff <= tolerance_abs or (base > 0 and diff / base * 100 <= tolerance_pct)


async def reconcile(
    s: AsyncSession,
    inv: Invoice,
    lines: list[InvoiceLine],
    *,
    tolerance_pct: Decimal,
    tolerance_abs: Decimal,
    token_tolerance_pct: Decimal,
) -> dict:
    """Compare every invoice line with the ledger, annotate the lines, and return the report stored on the invoice."""
    ledger = await ledger_lines(s, inv.provider, inv.period_start, inv.period_end)
    seen: set[tuple[str, date]] = set()
    invoice_total = Decimal(0)
    ledger_total = sum((line_.amount for line_ in ledger.values()), Decimal(0))
    counts = {MATCHED: 0, AMOUNT_MISMATCH: 0, TOKEN_MISMATCH: 0, UNMATCHED: 0}
    for line in lines:
        key = (line.provider_model, line.day)
        seen.add(key)
        invoice_total += Decimal(line.amount)
        led = ledger.get(key)
        line.ledger_amount = led.amount if led else None
        line.ledger_prompt_tokens = led.prompt_tokens if led else None
        line.ledger_completion_tokens = led.completion_tokens if led else None
        line.ledger_requests = led.requests if led else 0
        line.ledger_ambiguous = led.ambiguous if led else 0
        if led is None:
            line.status = UNMATCHED
        elif not _within(Decimal(line.amount), led.amount, tolerance_pct, tolerance_abs):
            line.status = AMOUNT_MISMATCH
        elif _tokens_differ(line, led, token_tolerance_pct):
            line.status = TOKEN_MISMATCH
        else:
            line.status = MATCHED
        line.delta_amount = (Decimal(line.amount) - led.amount) if led else None
        counts[line.status] += 1
    missing = [
        {
            "provider_model": led.provider_model,
            "day": led.day.isoformat(),
            "ledger_amount": format(led.amount, "f"),
            "prompt_tokens": led.prompt_tokens,
            "completion_tokens": led.completion_tokens,
            "requests": led.requests,
        }
        for key, led in sorted(ledger.items(), key=lambda kv: (kv[0][1], kv[0][0]))
        if key not in seen
    ]
    delta = invoice_total - ledger_total
    status = (
        "matched"
        if not missing and counts[AMOUNT_MISMATCH] == 0 and counts[UNMATCHED] == 0 and counts[TOKEN_MISMATCH] == 0
        else "mismatch"
    )
    if status == "matched" and not _within(invoice_total, ledger_total, tolerance_pct, tolerance_abs):
        status = "mismatch"
    inv.invoice_total, inv.ledger_total, inv.delta_amount = invoice_total, ledger_total, delta
    inv.status, inv.reconciled_at = status, datetime.now(UTC)
    inv.report = {
        "lines": counts,
        "missing_in_invoice": missing,
        "ambiguous_attempts": sum(led.ambiguous for led in ledger.values()),
        "tolerance": {
            "pct": format(tolerance_pct, "f"),
            "abs": format(tolerance_abs, "f"),
            "token_pct": format(token_tolerance_pct, "f"),
        },
    }
    return inv.report


def _tokens_differ(line: InvoiceLine, led: LedgerLine, tolerance_pct: Decimal) -> bool:
    for billed, ours in ((line.prompt_tokens, led.prompt_tokens), (line.completion_tokens, led.completion_tokens)):
        if billed is None:
            continue
        if not _within(Decimal(billed), Decimal(ours), tolerance_pct, Decimal(0)):
            return True
    return False
