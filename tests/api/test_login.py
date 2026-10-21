import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from recon_storage.db import make_engine, url
from recon_storage.users import demo_password
from tests.api.test_api import api, login  # noqa: F401
from tests.conftest import NAMES


async def attempt(c, user="operator", password=None, tenant="t_acme", **extra):
    body = {"user": user, "password": demo_password() if password is None else password, "tenant_id": tenant}
    return await c.post("/v1/login", json=body | extra)


async def test_the_role_comes_from_the_membership_not_the_form(api):  # noqa: F811
    r = await attempt(api.c, role="supervisor")
    assert r.status_code == 200 and r.json()["role"] == "operator"
    assert (await api.c.get("/v1/me")).json()["role"] == "operator"


@pytest.mark.parametrize(
    "kw",
    [
        {"password": "not-it"},
        {"user": "nobody"},
        # a real user and password, but a tenant they don't belong to
        {"tenant": "t_nowhere"},
    ],
)
async def test_every_way_of_getting_it_wrong_reads_the_same(api, kw):  # noqa: F811
    r = await attempt(api.c, **kw)
    assert r.status_code == 401 and r.json() == {"detail": "wrong name or password"}
    assert (await api.c.get("/v1/me")).status_code == 401


async def test_five_misses_lock_the_account_for_a_while(api, clock):  # noqa: F811
    for _ in range(5):
        assert (await attempt(api.c, password="guess")).status_code == 401
    # locked, so even the right password is turned away
    assert (await attempt(api.c)).status_code == 401
    clock.advance(minutes=6)
    assert (await attempt(api.c)).status_code == 200


async def test_a_good_sign_in_resets_the_miss_count(api):  # noqa: F811
    for _ in range(4):
        await attempt(api.c, password="guess")
    assert (await attempt(api.c)).status_code == 200
    for _ in range(4):
        await attempt(api.c, password="guess")
    assert (await attempt(api.c)).status_code == 200


async def test_passwords_are_stored_hashed(api, app_sessions):  # noqa: F811
    async with app_sessions() as s:
        hashes = (await s.execute(text("SELECT password_hash FROM users"))).scalars().all()
    assert hashes and all(h.startswith("scrypt$") and demo_password() not in h for h in hashes)


@pytest.mark.parametrize("role", ["recon_eval", "recon_mcp_read"])
async def test_the_verifier_and_the_agents_cant_read_users(api, role):  # noqa: F811
    engine = make_engine(url(role, NAMES.recon))
    try:
        for table in ("users", "memberships"):
            async with engine.connect() as c:
                with pytest.raises(DBAPIError, match="permission denied"):
                    await c.execute(text(f"SELECT * FROM {table}"))
    finally:
        await engine.dispose()
