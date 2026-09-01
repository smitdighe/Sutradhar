"""What must be true of every route, every log line, and every error body.

Four properties, all asserted mechanically rather than by review:

**No route is unauthenticated by accident.** The public allowlist is written out
here, and every other operation in the live OpenAPI document must resolve an
authentication dependency. A route added next month with the ``Depends`` line
forgotten fails this file rather than shipping.

**Nothing identifying reaches a log.** The whole end-to-end flow is run with the
log stream captured and every emitted line searched for the seeded email, the
display name, the access token, the raw refresh token and the raw device
fingerprint. Phase 11 reports the fingerprint *source*; the value itself must
never appear anywhere.

**No secret reaches a response body.** Five values -- the signing key, the
pending-token secret, the password pepper, the Pinata JWT, the relayer key --
are searched for across every error this suite can provoke.

**Every limiter fires where it is configured to.** Not at a number written into
the test: at ``settings.rate_limit_*``, so tuning a limit in ``.env`` and
forgetting the consequence shows up here.
"""

from __future__ import annotations

import logging
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.routing import APIRoute

from app.api.health import router as health_router
from app.auth.guards import get_current_user, get_optional_user
from app.config import get_settings
from app.db.models.enums import UserRole
from tests.integration.helpers import (
    API,
    GUJARAT,
    PASSWORD,
    auth_headers,
    make_user,
    tagged_item,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

SETTINGS = get_settings()
BACKEND = Path(__file__).resolve().parents[2]

# Every operation reachable without a credential, and the reason each one is.
# Anything not on this list must resolve an authentication dependency.
PUBLIC_ALLOWLIST: dict[str, str] = {
    # Phase 11. The whole point: a shopper has no account.
    "GET /v/{tag_code}": "the public verification page",
    "POST /v/{tag_code}/scan": "recording a scan of a printed tag",
    # Probes. An orchestrator cannot hold a bearer token.
    "GET /healthz": "liveness",
    "GET /readyz": "readiness",
    # Auth. Every one of these exists to *obtain* a credential, so requiring one
    # would be circular.
    f"POST {API}/auth/register": "creating an account",
    f"POST {API}/auth/login": "exchanging credentials for a token",
    f"POST {API}/auth/refresh": "rotating a refresh token, which is its own credential",
    f"POST {API}/auth/logout": "clearing a session; the cookie is the credential",
    f"GET {API}/auth/oauth/providers": "which sign-in buttons to draw",
    f"GET {API}/auth/oauth/google/start": "beginning the authorization-code flow",
    f"GET {API}/auth/oauth/google/callback": "Google's redirect back, carrying state",
    f"POST {API}/auth/oauth/complete": "the pending token is the credential",
    # The GI catalogue. Public by design and documented as such in
    # `app.catalog.router` -- these describe what a Geographical Indication
    # *is*, which is public information published by the GI registry, and the
    # frontend renders a category page before anybody signs in.
    #
    # The phase brief named a narrower allowlist than this. These five are not
    # new and are not accidental; they are listed here rather than guarded
    # because guarding them would break the demo sequence they exist for. What
    # the audit did find is that one of them was doing real work unmetered --
    # see `POST /categories/{slug}/validate`, which now carries a limiter.
    f"GET {API}/categories": "the GI catalogue is public reference data",
    f"GET {API}/categories/{{slug}}": "one category's current schema",
    f"GET {API}/categories/{{slug}}/versions": "a category's version history",
    f"GET {API}/categories/{{slug}}/v/{{version}}": "one historical schema version",
    f"POST {API}/categories/{{slug}}/validate": (
        "dry-run validation for a form; writes nothing, and rate limited "
        "because it is the one unauthenticated endpoint that does real work"
    ),
    # FastAPI's own documentation surface.
    "GET /docs": "swagger ui",
    "GET /docs/oauth2-redirect": "swagger ui",
    "GET /redoc": "redoc",
    "GET /openapi.json": "the schema itself",
}

# Secrets that must never be echoed to a client, whatever goes wrong.
SECRET_VALUES: dict[str, str] = {
    "PENDING_TOKEN_SECRET": SETTINGS.pending_token_secret,
    "CURSOR_SECRET": SETTINGS.cursor_secret,
    "PASSWORD_PEPPER": SETTINGS.password_pepper,
    "PINATA_JWT": SETTINGS.pinata_jwt,
    "CHAIN_SIGNER_PRIVATE_KEY": SETTINGS.chain_signer_private_key,
    "IDENTITY_HASH_PEPPER": SETTINGS.identity_hash_pepper,
}


# ---------------------------------------------------------------- route guard


def _auth_dependencies(dependant: Any, seen: set[int] | None = None) -> set[str]:
    """Names of every dependency callable in the tree below *dependant*.

    Walked recursively because the guard is rarely on the route itself: it comes
    in through ``require_role``, which itself depends on ``get_current_user``,
    which depends on the bearer scheme. A check that only looked one level deep
    would pass every role-gated route for the wrong reason.
    """
    seen = seen if seen is not None else set()
    if id(dependant) in seen:
        return set()
    seen.add(id(dependant))

    found: set[str] = set()
    call = getattr(dependant, "call", None)
    if call is not None:
        found.add(getattr(call, "__qualname__", getattr(call, "__name__", "")))
    for child in getattr(dependant, "dependencies", ()):
        found |= _auth_dependencies(child, seen)
    return found


def _is_guarded(route: APIRoute) -> bool:
    names = _auth_dependencies(route.dependant)
    return any(
        name == get_current_user.__qualname__
        or name == get_optional_user.__qualname__
        # `require_role` returns a closure; its qualname carries the factory.
        or name.startswith("require_role")
        for name in names
    )


def _walk(routes: Any, prefix: str = "") -> list[tuple[str, APIRoute]]:
    """Every ``APIRoute`` under *routes*, with its full path.

    ``app.routes`` is not a flat list. FastAPI wraps each ``include_router``
    call in an ``_IncludedRouter`` that keeps the original router and its prefix
    and exposes no ``routes`` attribute of its own, so a walk that only looks at
    the top level finds nothing at all -- and a guard test built on it passes
    every route in the application by finding none of them. That is exactly what
    happened here, and it is why
    :meth:`TestEveryRouteIsGuarded.test_the_walk_sees_every_documented_path`
    exists below: a route inventory has to prove it is an inventory.
    """
    found: list[tuple[str, APIRoute]] = []
    for route in routes:
        if isinstance(route, APIRoute):
            for method in sorted(route.methods or set()):
                if method in {"HEAD", "OPTIONS"}:
                    continue
                found.append((f"{method} {prefix}{route.path}", route))
            continue

        original = getattr(route, "original_router", None)
        if original is not None:
            context = getattr(route, "include_context", None)
            found.extend(
                _walk(original.routes, prefix + (getattr(context, "prefix", "") or ""))
            )
            continue

        nested = getattr(route, "routes", None)
        if nested:
            found.extend(_walk(nested, prefix + (getattr(route, "prefix", "") or "")))
    return found


def _operations(application: Any) -> list[tuple[str, APIRoute]]:
    return _walk(application.routes)


class TestEveryRouteIsGuarded:
    async def test_the_walk_sees_every_documented_path(self) -> None:
        """The inventory must actually be an inventory.

        Checked against the OpenAPI document, which is generated by FastAPI
        itself from the same routers. If the two disagree the guard test below
        is inspecting a subset -- possibly an empty one -- and passing for the
        wrong reason.
        """
        from app.main import create_app

        application = create_app()
        walked = {
            f"{method.lower()} {path}"
            for label, _route in _operations(application)
            for method, path in [label.split(" ", 1)]
        }
        documented = {
            f"{method.lower()} {path}"
            for path, operations in application.openapi()["paths"].items()
            for method in operations
        }
        assert walked == documented, (
            "the route walk and the OpenAPI schema disagree.\n"
            f"  only in the walk:   {sorted(walked - documented)}\n"
            f"  only in the schema: {sorted(documented - walked)}"
        )

    async def test_no_route_is_public_by_accident(self) -> None:
        from app.main import create_app

        application = create_app()
        unguarded = [
            label
            for label, route in _operations(application)
            if label not in PUBLIC_ALLOWLIST and not _is_guarded(route)
        ]
        assert unguarded == [], (
            "these routes require no authentication and are not on the public "
            f"allowlist: {unguarded}. Either add the guard, or add the route to "
            "PUBLIC_ALLOWLIST with the reason it is public."
        )

    async def test_the_allowlist_has_no_stale_entries(self) -> None:
        """A route that was removed must not leave a permission behind.

        An allowlist nobody prunes eventually names a path that has been
        replaced, and the replacement inherits the exemption by accident.
        """
        from app.main import create_app

        application = create_app()
        live = {label for label, _route in _operations(application)}
        # Starlette mounts /docs and friends outside APIRoute, so those entries
        # are expected not to appear here.
        documentation = {
            "GET /docs",
            "GET /docs/oauth2-redirect",
            "GET /redoc",
            "GET /openapi.json",
        }
        stale = set(PUBLIC_ALLOWLIST) - live - documentation
        assert stale == set(), f"PUBLIC_ALLOWLIST names routes that no longer exist: {stale}"

    async def test_the_health_router_is_the_only_unprefixed_authenticated_gap(self) -> None:
        """The probes really are the only unprefixed non-public routes."""
        paths = {route.path for route in health_router.routes}  # type: ignore[attr-defined]
        assert paths == {"/healthz", "/readyz"}

    async def test_an_admin_route_actually_refuses_a_weaver(
        self, client: httpx.AsyncClient, session: Any
    ) -> None:
        """The allowlist proves a dependency exists; this proves it bites."""
        weaver = await make_user(session, UserRole.WEAVER, prefix="sec")
        headers = await auth_headers(client, weaver)

        response = await client.get(f"{API}/admin/system/status", headers=headers)
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "INSUFFICIENT_ROLE"

    async def test_the_admin_status_route_needs_a_token_at_all(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.get(f"{API}/admin/system/status")
        assert response.status_code == 401, response.text
        assert response.json()["error"]["code"] == "UNAUTHENTICATED"


# ---------------------------------------------------------------- log hygiene


class TestLogsCarryNoIdentities:
    async def test_a_full_flow_logs_no_identifying_value(
        self,
        client: httpx.AsyncClient,
        session: Any,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Run real traffic with the log captured, then read every line.

        The device fingerprint is the subtle one. Phase 11 records its *source*
        -- "client" or "derived" -- precisely so the raw value never has to be
        written down, and a debug line added in a hurry is exactly how it would
        end up in an aggregator anyway.
        """
        fingerprint = "raw-device-fingerprint-9f3a2b"

        with caplog.at_level(logging.DEBUG):
            weaver, headers, item_id, code = await tagged_item(client, session)
            login = await client.post(
                f"{API}/auth/login",
                json={"email": weaver.email, "password": PASSWORD},
            )
            assert login.status_code == 200, login.text
            refresh_token = login.cookies[SETTINGS.refresh_cookie_name]
            access_token = login.json()["access_token"]

            await client.get(f"/v/{code}")
            await client.post(
                f"/v/{code}/scan",
                json={"device_fingerprint": fingerprint},
                headers=GUJARAT,
            )
            await client.get(f"{API}/items/{item_id}", headers=headers)

        logged = caplog.text
        assert logged, "nothing was captured, so this test proved nothing"

        for label, value in (
            ("the weaver's email", weaver.email),
            ("the weaver's display name", weaver.display_name),
            ("an access token", access_token),
            ("a raw refresh token", refresh_token),
            ("the raw device fingerprint", fingerprint),
            ("the password", PASSWORD),
        ):
            assert value not in logged, f"{label} was written to a log line"

    async def test_the_access_log_carries_the_user_id_when_authenticated(
        self, client: httpx.AsyncClient, session: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An id, so a request can be traced to an account. Never an email."""
        weaver = await make_user(session, UserRole.WEAVER, prefix="sec")
        headers = await auth_headers(client, weaver)

        with caplog.at_level(logging.INFO):
            response = await client.get(f"{API}/auth/me", headers=headers)

        assert response.status_code == 200, response.text
        assert str(weaver.id) in caplog.text, (
            "an authenticated request logged no user id, so a report of "
            "'something went wrong for me' cannot be traced to an account"
        )
        assert weaver.email not in caplog.text

    async def test_an_anonymous_request_logs_no_user(
        self, client: httpx.AsyncClient, session: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        _weaver, _headers, _item_id, code = await tagged_item(client, session)

        with caplog.at_level(logging.INFO):
            await client.get(f"/v/{code}")

        # The public read has no caller to name, and inventing one would be
        # worse than the field being absent.
        access_lines = [
            line for line in caplog.text.splitlines() if f"/v/{code}" in line
        ]
        assert access_lines, "the public read produced no access log line"
        assert all("user_id=None" in line or "user_id" not in line for line in access_lines), (
            f"an anonymous request logged a user: {access_lines}"
        )


# ---------------------------------------------------------------- secrets


class TestNoSecretInAnyBody:
    async def test_provoked_errors_never_echo_a_configured_secret(
        self, client: httpx.AsyncClient, session: Any
    ) -> None:
        weaver = await make_user(session, UserRole.WEAVER, prefix="sec")
        headers = await auth_headers(client, weaver)

        bodies: list[str] = []
        for response in (
            await client.get(f"{API}/items/{uuid.uuid4()}", headers=headers),
            await client.get(f"{API}/items?cursor=not-a-real-cursor", headers=headers),
            await client.get("/v/NOTAREALCODE"),
            await client.post(f"{API}/items", json={}, headers=headers),
            await client.post(f"{API}/auth/refresh", json={"refresh_token": "nope"}),
            await client.post(
                f"{API}/auth/oauth/complete",
                json={"pending_token": "nope", "role": "CONSUMER", "display_name": "x"},
            ),
            await client.get(f"{API}/admin/system/status"),
        ):
            bodies.append(response.text)
            assert response.status_code != 500, response.text

        joined = "\n".join(bodies)
        for name, value in SECRET_VALUES.items():
            if not value:
                # An unset optional secret cannot leak, and searching for the
                # empty string would match everything.
                continue
            assert value not in joined, f"{name} appeared in an error body"

    async def test_the_signing_key_is_never_in_a_body(
        self, client: httpx.AsyncClient
    ) -> None:
        private_key = SETTINGS.jwt_private_key_path.read_text(encoding="utf-8")
        body = private_key.replace("-----BEGIN PRIVATE KEY-----", "").strip()

        response = await client.get(f"{API}/items")
        assert response.status_code == 401
        assert body[:40] not in response.text
        assert "BEGIN PRIVATE KEY" not in response.text


# ---------------------------------------------------------------- limiters


class TestRateLimits:
    """Each limiter, at its own configured threshold.

    Read from settings rather than hard-coded: a limit tuned in ``.env`` without
    thinking about the consequence should fail here, not in production.
    """

    async def test_login_per_account(
        self, client: httpx.AsyncClient, session: Any
    ) -> None:
        user = await make_user(session, UserRole.CONSUMER, prefix="rl")
        limit = SETTINGS.rate_limit_login_per_minute
        body = {"email": user.email, "password": "wrong-password-entirely"}

        for attempt in range(limit):
            response = await client.post(f"{API}/auth/login", json=body)
            assert response.status_code == 401, (
                f"attempt {attempt + 1} of {limit} was limited early: {response.text}"
            )

        limited = await client.post(f"{API}/auth/login", json=body)
        assert limited.status_code == 429, limited.text
        assert limited.json()["error"]["code"] == "RATE_LIMITED"
        assert int(limited.headers["Retry-After"]) >= 1

    async def test_register(self, client: httpx.AsyncClient) -> None:
        limit = SETTINGS.rate_limit_register_per_hour

        for attempt in range(limit):
            response = await client.post(
                f"{API}/auth/register",
                json={
                    "email": f"rl-{uuid.uuid4().hex[:10]}@example.com",
                    "password": PASSWORD,
                    "display_name": "Rate Limited",
                    "role": "CONSUMER",
                },
            )
            assert response.status_code == 201, (
                f"registration {attempt + 1} of {limit} failed: {response.text}"
            )

        limited = await client.post(
            f"{API}/auth/register",
            json={
                "email": f"rl-{uuid.uuid4().hex[:10]}@example.com",
                "password": PASSWORD,
                "display_name": "One Too Many",
                "role": "CONSUMER",
            },
        )
        assert limited.status_code == 429, limited.text
        assert limited.json()["error"]["code"] == "RATE_LIMITED"

    async def test_refresh(self, client: httpx.AsyncClient, session: Any) -> None:
        """Counted per owning account, and counted before the rotation runs.

        Every request after the first presents a token that reuse-detection has
        already killed, so they are 401s -- which is the point: a limiter that
        only counted successes would let an attacker spin freely on failures.
        """
        user = await make_user(session, UserRole.CONSUMER, prefix="rl")
        login = await client.post(
            f"{API}/auth/login", json={"email": user.email, "password": PASSWORD}
        )
        token = login.cookies[SETTINGS.refresh_cookie_name]

        limit = SETTINGS.rate_limit_refresh_per_minute
        for _ in range(limit):
            response = await client.post(
                f"{API}/auth/refresh", json={"refresh_token": token}
            )
            assert response.status_code != 429, response.text

        limited = await client.post(f"{API}/auth/refresh", json={"refresh_token": token})
        assert limited.status_code == 429, limited.text

    async def test_oauth_start(
        self, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Limited before the provider check, so an unconfigured provider still counts.

        The bucket is consumed first on purpose: otherwise a deployment with no
        Google credentials would offer an unmetered endpoint, which is a free
        way to make this service open database connections.
        """
        limit = SETTINGS.rate_limit_oauth_start_per_minute

        for _ in range(limit):
            response = await client.get(f"{API}/auth/oauth/google/start")
            assert response.status_code != 429, response.text

        limited = await client.get(f"{API}/auth/oauth/google/start")
        assert limited.status_code == 429, limited.text
        assert limited.json()["error"]["code"] == "RATE_LIMITED"

    async def test_oauth_complete(self, client: httpx.AsyncClient) -> None:
        """Keyed on the pending token's jti, so one sign-up attempt is limited.

        Not on the IP: a whole co-operative signing up from one connection is a
        normal afternoon, and keying on the address would lock out the fourth
        weaver through the door.
        """

        # A token that will fail to burn, so every request costs a bucket and
        # nothing else. The limiter runs before the burn.
        limit = SETTINGS.rate_limit_complete_per_minute
        body = {
            "pending_token": _forged_pending_token(),
            "role": "CONSUMER",
            "display_name": "Rate Limited",
        }

        for _ in range(limit):
            response = await client.post(f"{API}/auth/oauth/complete", json=body)
            assert response.status_code != 429, response.text

        limited = await client.post(f"{API}/auth/oauth/complete", json=body)
        assert limited.status_code == 429, limited.text

    async def test_public_scan(self, client: httpx.AsyncClient, session: Any) -> None:
        """The read and the write share a threshold and separate buckets."""
        _weaver, _headers, _item_id, code = await tagged_item(client, session)
        limit = SETTINGS.rate_limit_scan_per_minute

        for _ in range(limit):
            response = await client.post(
                f"/v/{code}/scan", json={"device_fingerprint": "phone"}, headers=GUJARAT
            )
            assert response.status_code != 429, response.text

        limited = await client.post(
            f"/v/{code}/scan", json={"device_fingerprint": "phone"}, headers=GUJARAT
        )
        assert limited.status_code == 429, limited.text
        assert limited.json()["error"]["code"] == "RATE_LIMITED"

    async def test_catalog_validate(
        self, client: httpx.AsyncClient, session: Any
    ) -> None:
        """The one unauthenticated endpoint that compiles and runs a schema.

        Found by the route audit in this file: it was public, which is by
        design, and unmetered, which was not. Loading a schema and validating
        arbitrary JSON against it is real CPU, and this service runs as a single
        process on a free tier.
        """
        from tests.integration.helpers import PATOLA, load_catalogue

        await load_catalogue(session)
        limit = SETTINGS.rate_limit_scan_per_minute
        body = {"attributes": PATOLA}

        for _ in range(limit):
            response = await client.post(
                f"{API}/categories/patola-silk/validate", json=body
            )
            assert response.status_code != 429, response.text

        limited = await client.post(
            f"{API}/categories/patola-silk/validate", json=body
        )
        assert limited.status_code == 429, limited.text
        assert limited.json()["error"]["code"] == "RATE_LIMITED"

    async def test_public_read(self, client: httpx.AsyncClient, session: Any) -> None:
        """A public GET that recomputes a keccak256 is not free either."""
        _weaver, _headers, _item_id, code = await tagged_item(client, session)
        limit = SETTINGS.rate_limit_scan_per_minute

        for _ in range(limit):
            response = await client.get(f"/v/{code}")
            assert response.status_code != 429, response.text

        limited = await client.get(f"/v/{code}")
        assert limited.status_code == 429, limited.text


def _forged_pending_token() -> str:
    """A syntactically valid pending token for a jti that was never issued.

    Signed with the real secret so it passes verification and reaches the
    limiter, which is the code under test; the burn then fails, which is fine.
    """
    import datetime as dt

    import jwt

    now = dt.datetime.now(dt.UTC)
    return jwt.encode(
        {
            "jti": str(uuid.uuid4()),
            "provider": "GOOGLE",
            "provider_subject": "1234567890",
            "provider_email": "nobody@example.com",
            "iss": SETTINGS.jwt_issuer,
            "aud": SETTINGS.pending_token_audience,
            "iat": int(now.timestamp()),
            "exp": int((now + dt.timedelta(minutes=5)).timestamp()),
        },
        SETTINGS.pending_token_secret,
        algorithm="HS256",
    )


# ---------------------------------------------------------------- CORS


class TestCors:
    async def test_a_non_allowlisted_origin_gets_no_grant(
        self, client: httpx.AsyncClient, session: Any
    ) -> None:
        _weaver, _headers, _item_id, code = await tagged_item(client, session)

        response = await client.get(
            f"/v/{code}", headers={"Origin": "https://not-our-frontend.example"}
        )
        assert "access-control-allow-origin" not in response.headers, (
            "an arbitrary origin was granted access to the API"
        )

    async def test_the_configured_origin_is_granted(
        self, client: httpx.AsyncClient, session: Any
    ) -> None:
        _weaver, _headers, _item_id, code = await tagged_item(client, session)
        allowed = SETTINGS.cors_origins[0]

        response = await client.get(f"/v/{code}", headers={"Origin": allowed})
        assert response.headers.get("access-control-allow-origin") == allowed

    async def test_etag_and_scan_recorded_are_exposed(
        self, client: httpx.AsyncClient, session: Any
    ) -> None:
        """The frontend reads both across origins, so both must be exposed.

        Without ``Access-Control-Expose-Headers`` a browser hides them, and the
        page silently loses conditional reads and the ability to tell a fresh
        scan from a deduplicated retry.
        """
        _weaver, _headers, _item_id, code = await tagged_item(client, session)
        allowed = SETTINGS.cors_origins[0]

        read = await client.get(f"/v/{code}", headers={"Origin": allowed})
        exposed = {
            name.strip().lower()
            for name in read.headers.get("access-control-expose-headers", "").split(",")
        }
        assert "etag" in exposed, read.headers
        assert "x-scan-recorded" in exposed, read.headers
        assert "x-request-id" in exposed, read.headers

    async def test_the_headers_appear_on_the_public_routes(
        self, client: httpx.AsyncClient, session: Any
    ) -> None:
        _weaver, _headers, _item_id, code = await tagged_item(client, session)

        read = await client.get(f"/v/{code}")
        assert read.headers.get("ETag"), "the public read served no ETag"

        scan = await client.post(
            f"/v/{code}/scan", json={"device_fingerprint": "phone"}, headers=GUJARAT
        )
        assert scan.headers.get("X-Scan-Recorded") == "true"

    async def test_they_do_not_appear_on_authenticated_reads(
        self, client: httpx.AsyncClient, session: Any
    ) -> None:
        """These two headers belong to the public surface and nothing else."""
        weaver = await make_user(session, UserRole.WEAVER, prefix="sec")
        headers = await auth_headers(client, weaver)

        response = await client.get(f"{API}/auth/me", headers=headers)
        assert "X-Scan-Recorded" not in response.headers


# ---------------------------------------------------------------- the greps


def _grep(pattern: str, path: Path) -> list[str]:
    """Case-insensitive extended-regex search, as a list of ``file:line:text``.

    Shelling out to ``grep`` rather than walking the tree in Python because the
    phase brief specifies these two commands and an operator will run them by
    hand; the test has to be searching the same way they will be.
    """
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            "grep",
            "-rniE",
            "--include=*.py",
            "--include=*.md",
            "--include=*.json",
            pattern,
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        pytest.skip(f"grep unavailable: {result.stderr.strip()}")
    return [line for line in result.stdout.splitlines() if line.strip()]


def _outside(lines: list[str], allowed: tuple[str, ...]) -> list[str]:
    return [
        line
        for line in lines
        if not any(marker in line.replace("\\", "/") for marker in allowed)
    ]


class TestVerdictLanguage:
    """No verdict words outside the places that are documented and tested.

    The rule is not stylistic. This system reports who vouched and what a scan
    pattern looks like; the moment a payload claims an object is real it has made
    a promise about a physical thing that no amount of cryptography backs.
    """

    # The pattern from the phase brief, with word boundaries around the two
    # words that have innocent longer forms. The bare pattern matches the whole
    # `authentication` family, which this codebase says constantly and must; a
    # check that fires on every one of those is a check somebody switches off.
    # The narrowing is what makes it a rule rather than noise.
    PATTERN = r"\bgenuine\b|\bauthentic\b|counterfeit.proof|is_fake|is_real|\bgithub\b"

    # Where the surviving words are legitimate: prose that names the attack in
    # order to explain the defence against it. A file, not a directory, so a new
    # occurrence anywhere else fails.
    ALLOWED = ("/app/provenance/mass_balance.py",)

    def test_no_verdict_or_github_language_in_the_backend(self) -> None:
        hits = _grep(self.PATTERN, BACKEND / "app")
        stray = _outside(hits, self.ALLOWED)
        assert stray == [], (
            "verdict language, or a GitHub reference in a project with no "
            "GitHub integration, outside the documented allowlist:\n"
            + "\n".join(stray)
        )

    def test_the_allowed_occurrences_are_prose_and_not_output(self) -> None:
        """The exemption covers an explanation, not a string a user could see.

        ``mass_balance.py`` uses the word twice, in a docstring describing the
        substitution attack the module exists to prevent. If either ever moved
        into a string literal it would be one step from a response body.
        """
        literal = re.compile(r"""["'].*\b(genuine|authentic)\b""", re.IGNORECASE)
        for line in _grep(self.PATTERN, BACKEND / "app"):
            text = line.split(":", 2)[-1]
            assert not literal.search(text), (
                f"a verdict word appears inside a string literal: {line}"
            )

    def test_the_public_package_never_names_an_accusation(self) -> None:
        """Narrower and stricter: the words a shopper could actually read.

        Phase 11's claim message says a tag was already claimed on a date and
        suggests asking the seller. It does not say the object is a copy, because
        a retail display gets scanned by dozens of people who are doing nothing
        wrong, and being wrong at that moment, in public, to the one customer who
        cared enough to check, is the worst place this system could be wrong.
        """
        accusation = re.compile(
            r"""["'].*(counterfeit|fake|duplicate|stolen)""", re.IGNORECASE
        )
        offending = [
            line
            for line in _grep("counterfeit|fake|duplicate|stolen", BACKEND / "app" / "verification")
            if accusation.search(line)
        ]
        assert offending == [], (
            "an accusatory word appears in a string literal on the public "
            "surface:\n" + "\n".join(offending)
        )

    def test_the_grep_actually_finds_things(self) -> None:
        """A grep that silently matches nothing is a test that always passes."""
        assert _grep("provenance", BACKEND / "app"), (
            "the grep helper found nothing at all; it is not searching"
        )
