"""Load-test harness CLI (docs/spec/06): concurrent chat / stream / embeddings traffic against a running gateway,
optionally a baseline pass straight at the upstream to report the gateway's *added* TTFB per percentile, and
threshold flags that turn the run into a CI gate.

Examples (compose stack: gateway :8080, mock upstream :9000):

    python scripts/loadtest.py --key aigw_… --requests 500 --concurrency 32 --upstream http://localhost:9000
    python scripts/loadtest.py --key aigw_… --duration 60 --stream-ratio 0.8 --steer slow --json out.json \
        --max-error-rate 0.01 --max-added-p95-ms 25
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

import httpx

from aigw.testing.loadtest import LoadConfig, added_latency, check_thresholds, run, to_json


def _fmt(ms: float | None) -> str:
    return "—" if ms is None else f"{ms:8.1f}"


def print_summary(s: dict) -> None:
    print(
        f"\n== {s['label']}: {s['requests']} requests, {s['ok']} ok, {s['rps']:.1f} rps, {s['elapsed_s']} s  kinds={s['by_kind']}"
    )
    print(
        f"   ttfb  ms  p50 {_fmt(s['ttfb_ms']['p50'])}  p95 {_fmt(s['ttfb_ms']['p95'])}  p99 {_fmt(s['ttfb_ms']['p99'])}  mean {_fmt(s['ttfb_ms']['mean'])}"
    )
    print(
        f"   total ms  p50 {_fmt(s['total_ms']['p50'])}  p95 {_fmt(s['total_ms']['p95'])}  p99 {_fmt(s['total_ms']['p99'])}"
    )
    print(
        f"   tokens prompt={s['tokens']['prompt']} completion={s['tokens']['completion']}  cache={s['cache'] or '-'}  deployments={s['deployments'] or '-'}"
    )
    if s["errors"]:
        print(f"   errors ({s['error_rate']:.1%}): {s['errors']}")


async def main(a: argparse.Namespace) -> int:
    cfg = LoadConfig(
        model=a.model,
        requests=a.requests,
        concurrency=a.concurrency,
        duration_seconds=a.duration,
        stream_ratio=a.stream_ratio,
        embeddings_ratio=a.embeddings_ratio,
        embeddings_model=a.embeddings_model,
        prompt_words=a.prompt_words,
        max_tokens=a.max_tokens,
        steer=a.steer,
        seed=a.seed,
    )
    limits = httpx.Limits(max_connections=a.concurrency * 2, max_keepalive_connections=a.concurrency)
    timeout = httpx.Timeout(a.timeout)
    async with httpx.AsyncClient(base_url=a.gateway, limits=limits, timeout=timeout) as gw:
        gateway = await run(gw, cfg, headers={"authorization": f"Bearer {a.key}"}, label="gateway")
    summary = gateway.summary()
    print_summary(summary)
    added = None
    if a.upstream:
        async with httpx.AsyncClient(base_url=a.upstream, limits=limits, timeout=timeout) as up:
            upstream = await run(up, cfg, label="upstream (baseline)", model=a.upstream_model)
        print_summary(upstream.summary())
        added = added_latency(gateway, upstream)
        print(
            f"\n== added gateway latency (gateway TTFB − upstream TTFB): p50 {_fmt(added['p50'])}  p95 {_fmt(added['p95'])}  p99 {_fmt(added['p99'])}  mean {_fmt(added['mean'])} ms"
        )
    if a.json:
        out = {"gateway": to_json(gateway), "config": cfg.__dict__, "added_latency_ms": added}
        if a.upstream:
            out["upstream"] = to_json(upstream)
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=1)
        print(f"\nwrote {a.json}")
    bad = check_thresholds(
        summary,
        max_error_rate=a.max_error_rate,
        max_p95_ms=a.max_p95_ms,
        min_rps=a.min_rps,
        added=added,
        max_added_p95_ms=a.max_added_p95_ms,
    )
    if bad:
        print("\nTHRESHOLDS VIOLATED: " + "; ".join(bad))
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--gateway", default="http://localhost:8080")
    p.add_argument(
        "--upstream", default=None, help="mock upstream base URL for the baseline pass (e.g. http://localhost:9000)"
    )
    p.add_argument("--upstream-model", default="mock-chat", help="model name the upstream expects")
    p.add_argument("--key", default=os.environ.get("AIGW_LOADTEST_KEY"), help="virtual key (or AIGW_LOADTEST_KEY)")
    p.add_argument("--model", default="local-chat")
    p.add_argument("--embeddings-model", default="local-embed")
    p.add_argument("--requests", type=int, default=200)
    p.add_argument("--duration", type=float, default=None, help="seconds; overrides --requests")
    p.add_argument("--concurrency", type=int, default=16)
    p.add_argument("--stream-ratio", type=float, default=0.5)
    p.add_argument("--embeddings-ratio", type=float, default=0.0)
    p.add_argument("--prompt-words", type=int, default=120)
    p.add_argument("--max-tokens", type=int, default=64)
    p.add_argument("--steer", default=None, help="mock upstream `user` steering, e.g. slow")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--timeout", type=float, default=60.0)
    p.add_argument("--json", default=None, help="write the full report here")
    p.add_argument("--max-error-rate", type=float, default=None)
    p.add_argument("--max-p95-ms", type=float, default=None, help="gateway TTFB p95 ceiling")
    p.add_argument("--min-rps", type=float, default=None)
    p.add_argument(
        "--max-added-p95-ms", type=float, default=None, help="ceiling on gateway-added TTFB p95 (needs --upstream)"
    )
    args = p.parse_args()
    if not args.key:
        sys.exit(
            "a virtual key is required (--key or AIGW_LOADTEST_KEY); see `docker compose logs migrate | grep key:`"
        )
    sys.exit(asyncio.run(main(args)))
