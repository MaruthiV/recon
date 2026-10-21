from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from .models import Membership, Tenant, User

N, R, P = 2**14, 8, 1
# local demo only, override with RECON_DEMO_PASSWORD
DEMO_PASSWORD = "recon-local-demo"
DEMO_ROLES = ("operator", "supervisor")


def demo_password() -> str:
    return os.environ.get("RECON_DEMO_PASSWORD") or DEMO_PASSWORD


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=N, r=R, p=P)
    return f"scrypt${N}${R}${P}${_b64(salt)}${_b64(digest)}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, want = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p))
    except ValueError:
        return False
    return hmac.compare_digest(got, base64.b64decode(want))


# checked against when the user doesn't exist, so a wrong name takes as long as a wrong password
DUMMY_HASH = hash_password(secrets.token_hex(8))


async def ensure_demo_users(
    sessions: async_sessionmaker, now: datetime, tenant_ids: Iterable[str] | None = None
) -> None:
    # one operator and one supervisor, members of every given tenant (all of them by default)
    password = demo_password()
    async with sessions() as s, s.begin():
        if tenant_ids is None:
            tenant_ids = list((await s.execute(select(Tenant.id))).scalars())
        for role in DEMO_ROLES:
            await s.execute(
                insert(User)
                .values(id=f"usr_{role}", username=role, password_hash=hash_password(password), created_at=now)
                .on_conflict_do_nothing(index_elements=["username"])
            )
            uid = (await s.execute(select(User.id).where(User.username == role))).scalar_one()
            for tid in tenant_ids:
                await s.execute(
                    insert(Membership)
                    .values(user_id=uid, tenant_id=tid, role=role)
                    .on_conflict_do_nothing(index_elements=["user_id", "tenant_id"])
                )
