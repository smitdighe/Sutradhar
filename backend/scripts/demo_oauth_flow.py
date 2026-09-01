"""Walk the new-user Google flow against the fake provider and print what happens.

Demonstrates the security property this phase exists to guarantee: the callback
for an identity nobody has seen before produces a 302 carrying a pending token
and **no session at all** -- no Set-Cookie, no access token. A session appears
only after /complete supplies a role.

Run from the backend directory::

    uv run python scripts/demo_oauth_flow.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

CLIENT_ID = "demo-client-id.apps.googleusercontent.com"
os.environ["GOOGLE_CLIENT_ID"] = CLIENT_ID
os.environ["GOOGLE_CLIENT_SECRET"] = "demo-client-secret"

import httpx  # noqa: E402
import respx  # noqa: E402
from sqlalchemy import delete, select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.models.user import OAuthIdentity, PendingToken, User  # noqa: E402
from app.db.session import get_session  # noqa: E402
from app.main import create_app  # noqa: E402
from tests.fakes.fake_google import fake_google  # noqa: E402

get_settings.cache_clear()
SETTINGS = get_settings()
OAUTH = f"{SETTINGS.api_prefix}/auth/oauth"
AUTH = f"{SETTINGS.api_prefix}/auth"
DEMO_EMAIL = "demo-oauth-user@gmail.example.com"


def rule(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


async def main() -> int:
    engine = create_async_engine(SETTINGS.database_url)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    # Start clean so the demo is repeatable.
    async with factory() as session:
        existing = (
            await session.execute(select(User).where(User.email == DEMO_EMAIL))
        ).scalar_one_or_none()
        if existing is not None:
            await session.execute(
                delete(OAuthIdentity).where(OAuthIdentity.user_id == existing.id)
            )
            await session.delete(existing)
        await session.execute(delete(PendingToken))
        await session.commit()

    app = create_app()

    async def override_session():  # type: ignore[no-untyped-def]
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session

    google = fake_google(CLIENT_ID)
    google.email = DEMO_EMAIL
    google.subject = "demo-subject-000000000001"

    router = respx.mock(assert_all_called=False, assert_all_mocked=True)
    router.route(host="demo").pass_through()
    google.install(router)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://demo") as client:
        rule("1. GET /auth/oauth/providers")
        providers = await client.get(f"{OAUTH}/providers")
        print(f"HTTP {providers.status_code}  {providers.json()}")

        rule("2. GET /auth/oauth/google/start")
        started = await client.get(f"{OAUTH}/google/start")
        location = started.headers["location"]
        query = parse_qs(urlsplit(location).query)
        print(f"HTTP {started.status_code} -> {urlsplit(location).netloc}")
        print(f"  scope                 {query['scope'][0]}")
        print(f"  code_challenge_method {query['code_challenge_method'][0]}")
        print(f"  code_challenge        {query['code_challenge'][0][:24]}...")
        state = query["state"][0]

        rule("3. GET /auth/oauth/google/callback  (brand-new identity)")
        with router:
            callback = await client.get(
                f"{OAUTH}/google/callback",
                params={"code": "demo-auth-code", "state": state},
            )
        target = callback.headers["location"]
        header_names = {key.lower() for key in callback.headers}
        pending = parse_qs(urlsplit(target).query)["pending_token"][0]

        print(f"HTTP {callback.status_code} -> {target.split('?')[0]}")
        print(f"  Set-Cookie present    {'set-cookie' in header_names}   <- must be False")
        print(f"  access_token in body  {'access_token' in callback.text}   <- must be False")
        print(f"  access_token in URL   {'access_token' in target}   <- must be False")
        print(f"  pending_token         {pending[:32]}...")

        async with factory() as session:
            users = (await session.execute(select(User).where(User.email == DEMO_EMAIL))).all()
            print(f"  users created so far  {len(users)}   <- must be 0")

        rule("4. Pending token cannot authenticate anywhere")
        headers = {"Authorization": f"Bearer {pending}"}
        for label, response in [
            ("GET  /auth/me       ", await client.get(f"{AUTH}/me", headers=headers)),
            (
                "POST /auth/logout-all",
                await client.post(f"{AUTH}/logout-all", json={}, headers=headers),
            ),
            (
                "POST /auth/refresh  ",
                await client.post(f"{AUTH}/refresh", json={}, headers=headers),
            ),
        ]:
            print(f"  {label}  HTTP {response.status_code}   <- must be 401")

        rule("5. POST /auth/oauth/complete  role=ADMIN  (not self-assignable)")
        refused = await client.post(
            f"{OAUTH}/complete",
            json={"pending_token": pending, "role": "ADMIN", "display_name": "Demo"},
        )
        print(f"HTTP {refused.status_code}  {refused.json()['error']['code']}")

        rule("6. POST /auth/oauth/complete  role=WEAVER")
        completed = await client.post(
            f"{OAUTH}/complete",
            json={
                "pending_token": pending,
                "role": "WEAVER",
                "display_name": "Demo Weaver",
                "region": "Varanasi",
            },
        )
        body = completed.json()
        print(f"HTTP {completed.status_code}")
        print(f"  user.email            {body['user']['email']}")
        print(f"  user.role             {body['user']['role']}")
        print(f"  user.status           {body['user']['status']}   <- self-declared weaver")
        print(f"  access_token          {body['access_token'][:32]}...")
        print(f"  Set-Cookie present    {'set-cookie' in {k.lower() for k in completed.headers}}")

        rule("7. The issued session works")
        me = await client.get(
            f"{AUTH}/me", headers={"Authorization": f"Bearer {body['access_token']}"}
        )
        print(f"GET /auth/me  HTTP {me.status_code}  {me.json()['email']}")

        rule("8. Replaying the same pending token")
        replay = await client.post(
            f"{OAUTH}/complete",
            json={"pending_token": pending, "role": "WEAVER", "display_name": "Demo Weaver"},
        )
        print(f"HTTP {replay.status_code}  {replay.json()['error']['code']}")

    await engine.dispose()
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
