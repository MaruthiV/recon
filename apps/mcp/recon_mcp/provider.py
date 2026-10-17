from __future__ import annotations

import httpx

from recon_domain.connector import ProviderRefund, ReadFailed


def _refund(d: dict) -> ProviderRefund:
    return ProviderRefund(
        id=d["id"],
        payment_intent=d["payment_intent"],
        amount_minor=d["amount"],
        currency=d["currency"],
        status=d["status"],
        metadata=dict(d.get("metadata") or {}),
        failure_reason=d.get("failure_reason"),
    )


class HttpRefundReader:
    """read-only provider client, holds a restricted key so it can't create refunds even if asked"""

    def __init__(self, client: httpx.AsyncClient, read_key: str, timeout: int = 10) -> None:
        self._c = client
        self._auth = {"Authorization": f"Bearer {read_key}"}
        self._timeout = timeout

    async def list_refunds(self, payment_intent: str) -> list[ProviderRefund]:
        out: list[ProviderRefund] = []
        after: str | None = None
        while True:
            params = {"payment_intent": payment_intent, "limit": "100", **({"starting_after": after} if after else {})}
            try:
                r = await self._c.get("/v1/refunds", params=params, headers=self._auth, timeout=self._timeout)
            except httpx.TransportError as e:
                raise ReadFailed(str(e)) from e
            if r.status_code != 200:
                raise ReadFailed(f"list refunds http {r.status_code}")
            page = r.json()
            out += [_refund(d) for d in page["data"]]
            if not page["has_more"] or not page["data"]:
                return out
            after = page["data"][-1]["id"]

    async def get_refund(self, refund_id: str) -> ProviderRefund | None:
        try:
            r = await self._c.get(f"/v1/refunds/{refund_id}", headers=self._auth, timeout=self._timeout)
        except httpx.TransportError as e:
            raise ReadFailed(str(e)) from e
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise ReadFailed(f"get refund http {r.status_code}")
        return _refund(r.json())
