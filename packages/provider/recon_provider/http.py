from __future__ import annotations

import httpx

from recon_domain.connector import ProviderRefund, ReadFailed, SubmitKind, SubmitResult


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


class HttpConnector:
    """stripe v1 refunds over http, used against the simulator"""

    def __init__(self, client: httpx.AsyncClient, api_key: str, timeout: int = 10) -> None:
        self._c = client
        self._auth = {"Authorization": f"Bearer {api_key}"}
        self._timeout = timeout

    async def create_refund(
        self,
        *,
        payment_intent: str,
        amount_minor: int,
        currency: str,
        idempotency_key: str,
        metadata: dict[str, str],
    ) -> SubmitResult:
        data = {"payment_intent": payment_intent, "amount": str(amount_minor)}
        data |= {f"metadata[{k}]": v for k, v in sorted(metadata.items())}
        try:
            r = await self._c.post(
                "/v1/refunds",
                data=data,
                timeout=self._timeout,
                headers={**self._auth, "Idempotency-Key": idempotency_key},
            )
        except httpx.TransportError:
            # timeout, reset, refused: the provider may or may not have done it
            return SubmitResult(SubmitKind.UNKNOWN)
        replayed = r.headers.get("Idempotent-Replayed") == "true"
        rid = r.headers.get("Request-Id")
        if r.status_code == 200:
            body = r.json()
            if body.get("currency") != currency:
                return SubmitResult(SubmitKind.MISMATCH, _refund(body), 200, replayed=replayed, request_id=rid)
            return SubmitResult(SubmitKind.APPLIED, _refund(body), 200, replayed=replayed, request_id=rid)
        err = (r.json().get("error") or {}) if r.headers.get("content-type", "").startswith("application/json") else {}
        etype, code = err.get("type"), err.get("code")
        kind = SubmitKind.UNKNOWN
        if r.status_code == 400 and etype == "idempotency_error":
            kind = SubmitKind.MISMATCH
        elif r.status_code in (400, 401, 402, 403, 404):
            kind = SubmitKind.REJECTED
        return SubmitResult(kind, None, r.status_code, etype, code, replayed, rid)

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
