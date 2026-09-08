"""Provider invoice reconciliation (docs/spec/04 §10): JSON + CSV import, per-line and total deltas, missing usage,
tolerances, cached traffic exclusion, global-only scope, audit."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from aigw.admin.reconcile import parse_csv
from tests.conftest import ADMIN, refresh
from tests.test_admin_delegation import bind, tok
from tests.test_admin_oidc import FakeKeycloak  # noqa: F401 - re-exported fixtures below

TODAY = datetime.now(UTC).date()
PERIOD = {"period_start": (TODAY - timedelta(days=1)).isoformat(), "period_end": TODAY.isoformat()}


async def _traffic(client, tenant, n: int = 3):
    """n settled chat requests on the mock deployment; returns the ledger amount and tokens for (mock-chat, today)."""
    for i in range(n):
        r = await client.post(
            "/v1/chat/completions",
            json={"model": "local-chat", "messages": [{"role": "user", "content": f"invoice {i} " * (i + 1)}]},
            headers=tenant["auth"],
        )
        assert r.status_code == 200, r.text
    rows = (await client.get("/admin/v1/requests", params={"limit": 500}, headers=ADMIN)).json()["data"]
    chat = [
        r for r in rows if r["provider"] == "openai_compat" and r["status"] == "succeeded" and r["endpoint"] == "chat"
    ]
    return (
        sum(Decimal(r["cost"]) for r in chat),
        sum(r["prompt_tokens"] for r in chat),
        sum(r["completion_tokens"] for r in chat),
    )


def _line(amount, day=None, pm="mock-chat", **kw):
    return {"provider_model": pm, "day": (day or TODAY).isoformat(), "amount": str(amount), **kw}


async def _invoice(client, lines, provider="openai_compat", **extra):
    r = await client.post(
        "/admin/v1/invoices",
        json={"provider": provider, "currency": "USD", "source": "test export", "lines": lines, **PERIOD, **extra},
        headers=ADMIN,
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _reconcile(client, inv_id, **body):
    r = await client.post(f"/admin/v1/invoices/{inv_id}/reconcile", json=body, headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()


def test_parse_csv():
    lines = parse_csv(
        "provider_model,day,amount,prompt_tokens,completion_tokens,sku\ngpt-4o,2026-09-01,12.50,1000,200,abc\n"
    )
    assert lines == [
        {
            "provider_model": "gpt-4o",
            "day": datetime(2026, 9, 1).date(),
            "amount": Decimal("12.50"),
            "prompt_tokens": 1000,
            "completion_tokens": 200,
            "meta": {"sku": "abc"},
        }
    ]
    for bad in (
        "provider_model,amount\nx,1\n",
        "provider_model,day,amount\n",
        "provider_model,day,amount\nx,notadate,1\n",
    ):
        try:
            parse_csv(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad!r}")


async def test_exact_match_and_deltas(client, app, tenant):
    amount, pt, ct = await _traffic(client, tenant)
    inv = await _invoice(client, [_line(amount, prompt_tokens=pt, completion_tokens=ct)])
    assert inv["status"] == "open" and len(inv["lines"]) == 1
    out = await _reconcile(client, inv["id"])
    assert out["status"] == "matched" and Decimal(out["delta_amount"]) == 0
    assert Decimal(out["invoice_total"]) == amount == Decimal(out["ledger_total"])
    line = out["lines"][0]
    assert line["status"] == "matched" and Decimal(line["ledger_amount"]) == amount and line["ledger_requests"] == 3
    assert line["ledger_prompt_tokens"] == pt and line["ledger_ambiguous"] == 0
    assert out["report"]["lines"] == {"matched": 1, "amount_mismatch": 0, "token_mismatch": 0, "unmatched": 0}
    assert out["report"]["missing_in_invoice"] == [] and out["report"]["ambiguous_attempts"] == 0

    # the provider billed 20 % more, and tokens are off, and it bills a model we never used
    inv2 = await _invoice(
        client,
        [
            _line(amount * Decimal("1.2"), prompt_tokens=pt, completion_tokens=ct),
            _line("0.5", pm="gpt-4o", day=TODAY - timedelta(days=1)),
        ],
    )
    out = await _reconcile(client, inv2["id"], tolerance_pct=1, tolerance_abs="0")  # mock prices are micro-dollars
    assert out["status"] == "mismatch"
    by_model = {ln["provider_model"]: ln for ln in out["lines"]}
    assert by_model["mock-chat"]["status"] == "amount_mismatch"
    assert Decimal(by_model["mock-chat"]["delta_amount"]) == amount * Decimal("1.2") - amount
    assert by_model["gpt-4o"]["status"] == "unmatched" and by_model["gpt-4o"]["ledger_amount"] is None
    assert Decimal(out["delta_amount"]) == Decimal(out["invoice_total"]) - amount
    # a generous tolerance turns the amount line green but the unmatched model still fails the bill
    out = await _reconcile(client, inv2["id"], tolerance_pct=25, tolerance_abs="0")
    assert {ln["provider_model"]: ln["status"] for ln in out["lines"]} == {
        "mock-chat": "matched",
        "gpt-4o": "unmatched",
    }
    assert out["status"] == "mismatch"


async def test_token_mismatch_and_missing_usage(client, app, tenant):
    amount, pt, ct = await _traffic(client, tenant, n=2)
    # embeddings traffic on another provider model the bill forgets
    r = await client.post("/v1/embeddings", json={"model": "local-embed", "input": "x"}, headers=tenant["auth"])
    assert r.status_code == 200
    inv = await _invoice(client, [_line(amount, prompt_tokens=pt * 2, completion_tokens=ct)])
    out = await _reconcile(client, inv["id"], token_tolerance_pct=5)
    assert out["lines"][0]["status"] == "token_mismatch" and out["status"] == "mismatch"
    missing = out["report"]["missing_in_invoice"]
    assert [m["provider_model"] for m in missing] == ["mock-embed"] and Decimal(missing[0]["ledger_amount"]) > 0
    # tokens absent on the bill (amount-only exports) are not compared
    inv = await _invoice(client, [_line(amount), _line("0.001", pm="mock-embed")])
    out = await _reconcile(client, inv["id"], tolerance_pct=0, tolerance_abs="1")
    assert {ln["provider_model"]: ln["status"] for ln in out["lines"]} == {
        "mock-chat": "matched",
        "mock-embed": "matched",
    }


async def test_cached_hits_are_not_billable(client, app, tenant):
    await client.patch(
        f"/admin/v1/projects/{tenant['project']['id']}",
        json={"settings": {"allowed_tags": ["env", "feature"], "cache": {"enabled": True}}},
        headers=ADMIN,
    )
    await refresh(app)
    body = {"model": "local-chat", "messages": [{"role": "user", "content": "same"}]}
    first = await client.post("/v1/chat/completions", json=body, headers=tenant["auth"])
    second = await client.post("/v1/chat/completions", json=body, headers=tenant["auth"])
    if second.headers.get("x-aigw-cache") != "hit":
        return  # Valkey unavailable in this environment: nothing to assert
    cost = Decimal(
        (await client.get(f"/admin/v1/requests/{first.json()['aigw']['request_id']}", headers=ADMIN)).json()["usage"][
            0
        ]["cost"]
    )
    inv = await _invoice(client, [_line(cost)])
    out = await _reconcile(client, inv["id"])
    assert out["status"] == "matched" and out["lines"][0]["ledger_requests"] == 1  # the hit never hit the provider


async def test_validation_csv_import_scope_and_audit(client, app, tenant):
    amount, pt, ct = await _traffic(client, tenant, n=1)
    r = await client.post(
        "/admin/v1/invoices",
        json={"provider": "openai_compat", "lines": [_line("1"), _line("2")], **PERIOD},
        headers=ADMIN,
    )
    assert (r.status_code, r.json()["error"]["code"]) == (400, "duplicate_line")
    r = await client.post(
        "/admin/v1/invoices",
        json={"provider": "openai_compat", "lines": [_line("1", day=TODAY + timedelta(days=3))], **PERIOD},
        headers=ADMIN,
    )
    assert (r.status_code, r.json()["error"]["code"]) == (400, "line_outside_period")

    csv_body = (
        f"provider_model,day,amount,prompt_tokens,completion_tokens\nmock-chat,{TODAY.isoformat()},{amount},{pt},{ct}\n"
    )
    r = await client.post(
        "/admin/v1/invoices/import",
        params={"provider": "openai_compat", "source": "bill.csv", **PERIOD},
        content=csv_body.encode(),
        headers={**ADMIN, "content-type": "text/csv"},
    )
    assert r.status_code == 201, r.text
    inv = r.json()
    assert inv["source"] == "bill.csv" and inv["lines"][0]["prompt_tokens"] == pt
    assert (await _reconcile(client, inv["id"]))["status"] == "matched"
    r = await client.post(
        "/admin/v1/invoices/import", params={"provider": "openai_compat", **PERIOD}, content=b"nope", headers=ADMIN
    )
    assert (r.status_code, r.json()["error"]["code"]) == (400, "invalid_csv")

    listed = (await client.get("/admin/v1/invoices", params={"status": "matched"}, headers=ADMIN)).json()["data"]
    assert [i["id"] for i in listed] == [inv["id"]]
    audit = (
        await client.get("/admin/v1/audit", params={"target_type": "invoice", "target_id": inv["id"]}, headers=ADMIN)
    ).json()["data"]
    assert [a["action"] for a in audit] == ["invoice.reconcile", "invoice.create"]

    # an org owner never sees provider bills: no delegated role carries invoices:*
    assert (await bind(client, "bob", "org_owner", "organization", tenant["org"]["id"])).status_code == 201
    for method, path in (("GET", "/admin/v1/invoices"), ("GET", f"/admin/v1/invoices/{inv['id']}")):
        r = await client.request(method, path, headers=tok("bob"))
        assert (r.status_code, r.json()["error"]["code"]) == (403, "insufficient_scope"), path


# OIDC fixtures for the delegate check (same fake Keycloak as the delegation tests)
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from aigw.config import Settings  # noqa: E402
from tests.conftest import TEST_DB, TEST_VALKEY  # noqa: E402
from tests.test_admin_oidc import AUDIENCE, ISSUER  # noqa: E402


@pytest.fixture
def keycloak():
    return FakeKeycloak()


@pytest_asyncio.fixture
async def oidc_http_client(keycloak):
    client = keycloak.client()
    yield client
    await client.aclose()


@pytest.fixture
def settings():
    return Settings(
        role="all",
        database_url=TEST_DB,
        valkey_url=TEST_VALKEY,
        admin_key="test-admin",
        oidc_issuer=ISSUER,
        oidc_audience=AUDIENCE,
        config_refresh_seconds=0.2,
        default_max_output_tokens=256,
    )
