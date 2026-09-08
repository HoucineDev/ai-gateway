"""AWS Signature Version 4 (docs/spec/03 §5) — owned implementation, no AWS SDK.

Signs an HTTP request with an access key / secret (+ optional session token) for one region and service, and
exposes the canonical pieces so a test double can verify a signature with the same code path.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit

ALGORITHM = "AWS4-HMAC-SHA256"
_UNRESERVED = "-_.~"


@dataclass(frozen=True)
class AwsCredentials:
    access_key_id: str
    secret_access_key: str
    session_token: str | None = None

    @classmethod
    def parse(cls, credential: str) -> AwsCredentials:
        """``ACCESS:SECRET[:SESSION]`` or a JSON object with ``access_key_id`` / ``secret_access_key`` /
        ``session_token`` (also accepts the AWS CLI names ``aws_access_key_id`` …)."""
        import json

        credential = credential.strip()
        if credential.startswith("{"):
            d = json.loads(credential)
            ak = d.get("access_key_id") or d.get("aws_access_key_id") or d.get("AccessKeyId")
            sk = d.get("secret_access_key") or d.get("aws_secret_access_key") or d.get("SecretAccessKey")
            st = d.get("session_token") or d.get("aws_session_token") or d.get("SessionToken")
            if not ak or not sk:
                raise ValueError("credential JSON needs access_key_id and secret_access_key")
            return cls(str(ak), str(sk), str(st) if st else None)
        parts = credential.split(":", 2)
        if len(parts) < 2 or not parts[0] or not parts[1]:
            raise ValueError("credential must be ACCESS_KEY_ID:SECRET_ACCESS_KEY[:SESSION_TOKEN] or JSON")
        return cls(parts[0], parts[1], parts[2] if len(parts) == 3 and parts[2] else None)


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_uri(path: str) -> str:
    """Canonical URI for non-S3 services: the request path (already URI-encoded once, e.g. ``:`` → ``%3A`` in
    Bedrock model ids) encoded a second time, as the SigV4 spec and the AWS SDKs do."""
    return quote(path or "/", safe="/" + _UNRESERVED)


def canonical_query(query: str) -> str:
    if not query:
        return ""
    pairs = []
    for item in query.split("&"):
        if not item:
            continue
        k, _, v = item.partition("=")
        pairs.append((quote(k, safe=_UNRESERVED), quote(v, safe=_UNRESERVED)))
    return "&".join(f"{k}={v}" for k, v in sorted(pairs))


def canonical_request(method: str, url: str, headers: dict[str, str], signed: list[str], payload_hash: str) -> str:
    u = urlsplit(url)
    lowered = {k.lower(): " ".join(v.strip().split()) for k, v in headers.items()}
    canon_headers = "".join(f"{h}:{lowered[h]}\n" for h in signed)
    return "\n".join(
        [method.upper(), canonical_uri(u.path), canonical_query(u.query), canon_headers, ";".join(signed), payload_hash]
    )


def signing_key(secret: str, date: str, region: str, service: str) -> bytes:
    k = _hmac(("AWS4" + secret).encode(), date)
    k = _hmac(k, region)
    k = _hmac(k, service)
    return _hmac(k, "aws4_request")


def sign(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes,
    creds: AwsCredentials,
    region: str,
    service: str,
    now: datetime | None = None,
) -> dict[str, str]:
    """Return ``headers`` plus ``host``, ``x-amz-date``, ``x-amz-content-sha256``, optional
    ``x-amz-security-token`` and the ``authorization`` header."""
    now = now or datetime.now(UTC)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date = amz_date[:8]
    u = urlsplit(url)
    out = {k: v for k, v in headers.items()}
    out["host"] = u.netloc
    out["x-amz-date"] = amz_date
    payload_hash = sha256_hex(body)
    out["x-amz-content-sha256"] = payload_hash
    if creds.session_token:
        out["x-amz-security-token"] = creds.session_token
    signed = sorted(
        k.lower()
        for k in out
        if k.lower() in {"content-type", "host", "x-amz-date", "x-amz-content-sha256", "x-amz-security-token"}
    )
    creq = canonical_request(method, url, out, signed, payload_hash)
    scope = f"{date}/{region}/{service}/aws4_request"
    sts = "\n".join([ALGORITHM, amz_date, scope, sha256_hex(creq.encode())])
    sig = hmac.new(
        signing_key(creds.secret_access_key, date, region, service), sts.encode(), hashlib.sha256
    ).hexdigest()
    out["authorization"] = (
        f"{ALGORITHM} Credential={creds.access_key_id}/{scope}, SignedHeaders={';'.join(signed)}, Signature={sig}"
    )
    return out


def parse_authorization(value: str) -> dict[str, str]:
    """Split an ``AWS4-HMAC-SHA256 Credential=…, SignedHeaders=…, Signature=…`` header into its parts."""
    if not value.startswith(ALGORITHM + " "):
        raise ValueError("not a SigV4 authorization header")
    out: dict[str, str] = {}
    for part in value[len(ALGORITHM) + 1 :].split(","):
        k, _, v = part.strip().partition("=")
        out[k] = v
    return out
