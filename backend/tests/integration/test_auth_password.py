"""End-to-end auth: registration, login, sessions, rotation, reuse detection."""

from __future__ import annotations

import asyncio
import statistics
import time
import uuid
from typing import Any

import httpx
import jwt
import pytest
from argon2 import PasswordHasher
from argon2.low_level import Type
from freezegun import freeze_time
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.password import verify_password
from app.config import get_settings
from app.core.clock import now
from app.db.models.enums import AuthEventType, UserRole, UserStatus
from app.db.models.user import AuthEvent, RefreshToken, User

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

PREFIX = f"{get_settings().api_prefix}/auth"
PASSWORD = "correct-horse-battery-staple"
COOKIE = get_settings().refresh_cookie_name


def unique_email(tag: str = "user") -> str:
    return f"{tag}-{uuid.uuid4().hex[:10]}@example.com"


async def register(
    client: httpx.AsyncClient, email: str, password: str = PASSWORD, **extra: Any
) -> httpx.Response:
    body = {"email": email, "password": password, "display_name": "Test User", **extra}
    return await client.post(f"{PREFIX}/register", json=body)


async def login(
    client: httpx.AsyncClient, email: str, password: str = PASSWORD
) -> httpx.Response:
    return await client.post(f"{PREFIX}/login", json={"email": email, "password": password})


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestRegistration:
    async def test_register_returns_201_and_the_profile(self, client: httpx.AsyncClient) -> None:
        email = unique_email()
        response = await register(client, email)
        assert response.status_code == 201
        body = response.json()
        assert body["email"] == email
        assert body["role"] == UserRole.CONSUMER
        assert body["status"] == UserStatus.ACTIVE

    async def test_register_never_returns_secrets(self, client: httpx.AsyncClient) -> None:
        body = (await register(client, unique_email())).json()
        for leaked in ("password", "password_hash", "identity_salt"):
            assert leaked not in body

    async def test_register_does_not_log_the_caller_in(self, client: httpx.AsyncClient) -> None:
        response = await register(client, unique_email())
        assert "access_token" not in response.json()
        assert COOKIE not in response.cookies

    async def test_duplicate_email_is_409(self, client: httpx.AsyncClient) -> None:
        email = unique_email()
        assert (await register(client, email)).status_code == 201
        response = await register(client, email)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "EMAIL_ALREADY_REGISTERED"

    async def test_duplicate_email_is_case_insensitive(self, client: httpx.AsyncClient) -> None:
        # citext: Weaver@x and weaver@x must be one account, not two.
        email = unique_email()
        await register(client, email)
        assert (await register(client, email.upper())).status_code == 409

    async def test_weaver_lands_in_pending_verification(
        self, client: httpx.AsyncClient
    ) -> None:
        # A self-declared weaver is not a trusted weaver.
        body = (await register(client, unique_email(), role="WEAVER")).json()
        assert body["role"] == UserRole.WEAVER
        assert body["status"] == UserStatus.PENDING_VERIFICATION

    @pytest.mark.parametrize("role", ["ADMIN", "INSPECTOR", "COOP_OFFICER"])
    async def test_privileged_roles_are_refused(
        self, client: httpx.AsyncClient, role: str
    ) -> None:
        response = await register(client, unique_email(), role=role)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "ROLE_NOT_SELF_ASSIGNABLE"

    async def test_privileged_role_creates_no_account(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        email = unique_email()
        await register(client, email, role="ADMIN")
        found = (
            await session.execute(select(User).where(User.email == email))
        ).scalar_one_or_none()
        assert found is None

    @pytest.mark.parametrize("password", ["short", "a" * 11, ""])
    async def test_passwords_below_the_minimum_are_rejected(
        self, client: httpx.AsyncClient, password: str
    ) -> None:
        assert (await register(client, unique_email(), password=password)).status_code == 422

    async def test_overlong_password_is_rejected(self, client: httpx.AsyncClient) -> None:
        # argon2 cost scales with input length, so this is a DoS lever.
        assert (await register(client, unique_email(), password="a" * 129)).status_code == 422

    async def test_password_equal_to_the_email_local_part_is_rejected(
        self, client: httpx.AsyncClient
    ) -> None:
        email = "averylongusername@example.com"
        response = await register(client, email, password="averylongusername")
        assert response.status_code == 422

    async def test_password_is_stored_as_argon2id(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        email = unique_email()
        await register(client, email)
        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        assert user.password_hash is not None
        assert user.password_hash.startswith("$argon2id$")
        assert PASSWORD not in user.password_hash

    async def test_register_writes_an_auth_event(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        email = unique_email()
        await register(client, email)
        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        events = (
            (await session.execute(select(AuthEvent).where(AuthEvent.user_id == user.id)))
            .scalars()
            .all()
        )
        assert [event.event_type for event in events] == [AuthEventType.REGISTER]


class TestLoginAndSession:
    async def test_full_happy_path(self, client: httpx.AsyncClient) -> None:
        # register -> login -> /me -> refresh -> /me with the new token
        email = unique_email()
        await register(client, email)

        logged_in = await login(client, email)
        assert logged_in.status_code == 200
        first = logged_in.json()
        assert first["token_type"] == "bearer"
        assert first["expires_in"] == get_settings().access_token_ttl_seconds
        assert first["user"]["email"] == email

        me = await client.get(f"{PREFIX}/me", headers=bearer(first["access_token"]))
        assert me.status_code == 200
        assert me.json()["email"] == email

        refreshed = await client.post(f"{PREFIX}/refresh", json={})
        assert refreshed.status_code == 200
        second = refreshed.json()
        assert second["access_token"] != first["access_token"]

        me_again = await client.get(f"{PREFIX}/me", headers=bearer(second["access_token"]))
        assert me_again.status_code == 200
        assert me_again.json()["email"] == email

    async def test_login_sets_an_httponly_refresh_cookie(
        self, client: httpx.AsyncClient
    ) -> None:
        email = unique_email()
        await register(client, email)
        response = await login(client, email)

        raw = response.headers["set-cookie"]
        assert COOKIE in raw
        assert "HttpOnly" in raw
        assert "SameSite=lax" in raw
        assert f"Path={PREFIX}" in raw

    async def test_raw_refresh_token_is_never_stored(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        email = unique_email()
        await register(client, email)
        await login(client, email)
        raw = client.cookies[COOKIE]

        stored = (await session.execute(select(RefreshToken.token_hash))).scalars().all()
        assert raw not in stored
        assert all(len(value) == 64 for value in stored)

    async def test_login_response_is_not_cacheable(self, client: httpx.AsyncClient) -> None:
        email = unique_email()
        await register(client, email)
        response = await login(client, email)
        assert response.headers["cache-control"] == "no-store"

    async def test_suspended_account_is_403(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        email = unique_email()
        await register(client, email)
        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        user.status = UserStatus.SUSPENDED
        await session.commit()

        response = await login(client, email)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "ACCOUNT_SUSPENDED"

    async def test_login_records_success_and_failure(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        email = unique_email()
        await register(client, email)
        await login(client, email, "wrong-password-here")
        await login(client, email)

        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        events = {
            event.event_type
            for event in (
                await session.execute(select(AuthEvent).where(AuthEvent.user_id == user.id))
            )
            .scalars()
            .all()
        }
        assert AuthEventType.LOGIN_FAILURE in events
        assert AuthEventType.LOGIN_SUCCESS in events

    async def test_last_login_at_is_recorded(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        email = unique_email()
        await register(client, email)
        await login(client, email)
        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        await session.refresh(user)
        assert user.last_login_at is not None


class TestCredentialIndistinguishability:
    async def test_wrong_password_and_unknown_email_are_byte_identical(
        self, client: httpx.AsyncClient
    ) -> None:
        email = unique_email()
        await register(client, email)

        wrong_password = await login(client, email, "definitely-not-the-password")
        unknown_email = await login(client, unique_email("ghost"), "definitely-not-the-password")

        assert wrong_password.status_code == unknown_email.status_code == 401
        # Identical bar the request id, which is per-request by design.
        first, second = wrong_password.json()["error"], unknown_email.json()["error"]
        first.pop("request_id")
        second.pop("request_id")
        assert first == second
        assert first["code"] == "INVALID_CREDENTIALS"

    async def test_timing_is_indistinguishable(self, client: httpx.AsyncClient) -> None:
        # If the unknown-email path skipped argon2 it would be an order of
        # magnitude faster, turning login into a user-enumeration oracle.
        # Compared at the median, which is robust to the odd scheduling spike.
        email = unique_email()
        await register(client, email)

        async def sample(target: str) -> float:
            started = time.perf_counter()
            await login(client, target, "definitely-not-the-password")
            return (time.perf_counter() - started) * 1000

        wrong_password = [await sample(email) for _ in range(50)]
        unknown_email = [await sample(unique_email("ghost")) for _ in range(50)]

        difference = abs(statistics.median(wrong_password) - statistics.median(unknown_email))
        assert difference < 30, (
            f"p50 gap {difference:.1f}ms: wrong-password "
            f"{statistics.median(wrong_password):.1f}ms vs unknown-email "
            f"{statistics.median(unknown_email):.1f}ms"
        )

    async def test_oauth_only_account_cannot_be_password_logged_in(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        # Phase 3 creates users with password_hash NULL. Those must not become
        # loggable-in by presenting any password at all.
        user = User(
            email=unique_email("oauth"),
            password_hash=None,
            display_name="OAuth Only",
            role=UserRole.CONSUMER,
            status=UserStatus.ACTIVE,
            identity_salt=b"\x00" * 32,
        )
        session.add(user)
        await session.commit()

        response = await login(client, user.email, PASSWORD)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"


class TestRefreshRotation:
    async def test_refresh_rotates_the_cookie(self, client: httpx.AsyncClient) -> None:
        email = unique_email()
        await register(client, email)
        await login(client, email)
        first = client.cookies[COOKIE]

        await client.post(f"{PREFIX}/refresh", json={})
        assert client.cookies[COOKIE] != first

    async def test_refresh_links_old_to_new(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        email = unique_email()
        await register(client, email)
        await login(client, email)
        await client.post(f"{PREFIX}/refresh", json={})

        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        tokens = (
            (
                await session.execute(
                    select(RefreshToken)
                    .where(RefreshToken.user_id == user.id)
                    .order_by(RefreshToken.issued_at)
                )
            )
            .scalars()
            .all()
        )
        assert len(tokens) == 2
        old, new = tokens
        assert old.revoked_at is not None
        assert old.replaced_by == new.id
        assert new.revoked_at is None
        assert new.family_id == old.family_id  # same family, not a new login

    async def test_body_fallback_works_without_a_cookie(
        self, client: httpx.AsyncClient
    ) -> None:
        email = unique_email()
        await register(client, email)
        await login(client, email)
        raw = client.cookies[COOKIE]
        client.cookies.clear()

        response = await client.post(f"{PREFIX}/refresh", json={"refresh_token": raw})
        assert response.status_code == 200

    async def test_unknown_refresh_token_is_401(self, client: httpx.AsyncClient) -> None:
        response = await client.post(
            f"{PREFIX}/refresh", json={"refresh_token": "not-a-real-token"}
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "INVALID_REFRESH_TOKEN"

    async def test_missing_refresh_token_is_401(self, client: httpx.AsyncClient) -> None:
        response = await client.post(f"{PREFIX}/refresh", json={})
        assert response.status_code == 401

    async def test_expired_refresh_token_is_401(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        email = unique_email()
        await register(client, email)
        await login(client, email)

        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        token = (
            await session.execute(select(RefreshToken).where(RefreshToken.user_id == user.id))
        ).scalar_one()
        token.expires_at = now()
        await session.commit()

        response = await client.post(f"{PREFIX}/refresh", json={})
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "REFRESH_TOKEN_EXPIRED"


class TestReuseDetection:
    async def test_replaying_a_used_token_kills_the_whole_family(
        self, client: httpx.AsyncClient
    ) -> None:
        # Use A, get B, replay A. Both must die: there is no way to tell which
        # holder is the thief, so trusting the newer one is a coin flip.
        email = unique_email()
        await register(client, email)
        await login(client, email)
        token_a = client.cookies[COOKIE]

        assert (await client.post(f"{PREFIX}/refresh", json={})).status_code == 200
        token_b = client.cookies[COOKIE]
        assert token_b != token_a

        # Clear the jar first: the cookie now holds B, and the cookie wins over
        # the body by design, so a replay has to come in via the body alone.
        client.cookies.clear()
        replayed = await client.post(f"{PREFIX}/refresh", json={"refresh_token": token_a})
        assert replayed.status_code == 401
        assert replayed.json()["error"]["code"] == "REFRESH_TOKEN_REUSED"

        # B was valid a moment ago and is now dead too.
        client.cookies.clear()
        after = await client.post(f"{PREFIX}/refresh", json={"refresh_token": token_b})
        assert after.status_code == 401
        assert after.json()["error"]["code"] == "REFRESH_TOKEN_REUSED"

    async def test_reuse_writes_an_audit_event(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        email = unique_email()
        await register(client, email)
        await login(client, email)
        token_a = client.cookies[COOKIE]
        await client.post(f"{PREFIX}/refresh", json={})
        client.cookies.clear()
        await client.post(f"{PREFIX}/refresh", json={"refresh_token": token_a})

        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        events = (
            (
                await session.execute(
                    select(AuthEvent).where(
                        AuthEvent.user_id == user.id,
                        AuthEvent.event_type == AuthEventType.REFRESH_REUSE_DETECTED,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert events[0].detail is not None
        assert "family_id" in events[0].detail
        # The token hash is the lookup key; it must never reach the audit log.
        assert "token_hash" not in events[0].detail

    async def test_a_second_login_is_an_independent_family(
        self, client: httpx.AsyncClient
    ) -> None:
        # Killing one family must not log the user out of their other devices.
        email = unique_email()
        await register(client, email)

        await login(client, email)
        device_one = client.cookies[COOKIE]
        client.cookies.clear()

        await login(client, email)
        device_two = client.cookies[COOKIE]
        client.cookies.clear()

        await client.post(f"{PREFIX}/refresh", json={"refresh_token": device_one})
        client.cookies.clear()
        burned = await client.post(f"{PREFIX}/refresh", json={"refresh_token": device_one})
        assert burned.status_code == 401
        client.cookies.clear()

        survivor = await client.post(f"{PREFIX}/refresh", json={"refresh_token": device_two})
        assert survivor.status_code == 200

    async def test_concurrent_refresh_yields_exactly_one_success(
        self, client: httpx.AsyncClient
    ) -> None:
        # SELECT ... FOR UPDATE serialises the two. The loser sees the winner's
        # revocation and treats it as reuse, so the family dies -- correct,
        # because two clients racing on one token is indistinguishable from
        # theft. What must never happen is two live tokens.
        email = unique_email()
        await register(client, email)
        await login(client, email)
        raw = client.cookies[COOKIE]
        client.cookies.clear()

        responses = await asyncio.gather(
            *(
                client.post(f"{PREFIX}/refresh", json={"refresh_token": raw})
                for _ in range(2)
            ),
            return_exceptions=True,
        )
        statuses = [
            item.status_code for item in responses if isinstance(item, httpx.Response)
        ]
        assert sorted(statuses) == [200, 401]


class TestLogout:
    async def test_logout_revokes_the_family_and_clears_the_cookie(
        self, client: httpx.AsyncClient
    ) -> None:
        email = unique_email()
        await register(client, email)
        await login(client, email)
        raw = client.cookies[COOKIE]

        response = await client.post(f"{PREFIX}/logout", json={})
        assert response.status_code == 204

        client.cookies.clear()
        after = await client.post(f"{PREFIX}/refresh", json={"refresh_token": raw})
        assert after.status_code == 401

    async def test_logout_without_a_token_is_still_204(
        self, client: httpx.AsyncClient
    ) -> None:
        assert (await client.post(f"{PREFIX}/logout", json={})).status_code == 204

    async def test_logout_all_kills_every_family(self, client: httpx.AsyncClient) -> None:
        email = unique_email()
        await register(client, email)

        await login(client, email)
        device_one = client.cookies[COOKIE]
        client.cookies.clear()

        second = await login(client, email)
        device_two = client.cookies[COOKIE]
        access = second.json()["access_token"]

        assert (
            await client.post(f"{PREFIX}/logout-all", json={}, headers=bearer(access))
        ).status_code == 204

        client.cookies.clear()
        for token in (device_one, device_two):
            response = await client.post(f"{PREFIX}/refresh", json={"refresh_token": token})
            assert response.status_code == 401
            client.cookies.clear()

    async def test_logout_all_requires_authentication(
        self, client: httpx.AsyncClient
    ) -> None:
        assert (await client.post(f"{PREFIX}/logout-all", json={})).status_code == 401


class TestGuards:
    async def test_me_without_a_token_is_401(self, client: httpx.AsyncClient) -> None:
        assert (await client.get(f"{PREFIX}/me")).status_code == 401

    @pytest.mark.parametrize(
        "header",
        ["Bearer garbage", "Basic abc123", "Bearer ", "NotEvenAScheme"],
    )
    async def test_malformed_authorization_is_401(
        self, client: httpx.AsyncClient, header: str
    ) -> None:
        response = await client.get(f"{PREFIX}/me", headers={"Authorization": header})
        assert response.status_code == 401

    async def test_pending_audience_token_is_rejected_with_401(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        # Phase 3 will mint pending tokens for half-finished OAuth. They must be
        # structurally incapable of authenticating here -- 401, not 403, because
        # a pending token does not identify a session at all. Proven before the
        # code that mints them exists.
        email = unique_email()
        await register(client, email)
        user = (await session.execute(select(User).where(User.email == email))).scalar_one()

        settings = get_settings()
        forged = jwt.encode(
            {
                "sub": str(user.id),
                "role": str(user.role),
                "iss": settings.jwt_issuer,
                "aud": settings.pending_token_audience,  # sutradhar/pending
                "iat": int(now().timestamp()),
                "exp": int(now().timestamp()) + 600,
                "jti": uuid.uuid4().hex,
                "ver": 1,
            },
            settings.jwt_private_key_path.read_bytes(),
            algorithm="EdDSA",
        )

        response = await client.get(f"{PREFIX}/me", headers=bearer(forged))
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "TOKEN_INVALID"

    async def test_wrong_issuer_is_rejected(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        settings = get_settings()
        forged = jwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "iss": "somebody-else",
                "aud": settings.jwt_audience,
                "iat": int(now().timestamp()),
                "exp": int(now().timestamp()) + 600,
                "jti": uuid.uuid4().hex,
            },
            settings.jwt_private_key_path.read_bytes(),
            algorithm="EdDSA",
        )
        assert (await client.get(f"{PREFIX}/me", headers=bearer(forged))).status_code == 401

    async def test_alg_none_token_is_rejected(self, client: httpx.AsyncClient) -> None:
        # The classic JWT bypass: strip the signature and claim it was intended.
        settings = get_settings()
        forged = jwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "iss": settings.jwt_issuer,
                "aud": settings.jwt_audience,
                "iat": int(now().timestamp()),
                "exp": int(now().timestamp()) + 600,
                "jti": uuid.uuid4().hex,
            },
            key="",
            algorithm="none",
        )
        assert (await client.get(f"{PREFIX}/me", headers=bearer(forged))).status_code == 401

    async def test_expired_access_token_is_401(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        settings = get_settings()
        forged = jwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "iss": settings.jwt_issuer,
                "aud": settings.jwt_audience,
                "iat": int(now().timestamp()) - 7200,
                "exp": int(now().timestamp()) - 3600,
                "jti": uuid.uuid4().hex,
            },
            settings.jwt_private_key_path.read_bytes(),
            algorithm="EdDSA",
        )
        response = await client.get(f"{PREFIX}/me", headers=bearer(forged))
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "TOKEN_EXPIRED"

    async def test_suspension_invalidates_a_live_access_token(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        email = unique_email()
        await register(client, email)
        access = (await login(client, email)).json()["access_token"]
        assert (await client.get(f"{PREFIX}/me", headers=bearer(access))).status_code == 200

        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        user.status = UserStatus.SUSPENDED
        await session.commit()

        response = await client.get(f"{PREFIX}/me", headers=bearer(access))
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "ACCOUNT_SUSPENDED"


class TestProfileUpdate:
    async def test_display_name_and_region_are_updatable(
        self, client: httpx.AsyncClient
    ) -> None:
        email = unique_email()
        await register(client, email)
        access = (await login(client, email)).json()["access_token"]

        response = await client.patch(
            f"{PREFIX}/me",
            json={"display_name": "Renamed", "region": "Varanasi"},
            headers=bearer(access),
        )
        assert response.status_code == 200
        assert response.json()["display_name"] == "Renamed"
        assert response.json()["region"] == "Varanasi"

    async def test_forbidden_fields_are_ignored_not_applied(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        # The escalation test. These fields are absent from the schema, so
        # there is no branch that could assign them however the body is shaped.
        email = unique_email()
        await register(client, email)
        access = (await login(client, email)).json()["access_token"]

        response = await client.patch(
            f"{PREFIX}/me",
            json={
                "role": "ADMIN",
                "status": "ACTIVE",
                "email": "attacker@evil.test",
                "password_hash": "$argon2id$whatever",
                "identity_salt": "00" * 32,
                "id": str(uuid.uuid4()),
                "display_name": "Legit Rename",
            },
            headers=bearer(access),
        )
        assert response.status_code == 200

        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        await session.refresh(user)
        assert user.role is UserRole.CONSUMER
        assert user.email == email
        assert user.display_name == "Legit Rename"
        assert user.password_hash is not None
        assert user.password_hash.startswith("$argon2id$")
        assert verify_password(PASSWORD, user.password_hash)

    async def test_weaver_cannot_activate_itself_via_patch(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        email = unique_email()
        await register(client, email, role="WEAVER")
        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        user.status = UserStatus.ACTIVE  # so it can log in at all
        await session.commit()
        access = (await login(client, email)).json()["access_token"]

        await client.patch(
            f"{PREFIX}/me", json={"status": "ACTIVE", "role": "ADMIN"}, headers=bearer(access)
        )
        await session.refresh(user)
        assert user.role is UserRole.WEAVER

    async def test_patch_requires_authentication(self, client: httpx.AsyncClient) -> None:
        assert (
            await client.patch(f"{PREFIX}/me", json={"display_name": "x"})
        ).status_code == 401


class TestRateLimiting:
    async def test_login_limit_returns_429_with_retry_after(
        self, client: httpx.AsyncClient
    ) -> None:
        email = unique_email()
        await register(client, email)
        limit = get_settings().rate_limit_login_per_minute

        # Pinned: rate-limit windows are epoch-aligned, so a run that straddles
        # a minute boundary would see the counter legitimately reset and the
        # last attempt succeed. That is correct behaviour and a useless test.
        with freeze_time("2026-08-26 12:00:10"):
            for _ in range(limit):
                await login(client, email, "wrong-password-here")

            response = await login(client, email, "wrong-password-here")

        assert response.status_code == 429
        assert response.json()["error"]["code"] == "RATE_LIMITED"
        assert int(response.headers["retry-after"]) > 0

    async def test_a_correct_password_does_not_reset_the_counter(
        self, client: httpx.AsyncClient
    ) -> None:
        # A limiter that only counts failures is a free oracle: an attacker
        # learns a password is right because the counter stopped moving.
        email = unique_email()
        await register(client, email)
        limit = get_settings().rate_limit_login_per_minute

        # Pinned for the same reason as above: without it, six successful
        # logins spanning a window boundary would leave the counter at one.
        with freeze_time("2026-08-26 12:10:10"):
            for _ in range(limit):
                assert (await login(client, email)).status_code == 200

            assert (await login(client, email)).status_code == 429

    async def test_register_limit_is_enforced_per_ip(
        self, client: httpx.AsyncClient
    ) -> None:
        limit = get_settings().rate_limit_register_per_hour
        # An hour-long window is far less likely to roll mid-test, but pinning
        # it costs nothing and removes the last source of clock flakiness here.
        with freeze_time("2026-08-26 12:20:10"):
            for _ in range(limit):
                await register(client, unique_email())
            response = await register(client, unique_email())
        assert response.status_code == 429


class TestPasswordRehash:
    async def test_login_transparently_upgrades_a_weak_hash(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        # Login is the only moment the plaintext is in hand and verified, so it
        # is the only moment a stronger hash can be written without asking the
        # user for anything.
        email = unique_email()
        await register(client, email)
        user = (await session.execute(select(User).where(User.email == email))).scalar_one()

        settings = get_settings()
        weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1, type=Type.ID)
        user.password_hash = weak.hash((PASSWORD + settings.password_pepper).encode())
        stale = user.password_hash
        await session.commit()

        assert (await login(client, email)).status_code == 200

        await session.refresh(user)
        assert user.password_hash != stale
        assert f"m={settings.argon2_memory_cost_kib}" in user.password_hash
        assert verify_password(PASSWORD, user.password_hash)

    async def test_a_current_hash_is_left_alone(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        email = unique_email()
        await register(client, email)
        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        original = user.password_hash

        await login(client, email)
        await session.refresh(user)
        assert user.password_hash == original


class TestSecretsNeverLeak:
    async def test_no_response_body_contains_a_password_or_token_hash(
        self, client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        email = unique_email()
        bodies = [
            (await register(client, email)).text,
            (await login(client, email)).text,
            (await client.post(f"{PREFIX}/refresh", json={})).text,
        ]
        async with session_factory() as db_session:
            hashes = (await db_session.execute(select(RefreshToken.token_hash))).scalars().all()
            password_hashes = (
                (await db_session.execute(select(User.password_hash))).scalars().all()
            )

        for body in bodies:
            assert PASSWORD not in body
            assert "identity_salt" not in body
            assert "password_hash" not in body
            for secret in [*hashes, *(value for value in password_hashes if value)]:
                assert secret not in body
