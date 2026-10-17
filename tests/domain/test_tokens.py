from datetime import UTC, datetime, timedelta

import pytest

from recon_domain.tokens import BadToken, mint, verify

KEY = b"k1"
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


def claims(**kw):
    return {"aud": "mcp-read", "tenant": "t_acme", "exp": int((NOW + timedelta(minutes=5)).timestamp()), **kw}


def test_roundtrip():
    assert verify(mint(claims(), KEY), KEY, "mcp-read", NOW)["tenant"] == "t_acme"


@pytest.mark.parametrize(
    ("token", "key", "aud", "now", "why"),
    [
        (mint(claims(), b"other"), KEY, "mcp-read", NOW, "signature"),
        (mint(claims(aud="mcp-exec"), KEY), KEY, "mcp-read", NOW, "audience"),
        (mint(claims(), KEY), KEY, "mcp-read", NOW + timedelta(minutes=5), "expired"),
        ("", KEY, "mcp-read", NOW, "signature"),
        ("garbage", KEY, "mcp-read", NOW, "signature"),
    ],
)
def test_rejects(token, key, aud, now, why):
    with pytest.raises(BadToken, match=why):
        verify(token, key, aud, now)


def test_tampered_tenant_is_rejected():
    body, sig = mint(claims(), KEY).split(".")
    forged = mint(claims(tenant="t_globex"), KEY).split(".")[0]
    with pytest.raises(BadToken):
        verify(f"{forged}.{sig}", KEY, "mcp-read", NOW)
    assert body != forged
