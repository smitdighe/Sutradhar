"""ADMIN must be unreachable from the API. Every route in, closed.

ADMIN can grant every other role, so the whole authorization model rests on
there being no path from an unauthenticated request to holding it. There are
exactly three places a role enters the system from outside -- registration,
OAuth completion, and profile update -- and this file closes all three.

The parametrised test at the bottom is the durable one: it enumerates the enum,
so a role added later fails here until somebody classifies it. The three route
tests would keep passing.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.oauth.google import reset_jwks_cache
from app.auth.roles import GRANTABLE_ONLY_ROLES, SELF_ASSIGNABLE_ROLES, Role
from app.config import get_settings
from app.db.models.enums import UserRole, UserStatus
from app.db.models.user import User
from tests.fakes.fake_google import FakeGoogle, fake_google

# No explicit asyncio mark: pytest.ini runs in auto mode, so async tests are
# collected as such automatically. Marking the module would also tag the pure
# classification checks below, which have no event loop to run in.
pytestmark = pytest.mark.integration

API = get_settings().api_prefix
AUTH = f"{API}/auth"
OAUTH = f"{API}/auth/oauth"
PASSWORD = "correct-horse-battery-staple"
CLIENT_ID = "test-client-id.apps.googleusercontent.com"

PRIVILEGED = ["ADMIN", "COOP_OFFICER", "INSPECTOR"]


def unique_email(tag: str = "esc") -> str:
    return f"{tag}-{uuid.uuid4().hex[:10]}@example.com"


# ---------------------------------------------------------------- the enum


class TestRoleClassification:
    """Pure checks -- no database, no event loop."""

    @pytest.mark.parametrize("role", list(Role))
    def test_every_role_is_on_exactly_one_side(self, role: Role) -> None:
        # Enumerated, not listed: a sixth role fails here until classified.
        assert (role in SELF_ASSIGNABLE_ROLES) != (role in GRANTABLE_ONLY_ROLES)

    @pytest.mark.parametrize("role", [Role.ADMIN, Role.COOP_OFFICER, Role.INSPECTOR])
    def test_privileged_roles_are_not_self_assignable(self, role: Role) -> None:
        assert role not in SELF_ASSIGNABLE_ROLES

    def test_admin_specifically(self) -> None:
        # Called out on its own because it is the one that grants the others.
        assert Role.ADMIN not in SELF_ASSIGNABLE_ROLES
        assert Role.ADMIN in GRANTABLE_ONLY_ROLES


# ---------------------------------------------------------------- registration


class TestRegistration:
    @pytest.mark.parametrize("role", PRIVILEGED)
    async def test_register_with_a_privileged_role_is_403(
        self, client: httpx.AsyncClient, role: str
    ) -> None:
        response = await client.post(
            f"{AUTH}/register",
            json={
                "email": unique_email(),
                "password": PASSWORD,
                "display_name": "Would-be Admin",
                "role": role,
            },
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "ROLE_NOT_SELF_ASSIGNABLE"

    async def test_refused_registration_creates_nothing(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        email = unique_email()
        await client.post(
            f"{AUTH}/register",
            json={
                "email": email,
                "password": PASSWORD,
                "display_name": "Would-be Admin",
                "role": "ADMIN",
            },
        )
        found = (
            await session.execute(select(User).where(User.email == email))
        ).scalar_one_or_none()
        assert found is None

    async def test_no_admin_exists_after_registration_attempts(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        for role in PRIVILEGED:
            await client.post(
                f"{AUTH}/register",
                json={
                    "email": unique_email(),
                    "password": PASSWORD,
                    "display_name": "Would-be Admin",
                    "role": role,
                },
            )
        admins = (
            (await session.execute(select(User).where(User.role == UserRole.ADMIN)))
            .scalars()
            .all()
        )
        assert admins == []


# ---------------------------------------------------------------- oauth completion


@pytest.fixture
def google_enabled(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    monkeypatch.setenv("GOOGLE_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-client-secret")
    get_settings.cache_clear()
    reset_jwks_cache()
    yield CLIENT_ID
    get_settings.cache_clear()
    reset_jwks_cache()


def mocked(fake: FakeGoogle) -> respx.MockRouter:
    router = respx.mock(assert_all_called=False, assert_all_mocked=True)
    router.route(host="testserver").pass_through()
    fake.install(router)
    return router


class TestOAuthCompletion:
    @pytest.fixture
    def provider(self, google_enabled: str) -> FakeGoogle:
        return fake_google(google_enabled)

    async def _pending_token(
        self, client: httpx.AsyncClient, provider: FakeGoogle
    ) -> str:
        start = await client.get(f"{OAUTH}/google/start")
        state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
        with mocked(provider):
            redirect = await client.get(
                f"{OAUTH}/google/callback",
                params={"code": "fake-auth-code", "state": state},
            )
        return parse_qs(urlsplit(redirect.headers["location"]).query)["pending_token"][0]

    @pytest.mark.parametrize("role", PRIVILEGED)
    async def test_complete_with_a_privileged_role_is_403(
        self, client: httpx.AsyncClient, provider: FakeGoogle, role: str
    ) -> None:
        token = await self._pending_token(client, provider)
        response = await client.post(
            f"{OAUTH}/complete",
            json={"pending_token": token, "role": role, "display_name": "Would-be Admin"},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "ROLE_NOT_SELF_ASSIGNABLE"

    async def test_refused_completion_creates_no_admin(
        self, client: httpx.AsyncClient, provider: FakeGoogle, session: AsyncSession
    ) -> None:
        token = await self._pending_token(client, provider)
        await client.post(
            f"{OAUTH}/complete",
            json={"pending_token": token, "role": "ADMIN", "display_name": "Would-be Admin"},
        )
        admins = (
            (await session.execute(select(User).where(User.role == UserRole.ADMIN)))
            .scalars()
            .all()
        )
        assert admins == []


# ---------------------------------------------------------------- profile update


class TestProfileUpdate:
    async def _session_for(self, client: httpx.AsyncClient, email: str) -> str:
        await client.post(
            f"{AUTH}/register",
            json={"email": email, "password": PASSWORD, "display_name": "Ordinary User"},
        )
        logged_in = await client.post(
            f"{AUTH}/login", json={"email": email, "password": PASSWORD}
        )
        token: str = logged_in.json()["access_token"]
        return token

    @pytest.mark.parametrize("role", PRIVILEGED)
    async def test_patch_me_cannot_change_role(
        self, client: httpx.AsyncClient, session: AsyncSession, role: str
    ) -> None:
        email = unique_email()
        access = await self._session_for(client, email)

        response = await client.patch(
            f"{AUTH}/me",
            json={"role": role, "display_name": "Renamed"},
            headers={"Authorization": f"Bearer {access}"},
        )
        # 200, not 4xx: the field is absent from the schema, so it is dropped
        # rather than rejected. What matters is the database.
        assert response.status_code == 200

        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        await session.refresh(user)
        assert user.role is UserRole.CONSUMER

    async def test_patch_me_cannot_change_status_or_email(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        email = unique_email()
        access = await self._session_for(client, email)

        payload: dict[str, Any] = {
            "role": "ADMIN",
            "status": "SUSPENDED",
            "email": "attacker@evil.example.com",
            "fraud_flagged_at": None,
            "identity_salt": "00" * 32,
            "password_hash": "$argon2id$forged",
            "display_name": "Renamed",
        }
        await client.patch(
            f"{AUTH}/me", json=payload, headers={"Authorization": f"Bearer {access}"}
        )

        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        await session.refresh(user)
        assert user.role is UserRole.CONSUMER
        assert user.status is UserStatus.ACTIVE
        assert user.email == email
        assert user.display_name == "Renamed"  # the one legitimate change landed


# ---------------------------------------------------------------- the only door


class TestScriptIsTheOnlyPath:
    async def test_no_api_route_mentions_admin_as_an_input(self) -> None:
        # A blunt but useful guard: if a future route ever accepts a role in a
        # request body, its schema shows up here and somebody has to look at it.
        from app.main import create_app

        schema = create_app().openapi()
        request_models = {
            name: model
            for name, model in schema.get("components", {}).get("schemas", {}).items()
            if name.endswith("Request")
        }
        role_bearing = {
            name
            for name, model in request_models.items()
            if "role" in model.get("properties", {})
        }
        # Only these two accept a role, and both re-validate it server-side
        # against SELF_ASSIGNABLE_ROLES.
        assert role_bearing == {"RegisterRequest", "CompleteRequest"}
