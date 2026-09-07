"""Smoke test for Keycloak OIDC on the control API (docs/spec/01 §3.1) against a running stack.

Fetches password-grant tokens for the dev realm users (deploy/compose/keycloak/realm-aigw.json), calls
GET /admin/v1/me and attempts an organization create with each, and checks the expected outcome:

    alice  aigw-admin     -> 201 created (audit actor_type=user, actor_id=alice)
    bob    aigw-operator  -> 403 insufficient_scope
    carol  aigw-viewer    -> 403 insufficient_scope
    dan    (no role)      -> 403 missing_role

Usage:  python scripts/oidc_smoke.py [--keycloak http://localhost:8180] [--admin http://localhost:8081]
        [--realm aigw] [--client aigw-portal] [--admin-key change-me-admin]
Exit code 0 when every expectation holds. Uses only httpx (no jq / curl needed).
"""

from __future__ import annotations

import argparse
import sys
import uuid

import httpx

EXPECTED = {  # user -> (password, expected status on POST /organizations, expected error code or None)
    "alice": ("alice", 201, None),
    "bob": ("bob", 403, "insufficient_scope"),
    "carol": ("carol", 403, "insufficient_scope"),
    "dan": ("dan", 403, "missing_role"),
}


def token(kc: httpx.Client, realm: str, client_id: str, user: str, password: str) -> str:
    r = kc.post(
        f"/realms/{realm}/protocol/openid-connect/token",
        data={"grant_type": "password", "client_id": client_id, "username": user, "password": password},
    )
    if r.status_code != 200:
        sys.exit(f"token request for {user} failed: {r.status_code} {r.text}")
    return r.json()["access_token"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keycloak", default="http://localhost:8180")
    ap.add_argument("--admin", default="http://localhost:8081")
    ap.add_argument("--realm", default="aigw")
    ap.add_argument("--client", default="aigw-portal")
    ap.add_argument("--admin-key", default="change-me-admin")
    args = ap.parse_args()

    ok = True
    with httpx.Client(base_url=args.keycloak, timeout=10) as kc, httpx.Client(base_url=args.admin, timeout=10) as gw:
        for user, (password, want_status, want_code) in EXPECTED.items():
            tok = token(kc, args.realm, args.client, user, password)
            h = {"authorization": f"Bearer {tok}"}
            me = gw.get("/admin/v1/me", headers=h)
            slug = f"smoke-{user}-{uuid.uuid4().hex[:6]}"
            r = gw.post("/admin/v1/organizations", json={"name": f"Smoke {user}", "slug": slug}, headers=h)
            code = r.json().get("error", {}).get("code") if r.status_code >= 400 else None
            good = r.status_code == want_status and code == want_code
            if me.status_code == 200:
                body = me.json()
                me_txt = (
                    f"me={body['actor_type']}:{body['actor_id']} roles={body['roles']} scopes={len(body['scopes'])}"
                )
            else:
                me_txt = f"me={me.status_code} {me.json().get('error', {}).get('code')}"
                good = good and want_code == "missing_role"  # /me itself is refused for a role-less token
            print(
                f"{'PASS' if good else 'FAIL'} {user:6} {me_txt}  POST /organizations -> {r.status_code} {code or ''}"
            )
            ok &= good
            if user == "alice" and r.status_code == 201:
                audit = gw.get(
                    "/admin/v1/audit",
                    params={"target_type": "organization", "target_id": r.json()["id"]},
                    headers={"x-admin-key": args.admin_key},
                )
                rows = audit.json().get("data", []) if audit.status_code == 200 else []
                actor = (rows[0]["actor_type"], rows[0]["actor_id"]) if rows else None
                good = actor == ("user", "alice")
                print(f"{'PASS' if good else 'FAIL'} audit  actor={actor} (via admin key)")
                ok &= good
    print("ALL PASS" if ok else "FAILURES")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
