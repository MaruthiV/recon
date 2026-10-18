from __future__ import annotations

import hashlib
from datetime import datetime


def duplicate_capture_ref(order_id: str, payment_id: str) -> str:
    return f"dup:{order_id}:{payment_id}"


def return_ref(order_id: str, return_id: str) -> str:
    return f"return:{order_id}:{return_id}"


def prorate_ref(subscription_id: str, period_end: datetime) -> str:
    # one entitlement per paid period, a second cancellation ticket lands on the same claim
    return f"prorate:{subscription_id}:{period_end.date().isoformat()}"


def command_id_for(tenant_id: str, entitlement_ref: str, op_seq: int) -> str:
    # stable logical operation id, so duplicate prepares collide on the pk too
    digest = hashlib.sha256(f"{tenant_id}|{entitlement_ref}|{op_seq}".encode()).hexdigest()
    return f"cmd_{digest[:24]}"


def idempotency_key_for(command_id: str) -> str:
    return f"recon_{command_id}"
