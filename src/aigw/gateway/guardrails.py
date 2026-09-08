"""Guardrail pipeline (docs/spec/04 §11): per-project pre (request) and post (response) detector chains with
block / redact / flag actions, per-rule timeouts and fail-open / fail-closed behaviour.

Built-in detectors need no dependency: ``pii`` (email, phone, IBAN, payment card with Luhn check), ``regex``,
``keyword``. ``http`` posts the text to any HTTP detector (Presidio / LLM Guard / provider moderation behind a
thin sidecar) with a small JSON contract: ``{"text", "direction", "request_id", "project_id", "metadata"}`` →
``{"flagged": bool, "categories": [..], "redacted_text": str | null}``. Detectors are replaceable: register a new
name in ``DETECTORS``. Every outcome is recorded in ``guardrail_events`` (audit trail) and counted in
``aigw_guardrail_total``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

ACTIONS = ("block", "redact", "flag")
FAIL_MODES = ("open", "closed")
STREAM_MODES = ("tail", "buffer")
DEFAULT_TIMEOUT_MS = 1000


@dataclass(frozen=True)
class Rule:
    detector: str
    action: str = "block"
    fail: str = "closed"
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class Verdict:
    flagged: bool = False
    categories: list[str] = field(default_factory=list)
    text: str | None = None  # redacted text when the detector can redact


@dataclass
class Outcome:
    rule: Rule
    action: str  # block | redact | flag | error
    categories: list[str]
    latency_ms: int
    error: str | None = None


@dataclass(frozen=True)
class GuardrailPolicy:
    pre: tuple[Rule, ...] = ()
    post: tuple[Rule, ...] = ()
    post_stream: str = "tail"  # tail: check the finished stream, block ends it with an error event; buffer: hold it

    @classmethod
    def from_project_settings(cls, settings: dict | None) -> GuardrailPolicy | None:
        """`projects.settings.guardrails = {"pre": [rule…], "post": [rule…], "post_stream": "tail"|"buffer"}`.
        Raises ValueError on an invalid policy (the snapshot then fails the project closed)."""
        cfg = (settings or {}).get("guardrails")
        if not cfg:
            return None
        if not isinstance(cfg, dict):
            raise ValueError("guardrails must be an object")
        mode = str(cfg.get("post_stream") or "tail")
        if mode not in STREAM_MODES:
            raise ValueError(f"guardrails.post_stream must be one of {STREAM_MODES}")
        pre = tuple(_rule(r, f"pre[{i}]") for i, r in enumerate(cfg.get("pre") or []))
        post = tuple(_rule(r, f"post[{i}]") for i, r in enumerate(cfg.get("post") or []))
        if not pre and not post:
            return None
        return cls(pre, post, mode)


def _rule(raw: Any, where: str) -> Rule:
    if not isinstance(raw, dict) or not raw.get("detector"):
        raise ValueError(f"guardrails.{where}: rule needs a detector")
    detector = str(raw["detector"])
    if detector not in DETECTORS:
        raise ValueError(f"guardrails.{where}: unknown detector {detector!r} (known: {sorted(DETECTORS)})")
    action = str(raw.get("action") or "block")
    if action not in ACTIONS:
        raise ValueError(f"guardrails.{where}: action must be one of {ACTIONS}")
    fail = str(raw.get("fail") or "closed")
    if fail not in FAIL_MODES:
        raise ValueError(f"guardrails.{where}: fail must be one of {FAIL_MODES}")
    timeout_ms = int(raw.get("timeout_ms") or DEFAULT_TIMEOUT_MS)
    params = {k: v for k, v in raw.items() if k not in ("detector", "action", "fail", "timeout_ms")}
    DETECTORS[detector].validate(params, where)
    return Rule(detector, action, fail, timeout_ms, params)


# ---- detectors ------------------------------------------------------------


class Detector:
    name = "base"

    def validate(self, params: dict, where: str) -> None:  # noqa: ARG002
        return None

    async def check(self, text: str, params: dict, context: dict, http: httpx.AsyncClient | None) -> Verdict:
        raise NotImplementedError


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


PII_PATTERNS: dict[str, re.Pattern[str]] = {
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "phone": re.compile(
        r"(?<![\w.])(?:\+\d{1,3}[\s.-]?)?(?:\(?\d{2,4}\)?[\s.-]?)?\d{2,4}[\s.-]?\d{2,4}[\s.-]?\d{2,4}(?![\w.])"
    ),
    "iban": re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}[ ]?[A-Z0-9]{1,4}\b"),
    "card": re.compile(r"\b(?:\d[ -]?){13,19}\b"),
}


class PIIDetector(Detector):
    name = "pii"

    def validate(self, params: dict, where: str) -> None:
        kinds = params.get("kinds")
        if kinds is not None and (not isinstance(kinds, list) or not set(kinds) <= set(PII_PATTERNS)):
            raise ValueError(f"guardrails.{where}: pii.kinds must be a subset of {sorted(PII_PATTERNS)}")

    async def check(self, text: str, params: dict, context: dict, http) -> Verdict:  # noqa: ARG002
        kinds = params.get("kinds") or list(PII_PATTERNS)
        found: list[str] = []
        out = text

        def sub(kind: str):
            def repl(m: re.Match) -> str:
                value = m.group(0)
                digits = re.sub(r"\D", "", value)
                if kind == "card" and not (13 <= len(digits) <= 19 and _luhn(digits)):
                    return value
                if kind == "phone" and len(digits) < 8:
                    return value
                if kind not in found:
                    found.append(kind)
                return f"[{kind.upper()}]"

            return repl

        for kind in ("card", "iban", "email", "phone"):  # cards before phones: a card number is all digits too
            if kind in kinds:
                out = PII_PATTERNS[kind].sub(sub(kind), out)
        return Verdict(flagged=bool(found), categories=[f"pii:{k}" for k in found], text=out if found else None)


class RegexDetector(Detector):
    name = "regex"

    def validate(self, params: dict, where: str) -> None:
        pats = params.get("patterns")
        if not isinstance(pats, list) or not pats:
            raise ValueError(f"guardrails.{where}: regex.patterns must be a non-empty list")
        for p in pats:
            if not isinstance(p, dict) or not p.get("pattern"):
                raise ValueError(f"guardrails.{where}: each pattern needs `pattern`")
            try:
                re.compile(p["pattern"], re.IGNORECASE if p.get("ignore_case", True) else 0)
            except re.error as exc:
                raise ValueError(f"guardrails.{where}: invalid pattern {p['pattern']!r}: {exc}") from None

    async def check(self, text: str, params: dict, context: dict, http) -> Verdict:  # noqa: ARG002
        found: list[str] = []
        out = text
        for p in params["patterns"]:
            rx = re.compile(p["pattern"], re.IGNORECASE if p.get("ignore_case", True) else 0)
            if rx.search(out):
                cat = str(p.get("category") or "regex")
                if cat not in found:
                    found.append(cat)
                out = rx.sub(str(p.get("replacement") or "[REDACTED]"), out)
        return Verdict(flagged=bool(found), categories=found, text=out if found else None)


class KeywordDetector(Detector):
    name = "keyword"

    def validate(self, params: dict, where: str) -> None:
        words = params.get("words")
        if not isinstance(words, list) or not words or not all(isinstance(w, str) and w for w in words):
            raise ValueError(f"guardrails.{where}: keyword.words must be a non-empty list of strings")

    async def check(self, text: str, params: dict, context: dict, http) -> Verdict:  # noqa: ARG002
        found: list[str] = []
        out = text
        for w in params["words"]:
            rx = re.compile(r"(?<!\w)" + re.escape(w) + r"(?!\w)", re.IGNORECASE)
            if rx.search(out):
                found.append(w.lower())
                out = rx.sub("***", out)
        return Verdict(flagged=bool(found), categories=[f"keyword:{w}" for w in found], text=out if found else None)


class HTTPDetector(Detector):
    """Generic HTTP detector contract; wrap Presidio, LLM Guard or a provider moderation API behind it."""

    name = "http"

    def validate(self, params: dict, where: str) -> None:
        url = params.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ValueError(f"guardrails.{where}: http.url must be an http(s) URL")

    async def check(self, text: str, params: dict, context: dict, http: httpx.AsyncClient | None) -> Verdict:
        if http is None:
            raise RuntimeError("no HTTP client for guardrail detector")
        payload = {"text": text, **context, "metadata": params.get("metadata") or {}}
        r = await http.post(params["url"], json=payload, headers=params.get("headers") or {})
        if r.status_code >= 400:
            raise RuntimeError(f"detector returned HTTP {r.status_code}")
        data = r.json()
        cats = [str(c) for c in (data.get("categories") or [])]
        return Verdict(flagged=bool(data.get("flagged")), categories=cats, text=data.get("redacted_text"))


DETECTORS: dict[str, Detector] = {
    d.name: d for d in (PIIDetector(), RegexDetector(), KeywordDetector(), HTTPDetector())
}


# ---- runner ----------------------------------------------------------------


class GuardrailRunner:
    def __init__(self, http: httpx.AsyncClient | None = None):
        self.http = http

    async def run(self, rules: tuple[Rule, ...], text: str, context: dict) -> tuple[str, list[Outcome]]:
        """Apply the rules in order. Returns the (possibly redacted) text and every outcome; a `block` outcome
        is always last. A failing detector blocks when `fail: closed`, else records an `error` outcome and
        moves on."""
        outcomes: list[Outcome] = []
        for rule in rules:
            started = time.perf_counter()
            try:
                async with asyncio.timeout(rule.timeout_ms / 1000):
                    verdict = await DETECTORS[rule.detector].check(text, rule.params, context, self.http)
            except (TimeoutError, Exception) as exc:  # noqa: BLE001 - any detector failure is a policy decision
                latency = int((time.perf_counter() - started) * 1000)
                reason = "timeout" if isinstance(exc, TimeoutError) else f"{type(exc).__name__}: {exc}"
                log.warning("guardrail %s (%s) failed: %s", rule.detector, context.get("direction"), reason)
                if rule.fail == "closed":
                    outcomes.append(Outcome(rule, "block", ["detector_error"], latency, error=reason))
                    return text, outcomes
                outcomes.append(Outcome(rule, "error", ["detector_error"], latency, error=reason))
                continue
            latency = int((time.perf_counter() - started) * 1000)
            if not verdict.flagged:
                continue
            action = rule.action
            if action == "redact" and verdict.text is not None:
                text = verdict.text
            elif action == "redact":
                action = "flag"  # detector cannot redact: record it, let the text through
            outcomes.append(Outcome(rule, action, verdict.categories, latency))
            if action == "block":
                break
        return text, outcomes
