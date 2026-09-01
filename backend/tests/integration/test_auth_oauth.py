"""Google OAuth: linking, the pending-token window, state, and availability.

The fake in ``tests/fakes/fake_google.py`` uses real RSA keys and real
signatures, so these tests drive the actual verification code in
:mod:`app.auth.oauth.google`. Only the network is faked.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
import pytest_asyncio
import respx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.oauth.google import reset_jwks_cache
from app.auth.roles import SELF_ASSIGNABLE_ROLES, Role
from app.config import get_settings
from app.core.clock import now
from app.db.models.enums import AuthEventType, OAuthProvider, UserRole, UserStatus
from app.db.models.user import AuthEvent, OAuthIdentity, PendingToken, User
from tests.fakes.fake_google import DEFAULT_EMAIL, DEFAULT_SUBJECT, FakeGoogle, fake_google

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

API = get_settings().api_prefix
OAUTH = f"{API}/auth/oauth"
AUTH = f"{API}/auth"
COOKIE = get_settings().refresh_cookie_name
CLIENT_ID = "test-client-id.apps.googleusercontent.com"


@pytest.fixture
def google_enabled(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Turn Google on for the duration of a test."""
    monkeypatch.setenv("GOOGLE_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-client-secret")
    get_settings.cache_clear()
    reset_jwks_cache()
    yield CLIENT_ID
    get_settings.cache_clear()
    reset_jwks_cache()


@pytest.fixture
def google_disabled(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Credentials absent -- the unconfigured path."""
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def provider(google_enabled: str) -> AsyncIterator[FakeGoogle]:
    """A fake Google bound to the configured client id."""
    yield fake_google(google_enabled)


async def begin_flow(client: httpx.AsyncClient, **params: Any) -> str:
    """Hit /start and return the ``state`` Google would have been given."""
    response = await client.get(f"{OAUTH}/google/start", params=params)
    assert response.status_code == 302, response.text
    query = parse_qs(urlsplit(response.headers["location"]).query)
    return query["state"][0]


async def callback(
    client: httpx.AsyncClient, state: str, code: str = "fake-auth-code"
) -> httpx.Response:
    return await client.get(f"{OAUTH}/google/callback", params={"code": code, "state": state})


def mocked(fake: FakeGoogle, **token_overrides: Any) -> respx.MockRouter:
    """respx router with the fake installed and the ASGI host passed through."""
    router = respx.mock(assert_all_called=False, assert_all_mocked=True)
    router.route(host="testserver").pass_through()
    fake.install(router, **token_overrides)
    return router


async def complete(
    client: httpx.AsyncClient, token: str, role: str = "CONSUMER", **extra: Any
) -> httpx.Response:
    body = {
        "pending_token": token,
        "role": role,
        "display_name": "Completed User",
        **extra,
    }
    return await client.post(f"{OAUTH}/complete", json=body)


def pending_token_from(response: httpx.Response) -> str:
    query = parse_qs(urlsplit(response.headers["location"]).query)
    return query["pending_token"][0]


async def count(session: AsyncSession, model: Any) -> int:
    return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


# ---------------------------------------------------------------- availability


class TestAvailability:
    async def test_providers_lists_google_enabled(
        self, client: httpx.AsyncClient, google_enabled: str
    ) -> None:
        response = await client.get(f"{OAUTH}/providers")
        assert response.status_code == 200
        assert response.json() == {"data": [{"provider": "google", "enabled": True}]}

    async def test_providers_reports_disabled_without_credentials(
        self, client: httpx.AsyncClient, google_disabled: None
    ) -> None:
        # (o) The frontend asks this to decide whether to render a button, so
        # it must answer 200 even when nothing is configured.
        response = await client.get(f"{OAUTH}/providers")
        assert response.status_code == 200
        assert response.json()["data"][0]["enabled"] is False

    async def test_start_is_503_when_unconfigured(
        self, client: httpx.AsyncClient, google_disabled: None
    ) -> None:
        response = await client.get(f"{OAUTH}/google/start")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "OAUTH_PROVIDER_UNAVAILABLE"

    async def test_callback_is_503_when_unconfigured(
        self, client: httpx.AsyncClient, google_disabled: None
    ) -> None:
        response = await client.get(
            f"{OAUTH}/google/callback", params={"code": "x", "state": "y"}
        )
        assert response.status_code == 503

    async def test_app_boots_without_credentials(self, google_disabled: None) -> None:
        # A missing optional environment variable must never stop the service
        # starting. No ImportError, no 500 at import, no crash.
        from app.main import create_app

        application = create_app()
        assert application is not None

    async def test_readyz_reports_unconfigured_not_down(
        self, client: httpx.AsyncClient, google_disabled: None
    ) -> None:
        # Unconfigured is not a failure, so it must not drag the overall
        # verdict down the way a real outage would.
        response = await client.get("/readyz")
        assert response.status_code == 200
        assert response.json()["checks"]["google_oauth"]["status"] == "unconfigured"

    async def test_readyz_reports_ok_when_configured(
        self, client: httpx.AsyncClient, google_enabled: str
    ) -> None:
        response = await client.get("/readyz")
        assert response.json()["checks"]["google_oauth"]["status"] == "ok"


# ---------------------------------------------------------------- start


class TestStart:
    async def test_start_redirects_to_google_with_pkce(
        self, client: httpx.AsyncClient, google_enabled: str
    ) -> None:
        response = await client.get(f"{OAUTH}/google/start")
        assert response.status_code == 302

        parts = urlsplit(response.headers["location"])
        assert parts.netloc == "accounts.google.com"
        query = parse_qs(parts.query)
        assert query["client_id"] == [CLIENT_ID]
        assert query["response_type"] == ["code"]
        assert query["scope"] == ["openid email profile"]
        assert query["code_challenge_method"] == ["S256"]
        assert query["code_challenge"][0]
        assert query["state"][0]

    async def test_pkce_verifier_never_appears_in_the_redirect(
        self, client: httpx.AsyncClient, google_enabled: str
    ) -> None:
        # The verifier stays server-side inside the signed state; only its hash
        # goes to Google. Leaking it would defeat the point of PKCE.
        response = await client.get(f"{OAUTH}/google/start")
        query = parse_qs(urlsplit(response.headers["location"]).query)
        challenge = query["code_challenge"][0]
        assert challenge not in query["state"][0]

    async def test_each_start_produces_a_distinct_state(
        self, client: httpx.AsyncClient, google_enabled: str
    ) -> None:
        first = await begin_flow(client)
        second = await begin_flow(client)
        assert first != second

    async def test_start_is_rate_limited(
        self, client: httpx.AsyncClient, google_enabled: str
    ) -> None:
        limit = get_settings().rate_limit_oauth_start_per_minute
        for _ in range(limit):
            assert (await client.get(f"{OAUTH}/google/start")).status_code == 302
        assert (await client.get(f"{OAUTH}/google/start")).status_code == 429


# ---------------------------------------------------------------- state



def _decode_state(state: str) -> bytes:
    """Decode a base64url state to the bytes the signature actually covers."""
    import base64

    return base64.urlsafe_b64decode(state + "=" * (-len(state) % 4))

class TestState:
    async def test_replayed_state_is_rejected(
        self, client: httpx.AsyncClient, provider: FakeGoogle
    ) -> None:
        # (l) A signature proves this server minted the state. It says nothing
        # about whether it has already been spent.
        state = await begin_flow(client)
        with mocked(provider):
            assert (await callback(client, state)).status_code == 302
            replay = await callback(client, state)
        assert replay.status_code == 400
        assert replay.json()["error"]["code"] == "OAUTH_STATE_INVALID"

    async def test_tampered_state_is_rejected(
        self, client: httpx.AsyncClient, provider: FakeGoogle
    ) -> None:
        """Flipping a *middle* character, deliberately.

        Flipping the last character of a base64url string is not reliably a
        change at all: the final character carries only the leftover bits, so
        for a 4-byte-aligned payload fifteen of the sixty-four alphabet
        characters decode to the identical bytes. The signature is over the
        decoded bytes, so those are genuinely the same state and accepting them
        is correct -- but a test that flipped the tail passed or failed
        depending on the random state it happened to draw.

        A middle character contributes six full bits, so changing it always
        changes the payload. The assertion below proves that rather than
        assuming it.
        """
        state = await begin_flow(client)

        middle = len(state) // 2
        replacement = "A" if state[middle] != "A" else "B"
        tampered = state[:middle] + replacement + state[middle + 1 :]
        assert _decode_state(tampered) != _decode_state(state)

        with mocked(provider):
            response = await callback(client, tampered)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "OAUTH_STATE_INVALID"

    async def test_forged_state_is_rejected(
        self, client: httpx.AsyncClient, provider: FakeGoogle
    ) -> None:
        with mocked(provider):
            response = await callback(client, "not-a-real-state")
        assert response.status_code == 400

    async def test_missing_state_redirects_to_the_error_page(
        self, client: httpx.AsyncClient, google_enabled: str
    ) -> None:
        response = await client.get(f"{OAUTH}/google/callback", params={"code": "x"})
        assert response.status_code == 302
        assert get_settings().frontend_auth_error_url in response.headers["location"]

    @pytest.mark.parametrize(
        "target",
        [
            "https://evil.test/steal",
            "//evil.test/steal",
            "http://localhost:3000.evil.test",
            "/relative/path",
        ],
    )
    async def test_off_origin_return_to_is_rejected(
        self, client: httpx.AsyncClient, google_enabled: str, target: str
    ) -> None:
        # (n) An open redirect here means a phishing page reached through a
        # genuine consent screen on this server's real domain.
        response = await client.get(f"{OAUTH}/google/start", params={"return_to": target})
        assert response.status_code == 422
        assert response.headers.get("location") is None

    async def test_allowed_return_to_is_honoured(
        self, client: httpx.AsyncClient, provider: FakeGoogle, session: AsyncSession
    ) -> None:
        allowed = f"{get_settings().cors_origins[0]}/dashboard"
        session.add(
            User(
                email=DEFAULT_EMAIL,
                display_name="Existing",
                role=UserRole.CONSUMER,
                status=UserStatus.ACTIVE,
                identity_salt=b"\x01" * 32,
            )
        )
        await session.commit()

        state = await begin_flow(client, return_to=allowed)
        with mocked(provider):
            response = await callback(client, state)
        assert response.status_code == 302
        assert response.headers["location"] == allowed


# ---------------------------------------------------------------- provider errors


class TestProviderErrors:
    async def test_user_denied_consent_redirects_safely(
        self, client: httpx.AsyncClient, google_enabled: str
    ) -> None:
        response = await client.get(
            f"{OAUTH}/google/callback", params={"error": "access_denied"}
        )
        assert response.status_code == 302
        location = response.headers["location"]
        assert get_settings().frontend_auth_error_url in location
        assert "provider_denied" in location
        # The provider's own text is never echoed back.
        assert "access_denied" not in location

    async def test_token_exchange_failure_is_400(
        self, client: httpx.AsyncClient, provider: FakeGoogle
    ) -> None:
        state = await begin_flow(client)
        provider.token_status = 400
        with mocked(provider):
            response = await callback(client, state)
        assert response.status_code == 400

    async def test_id_token_with_wrong_audience_is_rejected(
        self, client: httpx.AsyncClient, provider: FakeGoogle
    ) -> None:
        # A token minted for a different Google client must not be spendable
        # here, or any app could mint identities for this one.
        state = await begin_flow(client)
        with mocked(provider, aud="some-other-client.apps.googleusercontent.com"):
            response = await callback(client, state)
        assert response.status_code == 400

    async def test_id_token_with_wrong_issuer_is_rejected(
        self, client: httpx.AsyncClient, provider: FakeGoogle
    ) -> None:
        state = await begin_flow(client)
        with mocked(provider, iss="https://accounts.evil.test"):
            response = await callback(client, state)
        assert response.status_code == 400

    async def test_expired_id_token_is_rejected(
        self, client: httpx.AsyncClient, provider: FakeGoogle
    ) -> None:
        state = await begin_flow(client)
        stale = int(now().timestamp()) - 3600
        with mocked(provider, exp=stale, iat=stale - 60):
            response = await callback(client, state)
        assert response.status_code == 400

    @pytest.mark.parametrize("issuer", ["accounts.google.com", "https://accounts.google.com"])
    async def test_both_google_issuer_spellings_are_accepted(
        self, client: httpx.AsyncClient, provider: FakeGoogle, issuer: str
    ) -> None:
        # Google mints both and has for years. Rejecting either breaks real logins.
        provider.issuer = issuer
        state = await begin_flow(client)
        with mocked(provider):
            response = await callback(client, state)
        assert response.status_code == 302
        assert get_settings().frontend_completion_url in response.headers["location"]


# ---------------------------------------------------------------- linking matrix


class TestLinking:
    async def test_b_unverified_email_creates_nothing(
        self, client: httpx.AsyncClient, provider: FakeGoogle, session: AsyncSession
    ) -> None:
        # (b) An unverified provider email is an account-takeover primitive:
        # register the victim's address at the provider and be handed their
        # local account.
        users_before = await count(session, User)
        identities_before = await count(session, OAuthIdentity)

        provider.email_verified = False
        state = await begin_flow(client)
        with mocked(provider):
            response = await callback(client, state)

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "PROVIDER_EMAIL_UNVERIFIED"
        assert await count(session, User) == users_before
        assert await count(session, OAuthIdentity) == identities_before
        assert await count(session, PendingToken) == 0

    async def test_a_existing_password_user_is_linked_not_duplicated(
        self, client: httpx.AsyncClient, provider: FakeGoogle, session: AsyncSession
    ) -> None:
        # (a) The duplicate-account bug this branch exists to prevent: one
        # person, two accounts, one of which owns their items.
        existing = User(
            email=DEFAULT_EMAIL,
            password_hash="$argon2id$v=19$m=65536,t=3,p=2$fake",
            display_name="Password User",
            role=UserRole.WEAVER,
            status=UserStatus.ACTIVE,
            identity_salt=b"\x02" * 32,
        )
        session.add(existing)
        await session.commit()
        users_before = await count(session, User)

        state = await begin_flow(client)
        with mocked(provider):
            response = await callback(client, state)

        assert response.status_code == 302
        assert get_settings().frontend_post_login_url in response.headers["location"]
        assert await count(session, User) == users_before  # linked, not created

        identity = (
            await session.execute(
                select(OAuthIdentity).where(OAuthIdentity.provider_subject == DEFAULT_SUBJECT)
            )
        ).scalar_one()
        assert identity.user_id == existing.id
        assert identity.email_verified is True

        events = (
            (
                await session.execute(
                    select(AuthEvent).where(
                        AuthEvent.user_id == existing.id,
                        AuthEvent.event_type == AuthEventType.OAUTH_LINK,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1

    async def test_a_linked_user_keeps_their_role_and_gets_a_session(
        self, client: httpx.AsyncClient, provider: FakeGoogle, session: AsyncSession
    ) -> None:
        session.add(
            User(
                email=DEFAULT_EMAIL,
                display_name="Existing",
                role=UserRole.INSPECTOR,
                status=UserStatus.ACTIVE,
                identity_salt=b"\x03" * 32,
            )
        )
        await session.commit()

        state = await begin_flow(client)
        with mocked(provider):
            response = await callback(client, state)

        assert response.status_code == 302
        assert COOKIE in response.headers.get("set-cookie", "")
        user = (
            await session.execute(select(User).where(User.email == DEFAULT_EMAIL))
        ).scalar_one()
        assert user.role is UserRole.INSPECTOR  # OAuth never changes a role

    async def test_c_second_login_resolves_by_subject_without_duplicating(
        self, client: httpx.AsyncClient, provider: FakeGoogle, session: AsyncSession
    ) -> None:
        # (c)
        session.add(
            User(
                email=DEFAULT_EMAIL,
                display_name="Existing",
                role=UserRole.CONSUMER,
                status=UserStatus.ACTIVE,
                identity_salt=b"\x04" * 32,
            )
        )
        await session.commit()

        for _ in range(2):
            state = await begin_flow(client)
            with mocked(provider):
                assert (await callback(client, state)).status_code == 302

        assert await count(session, User) == 1
        assert await count(session, OAuthIdentity) == 1

    async def test_d_changed_provider_email_updates_identity_not_the_account(
        self, client: httpx.AsyncClient, provider: FakeGoogle, session: AsyncSession
    ) -> None:
        # (d) The local email is the account's own identity -- used for password
        # login and for anything sent to the user. A provider does not get to
        # rewrite it just because the address on their side moved.
        session.add(
            User(
                email=DEFAULT_EMAIL,
                display_name="Existing",
                role=UserRole.CONSUMER,
                status=UserStatus.ACTIVE,
                identity_salt=b"\x05" * 32,
            )
        )
        await session.commit()

        state = await begin_flow(client)
        with mocked(provider):
            assert (await callback(client, state)).status_code == 302

        # Same subject, new address on Google's side.
        provider.email = "renamed@gmail.example.com"
        state = await begin_flow(client)
        with mocked(provider):
            assert (await callback(client, state)).status_code == 302

        assert await count(session, User) == 1
        user = (await session.execute(select(User))).scalar_one()
        await session.refresh(user)
        assert user.email == DEFAULT_EMAIL  # unchanged

        identity = (await session.execute(select(OAuthIdentity))).scalar_one()
        await session.refresh(identity)
        assert identity.provider_email == "renamed@gmail.example.com"  # updated

    async def test_subject_wins_over_email(
        self, client: httpx.AsyncClient, provider: FakeGoogle, session: AsyncSession
    ) -> None:
        # Google recycles addresses for deleted Workspace accounts. If matching
        # were done on email, the next holder of the address would be handed the
        # original owner's account -- items, role and all.
        #
        # Subject-first resolution means the new subject does not resolve to the
        # existing identity. It then reaches the email branch, finds the account
        # already has a Google link, and is refused rather than grafted on.
        owner = User(
            email=DEFAULT_EMAIL,
            display_name="Original Owner",
            role=UserRole.WEAVER,
            status=UserStatus.ACTIVE,
            identity_salt=b"" * 32,
        )
        session.add(owner)
        await session.commit()

        state = await begin_flow(client)
        with mocked(provider):
            assert (await callback(client, state)).status_code == 302

        # A different person, same recycled address.
        provider.subject = "999999999999999999999"
        state = await begin_flow(client)
        with mocked(provider):
            response = await callback(client, state)

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "OAUTH_IDENTITY_LINKED"

        # The original link is untouched and no second one was created.
        identities = (await session.execute(select(OAuthIdentity))).scalars().all()
        assert len(identities) == 1
        assert identities[0].provider_subject == DEFAULT_SUBJECT
        assert await count(session, User) == 1

    async def test_suspended_linked_account_cannot_sign_in(
        self, client: httpx.AsyncClient, provider: FakeGoogle, session: AsyncSession
    ) -> None:
        session.add(
            User(
                email=DEFAULT_EMAIL,
                display_name="Suspended",
                role=UserRole.CONSUMER,
                status=UserStatus.SUSPENDED,
                identity_salt=b"\x07" * 32,
            )
        )
        await session.commit()

        state = await begin_flow(client)
        with mocked(provider):
            response = await callback(client, state)

        assert response.status_code == 302
        assert "account_suspended" in response.headers["location"]
        assert "set-cookie" not in response.headers


# ---------------------------------------------------------------- new identity


class TestNewIdentity:
    async def test_e_new_identity_gets_no_session_at_all(
        self, client: httpx.AsyncClient, provider: FakeGoogle, session: AsyncSession
    ) -> None:
        # (e) The security-critical assertion of this phase, checked against the
        # real response headers: a browser that has not chosen a role must not
        # be authenticated in any form.
        state = await begin_flow(client)
        with mocked(provider):
            response = await callback(client, state)

        assert response.status_code == 302
        location = response.headers["location"]
        assert get_settings().frontend_completion_url in location
        assert "pending_token=" in location

        assert "set-cookie" not in {key.lower() for key in response.headers}
        assert COOKIE not in response.cookies
        assert "access_token" not in location
        assert "access_token" not in response.text
        assert await count(session, User) == 0
        assert await count(session, OAuthIdentity) == 0

    async def test_pending_row_is_recorded(
        self, client: httpx.AsyncClient, provider: FakeGoogle, session: AsyncSession
    ) -> None:
        state = await begin_flow(client)
        with mocked(provider):
            await callback(client, state)

        record = (await session.execute(select(PendingToken))).scalar_one()
        assert record.provider is OAuthProvider.GOOGLE
        assert record.provider_subject == DEFAULT_SUBJECT
        assert record.consumed_at is None

    async def test_complete_creates_the_account_and_a_session(
        self, client: httpx.AsyncClient, provider: FakeGoogle, session: AsyncSession
    ) -> None:
        state = await begin_flow(client)
        with mocked(provider):
            redirect = await callback(client, state)
        token = pending_token_from(redirect)

        response = await complete(client, token, role="CONSUMER", region="Varanasi")
        assert response.status_code == 200
        body = response.json()
        assert body["user"]["email"] == DEFAULT_EMAIL
        assert body["user"]["role"] == UserRole.CONSUMER
        assert body["access_token"]
        assert COOKIE in response.headers.get("set-cookie", "")

        user = (await session.execute(select(User))).scalar_one()
        # OAuth-only account: no password exists, so none can be verified.
        assert user.password_hash is None
        assert user.region == "Varanasi"

        identity = (await session.execute(select(OAuthIdentity))).scalar_one()
        assert identity.user_id == user.id
        assert identity.provider_subject == DEFAULT_SUBJECT

        events = (
            (
                await session.execute(
                    select(AuthEvent).where(
                        AuthEvent.event_type == AuthEventType.OAUTH_NEW_ACCOUNT
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1

    async def test_completed_session_works_on_me(
        self, client: httpx.AsyncClient, provider: FakeGoogle
    ) -> None:
        state = await begin_flow(client)
        with mocked(provider):
            redirect = await callback(client, state)
        token = pending_token_from(redirect)
        access = (await complete(client, token)).json()["access_token"]

        me = await client.get(f"{AUTH}/me", headers={"Authorization": f"Bearer {access}"})
        assert me.status_code == 200
        assert me.json()["email"] == DEFAULT_EMAIL

    async def test_weaver_completion_lands_in_pending_verification(
        self, client: httpx.AsyncClient, provider: FakeGoogle, session: AsyncSession
    ) -> None:
        # Same rule as Phase 2 registration.
        state = await begin_flow(client)
        with mocked(provider):
            redirect = await callback(client, state)
        response = await complete(client, pending_token_from(redirect), role="WEAVER")

        assert response.status_code == 200
        assert response.json()["user"]["status"] == UserStatus.PENDING_VERIFICATION

    async def test_f_concurrent_completions_create_exactly_one_user(
        self, client: httpx.AsyncClient, provider: FakeGoogle, session: AsyncSession
    ) -> None:
        # (f) The conditional UPDATE is what makes this safe: two requests
        # cannot both observe an unconsumed row.
        state = await begin_flow(client)
        with mocked(provider):
            redirect = await callback(client, state)
        token = pending_token_from(redirect)

        responses = await asyncio.gather(
            *(complete(client, token) for _ in range(2)), return_exceptions=True
        )
        statuses = sorted(
            item.status_code for item in responses if isinstance(item, httpx.Response)
        )
        assert statuses == [200, 401]
        assert await count(session, User) == 1
        assert await count(session, OAuthIdentity) == 1


# ---------------------------------------------------------------- pending token


class TestPendingToken:
    @pytest_asyncio.fixture
    async def token(self, client: httpx.AsyncClient, provider: FakeGoogle) -> str:
        state = await begin_flow(client)
        with mocked(provider):
            redirect = await callback(client, state)
        return pending_token_from(redirect)

    async def test_g_second_use_is_rejected(
        self, client: httpx.AsyncClient, token: str
    ) -> None:
        # (g)
        assert (await complete(client, token)).status_code == 200
        second = await complete(client, token)
        assert second.status_code == 401
        assert second.json()["error"]["code"] == "PENDING_TOKEN_CONSUMED"

    async def test_h_expired_token_is_rejected(
        self, client: httpx.AsyncClient, google_enabled: str
    ) -> None:
        # (h)
        settings = get_settings()
        stale = int(now().timestamp()) - 3600
        expired = jwt.encode(
            {
                "jti": str(uuid.uuid4()),
                "provider": "GOOGLE",
                "provider_subject": DEFAULT_SUBJECT,
                "provider_email": DEFAULT_EMAIL,
                "iss": settings.jwt_issuer,
                "aud": settings.pending_token_audience,
                "iat": stale - 600,
                "exp": stale,
            },
            settings.pending_token_secret,
            algorithm="HS256",
        )
        response = await complete(client, expired)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "TOKEN_EXPIRED"

    async def test_i_token_signed_with_the_session_key_is_rejected(
        self, client: httpx.AsyncClient, google_enabled: str
    ) -> None:
        # (i) The two token families use different keys precisely so that one
        # can never be spent as the other, even by accident.
        settings = get_settings()
        forged = jwt.encode(
            {
                "jti": str(uuid.uuid4()),
                "provider": "GOOGLE",
                "provider_subject": DEFAULT_SUBJECT,
                "provider_email": DEFAULT_EMAIL,
                "iss": settings.jwt_issuer,
                "aud": settings.pending_token_audience,
                "iat": int(now().timestamp()),
                "exp": int(now().timestamp()) + 600,
            },
            settings.jwt_private_key_path.read_bytes(),
            algorithm="EdDSA",
        )
        assert (await complete(client, forged)).status_code == 401

    async def test_token_with_the_session_audience_is_rejected(
        self, client: httpx.AsyncClient, google_enabled: str
    ) -> None:
        settings = get_settings()
        forged = jwt.encode(
            {
                "jti": str(uuid.uuid4()),
                "provider": "GOOGLE",
                "provider_subject": DEFAULT_SUBJECT,
                "provider_email": DEFAULT_EMAIL,
                "iss": settings.jwt_issuer,
                "aud": settings.jwt_audience,  # sutradhar/api, not /pending
                "iat": int(now().timestamp()),
                "exp": int(now().timestamp()) + 600,
            },
            settings.pending_token_secret,
            algorithm="HS256",
        )
        assert (await complete(client, forged)).status_code == 401

    async def test_j_pending_token_cannot_authenticate_anywhere(
        self, client: httpx.AsyncClient, token: str
    ) -> None:
        # (j) The blast radius of a leaked pending token. It is signed by this
        # server, so the question is what it can be spent on -- and the answer
        # has to be nothing but its own completion endpoint.
        headers = {"Authorization": f"Bearer {token}"}

        assert (await client.get(f"{AUTH}/me", headers=headers)).status_code == 401
        assert (
            await client.post(f"{AUTH}/logout-all", json={}, headers=headers)
        ).status_code == 401
        assert (
            await client.post(f"{AUTH}/refresh", json={}, headers=headers)
        ).status_code == 401

    async def test_pending_token_carries_no_authority(
        self, client: httpx.AsyncClient, token: str
    ) -> None:
        # No role, no user id, no scopes. Nothing a client could influence and
        # nothing a downstream check could mistake for permission.
        claims = jwt.decode(token, options={"verify_signature": False})
        for forbidden in ("role", "user_id", "sub", "scope", "scopes", "status"):
            assert forbidden not in claims
        assert set(claims) == {
            "jti",
            "provider",
            "provider_subject",
            "provider_email",
            "iss",
            "aud",
            "iat",
            "exp",
        }

    async def test_garbage_token_is_rejected(
        self, client: httpx.AsyncClient, google_enabled: str
    ) -> None:
        assert (await complete(client, "not-a-token")).status_code == 401

    async def test_complete_is_rate_limited_per_jti(
        self, client: httpx.AsyncClient, token: str
    ) -> None:
        limit = get_settings().rate_limit_complete_per_minute
        # The first burns the token; the rest are 401s that still count.
        for _ in range(limit):
            await complete(client, token)
        assert (await complete(client, token)).status_code == 429


# ---------------------------------------------------------------- role whitelist


class TestRoleWhitelist:
    @pytest_asyncio.fixture
    async def token(self, client: httpx.AsyncClient, provider: FakeGoogle) -> str:
        state = await begin_flow(client)
        with mocked(provider):
            redirect = await callback(client, state)
        return pending_token_from(redirect)

    # (k) Parametrised over the whole enum, so a role added later fails here
    # until somebody classifies it in app/auth/roles.py.
    @pytest.mark.parametrize("role", list(Role))
    async def test_only_self_assignable_roles_are_accepted(
        self, client: httpx.AsyncClient, token: str, role: Role
    ) -> None:
        response = await complete(client, token, role=str(role))
        if role in SELF_ASSIGNABLE_ROLES:
            assert response.status_code == 200
        else:
            assert response.status_code == 403
            assert response.json()["error"]["code"] == "ROLE_NOT_SELF_ASSIGNABLE"

    @pytest.mark.parametrize("role", ["ADMIN", "INSPECTOR", "COOP_OFFICER"])
    async def test_a_refused_role_creates_nothing(
        self, client: httpx.AsyncClient, token: str, session: AsyncSession, role: str
    ) -> None:
        assert (await complete(client, token, role=role)).status_code == 403
        assert await count(session, User) == 0
        assert await count(session, OAuthIdentity) == 0

    async def test_a_refused_role_does_not_burn_the_token(
        self, client: httpx.AsyncClient, token: str
    ) -> None:
        # The role check runs before the burn, so an honest mistake does not
        # cost the user their sign-up link.
        assert (await complete(client, token, role="ADMIN")).status_code == 403
        assert (await complete(client, token, role="CONSUMER")).status_code == 200

    async def test_unknown_role_is_a_validation_error(
        self, client: httpx.AsyncClient, token: str
    ) -> None:
        assert (await complete(client, token, role="SUPERUSER")).status_code == 422
