from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime

RUN_TOKEN_HEADER = "x-recon-run-token"
MCP_READ = "mcp-read"


class BadToken(Exception):
    pass


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(body: str, key: bytes) -> str:
    return _b64(hmac.new(key, body.encode(), hashlib.sha256).digest())


def mint(claims: dict, key: bytes) -> str:
    body = _b64(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode())
    return f"{body}.{_sign(body, key)}"


def verify(token: str, key: bytes, aud: str, now: datetime) -> dict:
    body, _, sig = token.partition(".")
    if not body or not sig or not hmac.compare_digest(sig, _sign(body, key)):
        raise BadToken("bad signature")
    try:
        claims = json.loads(_unb64(body))
    except ValueError as e:
        raise BadToken("bad body") from e
    if claims.get("aud") != aud:
        raise BadToken("wrong audience")
    if int(now.timestamp()) >= int(claims.get("exp", 0)):
        raise BadToken("expired")
    return claims
