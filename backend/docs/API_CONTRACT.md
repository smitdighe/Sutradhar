# Sutradhar API — frozen contract

**Status: frozen.** Generated from the shipped implementation, not from a design
document. Every endpoint below was pulled from the running application's OpenAPI
schema, and every response example was copied out of a passing test response.
`tests/contract/test_contract_matches_implementation.py` fails the build if this
file and the application stop agreeing.

The system proves **who claimed what, and when**. It does not prove that a
physical object is what somebody says it is, and no field in this API says that
it does. Section 5 is the part a frontend developer cannot infer from the
schemas; read it before designing any screen.

Application version `0.1.0`. OpenAPI document at `GET /openapi.json`, Swagger UI
at `GET /docs`, ReDoc at `GET /redoc`.

---

## 1. Conventions

### 1.1 Base URLs

Two mounts, and they are not the same base.

| Mount | Value | What lives there |
|---|---|---|
| `{API_PREFIX}` | `/api/v1` | Everything a signed-in client calls |
| `{PUBLIC_PREFIX}` | `""` (empty) | `/v/{tag_code}` and `/v/{tag_code}/scan` |
| unprefixed | — | `/healthz`, `/readyz`, `/docs`, `/redoc`, `/openapi.json` |

**The public scan path is NOT under `/api/v1`.** `GET /v/{tag_code}` sits at the
origin root. This is deliberate and permanent: that path is printed onto cloth
inside a QR code, one segment, no API version, and a printed URL cannot be
migrated. `PUBLIC_PREFIX` is an operator setting that defaults to empty; if it is
ever set, it becomes the prefix of both public routes, and the value is validated
at boot (a scheme, a host, whitespace, `?` or `#` all refuse to start the
process).

`PUBLIC_BASE_URL` is a **separate** setting and is the *frontend* origin. It is
the base of the QR payload, so what a phone opens is the frontend's page. This
service answers the same path shape on its own origin, and the frontend page
calls it cross-origin with CORS.

CORS exposes `X-Request-ID`, `ETag` and `X-Scan-Recorded`. Credentials are
allowed; the allowed origins come from `CORS_ALLOWED_ORIGINS`.

### 1.2 Response envelope

A **single resource is a bare object.** There is no `{"success": true, ...}`
wrapper — the status line already carries success or failure.

```json
{
  "id": "01a0485f-d0bd-7601-bdcd-368ca29b8543",
  "email": "weaver-9ecbc9bd@example.com",
  "display_name": "Kanubhai R. Patel",
  "role": "WEAVER",
  "status": "PENDING_VERIFICATION",
  "region": "Patan, Gujarat",
  "org_name": "Patan Weavers Co-operative",
  "created_at": "2026-08-28T12:37:14.044581Z",
  "last_login_at": "2026-08-28T12:37:14.268547Z"
}
```

A **collection is `{"data": [...], "pagination": {...}}`.**

```json
{
  "data": [
    {
      "id": "01a04861-a927-7b40-9453-2eabbfd208b4",
      "category_id": "01a04861-a729-7bf3-9a3a-5214835724a6",
      "category_schema_version": 1,
      "parent_id": "01a04861-a84b-7b11-98c1-a4c84c9938aa",
      "registered_by": "01a04861-a77d-77f0-b757-22ed02c1bff5",
      "quantity": "6.0000",
      "quantity_unit": "metre",
      "item_hash": "0x4181a60e4bab57bfa3f930393536ca5d587c44f5c9b7b88f89eee60e4243a622",
      "tag_code": null,
      "status": "PENDING",
      "dispute_status": "NONE",
      "created_at": "2026-08-28T12:39:14.977076Z"
    }
  ],
  "pagination": { "next_cursor": null, "limit": 20 }
}
```

Five collections do not use that envelope, and each is called out at its own
entry below. They are `GET {API_PREFIX}/items/{item_id}/attestations`
(`{"items": [...], "next_cursor": ...}`), `GET {API_PREFIX}/categories/{slug}/versions`
(`{"slug": ..., "data": [...]}`, no pagination), `GET {API_PREFIX}/auth/oauth/providers`
(`{"data": [...]}`, no pagination), and the two that return a **bare JSON array**:
`GET {API_PREFIX}/items/{item_id}/tree` and `GET {API_PREFIX}/items/{item_id}/media`.

### 1.3 Error body

Every failure, from every route, is exactly this shape:

```json
{
  "error": {
    "code": "MASS_BALANCE_EXCEEDED",
    "message": "cannot allocate 9.0000 metre; only 0.5000 remains of this item",
    "details": {
      "parent_id": "01a04842-76c0-72c1-b417-0be953c395e1",
      "parent_quantity": "12.0000",
      "already_allocated": "11.5000",
      "remaining": "0.5000",
      "requested": "9.0000"
    },
    "request_id": "0a1c9d17fa324466b37dee6d7f3230cd"
  }
}
```

* `code` — a stable SCREAMING_SNAKE string from section 4. **Branch on this, never on `message`.**
* `message` — human-readable English. Wording is not part of the contract.
* `details` — endpoint-specific object, or `null`. The shape per code is given in section 4 where it carries one.
* `request_id` — the same value as the `X-Request-ID` response header. This is what a user quotes when reporting a failure.

`422` from request-shape validation carries a list rather than an object in
`details`, one entry per invalid field:

```json
{
  "error": {
    "code": "VALIDATION_FAILED",
    "message": "request validation failed",
    "details": [
      { "loc": ["body", "email"], "msg": "value is not a valid email address: An email address must have an @-sign.", "type": "value_error" },
      { "loc": ["body", "password"], "msg": "Field required", "type": "missing" }
    ],
    "request_id": "02be9d8a297b4b14a087b2e9c83afd20"
  }
}
```

A `500` never carries internal detail. It is always
`{"code": "INTERNAL_ERROR", "message": "an internal error occurred", "details": null, "request_id": "..."}`.

### 1.4 Datetimes

RFC 3339, UTC, terminated by `Z`, with **exactly six fractional digits, always** —
including when the instant lands on a whole second.

```
2026-08-28T12:37:14.044581Z      <- an ordinary timestamp
1970-01-01T00:00:00.000000Z      <- the Unix epoch, still six digits
```

Never `+00:00`, never a missing fractional part, never a local offset. Request
bodies contain no datetime fields anywhere in this API, so this rule is about
reading only.

### 1.5 Identifiers

Two namespaces. **They are never interchangeable, and no endpoint accepts one
where it documents the other.**

**Resource ids** are UUIDv7, canonical hyphenated lowercase, 36 characters:

```
01a0485f-d0bd-7601-bdcd-368ca29b8543
```

The timestamp prefix makes them sort in creation order, which is what keeps
cursor pagination stable. Items, users, categories, attestations, media and
events all use this form.

**Tag codes** are 12 characters over a reduced Crockford base32 alphabet
(`0123456789BCDFGHJKMNPQRSTVWXY` — no A, E, I, L, O, U or Z), of which the last
is a mod-29 check symbol. The **canonical form is uppercase with no separators**
and is what the API stores, compares and returns in `tag_code`:

```
B69C8K9853SS
```

The **display form** groups it in fours and is returned separately as
`display_code`. It is for printing and for reading aloud, and is never compared:

```
B69C-8K98-53SS
```

Any public route that takes a tag code in the path normalises the input first:
it uppercases, strips `-`, ` ` and `_`, and folds the excluded characters onto
what the reader almost certainly saw (`I`/`L` → `1`, `O` → `0`, `U` → `V`,
`Z` → `2`). So `b69c-8k98-53ss` resolves to the same item as `B69C8K9853SS`.
The check symbol is verified **before any database lookup**, so a mistyped code
returns `INVALID_TAG_CODE` (400) rather than a 404.

### 1.6 Decimals

**Every quantity is a JSON string with exactly four decimal places. Never a JSON
number.**

```json
{ "quantity": "12.0000", "quantity_unit": "metre" }
```

Requests send them as strings too (`"12.0000"`, or `"12"` — the server quantises
to 4dp on the way in). The reason is not style: the quantity is part of the
frozen preimage that produces `item_hash`. A value that becomes
`12.000000000000002` anywhere in a JavaScript client, or that loses its trailing
zeros, produces a different hash and a `MISMATCH` on the public page. Parse these
with a decimal library, never with `parseFloat`.

The same rule applies to the budget figures in `GET {API_PREFIX}/admin/system/status`,
which are `numeric(28,4)` and are serialised as `"268435456.0000"`.

### 1.7 Pagination

Two mechanisms. Which one an endpoint uses is stated at its entry, and is visible
from the key inside `pagination`.

**Keyset cursors** — `next_cursor`. Used where the table grows without bound:
`GET {API_PREFIX}/items` and `GET {API_PREFIX}/items/{item_id}/attestations`.

* Request `?limit=N&cursor=<opaque>`.
* `limit` defaults to **20** and is capped at **100**. Every paginated route declares `le=100` on the query parameter, so `?limit=1000` is `VALIDATION_FAILED` (422) rather than a clamped page. Ask for at most 100.
* `next_cursor` is an opaque, HMAC-signed, base64url string. Treat it as bytes: do not parse, construct, or modify it. A tampered cursor is `INVALID_CURSOR` (422), never silently coerced.
* `next_cursor` is `null` when the page returned fewer rows than the limit, which means there is nothing after it. Stop when it is `null`.
* Ordering is `created_at DESC, id DESC` — newest first.

**Offset paging** — `next_offset`. Used where the table is small or the order is
not temporal: `GET {API_PREFIX}/categories` (ordered by slug) and
`GET {API_PREFIX}/items/{item_id}/events` (oldest first, append-only).

* Request `?limit=N&offset=M`.
* `next_offset` is `null` at the end of the collection.

**There is no total count anywhere, and there will not be one.** `COUNT(*)` on
PostgreSQL is a full scan, and the tables that would most want a count are the
ones that grow fastest. Design list screens for "load more", not for "page 7 of
412".

### 1.8 Idempotency

`Idempotency-Key` is a request header carrying any client-chosen string. Records
are scoped to `(user_id, key)` and expire after 24 hours.

| Endpoint | `Idempotency-Key` |
|---|---|
| `POST {API_PREFIX}/items` | **required** |
| `POST {API_PREFIX}/items/{item_id}/split` | **required** |
| `POST {API_PREFIX}/items/{item_id}/tag` | **required** |
| `POST {API_PREFIX}/admin/categories` | optional |
| `POST {API_PREFIX}/admin/categories/{slug}/versions` | optional |
| everything else | not read |

Omitting it where it is required is `VALIDATION_FAILED` (422) with the message
`the Idempotency-Key header is required for this request`.

**A replay** — same key, byte-identical body — returns the **stored original
response, verbatim, with the original status code.** A replayed
`POST {API_PREFIX}/items` returns `201` and the same item id, and creates nothing:

```json
{
  "id": "01a0485f-deee-7cb0-8436-6d85ac834654",
  "category_id": "01a0485f-ddac-7912-b44d-5308d300f49e",
  "category_schema_version": 1,
  "parent_id": null,
  "registered_by": "01a0485f-de14-76f1-9897-4c742f4b77b5",
  "quantity": "9.0000",
  "quantity_unit": "metre",
  "item_hash": "0x050c32c2474d7f68fcf9b3d11d6455f8e998d5f1d13eca4f619117ed38b87392",
  "tag_code": null,
  "status": "PENDING",
  "dispute_status": "NONE",
  "created_at": "2026-08-28T12:37:17.677751Z"
}
```

**A key reused with a different body** is `IDEMPOTENCY_KEY_REUSED` (409). The new
request is not performed and the old response is not returned — silently
swallowing a different request would be worse than either:

```json
{
  "error": {
    "code": "IDEMPOTENCY_KEY_REUSED",
    "message": "idempotency key was already used for a different request",
    "details": { "key": "b3aec9fd6ab94ffca566aa2925d63767" },
    "request_id": "4e8fc373045040acbca8e15e2ad6882a"
  }
}
```

A key claimed by a request that crashed before finishing is treated as still in
flight: the retry does the work and records the response.

### 1.9 Rate limits

Fixed-window counters in PostgreSQL. Every limit below is per window, and the
window resets on a wall-clock boundary rather than a sliding one.

| Endpoint | Scope | Limit | Window | Counted per |
|---|---|---|---|---|
| `POST {API_PREFIX}/auth/register` | `register` | 10 | 1 hour | client IP |
| `POST {API_PREFIX}/auth/login` | `login_ip` | 20 | 1 minute | client IP |
| `POST {API_PREFIX}/auth/login` | `login` | 5 | 1 minute | client IP + email |
| `POST {API_PREFIX}/auth/refresh` | `refresh` | 30 | 1 minute | owning user |
| `POST {API_PREFIX}/auth/refresh` | `refresh_ip` | 60 | 1 minute | client IP, when the token matches no user |
| `GET {API_PREFIX}/auth/oauth/google/start` | `oauth_start` | 10 | 1 minute | client IP |
| `POST {API_PREFIX}/auth/oauth/complete` | `oauth_complete` | 5 | 1 minute | pending-token `jti` |
| `POST {API_PREFIX}/categories/{slug}/validate` | `catalog_validate` | 60 | 1 minute | client IP |
| `GET /v/{tag_code}` | `public_verify` | 60 | 1 minute | client IP |
| `POST /v/{tag_code}/scan` | `public_scan` | 60 | 1 minute | client IP |

Every other endpoint is unlimited. Login is counted **before** the password is
checked, so a wrong password costs exactly what a right one does.

Exceeding a limit is `429` with a `Retry-After` header in whole seconds, mirrored
into `details.retry_after`:

```
HTTP/1.1 429 Too Many Requests
Retry-After: 1365
```

```json
{
  "error": {
    "code": "RATE_LIMITED",
    "message": "rate limit exceeded for register",
    "details": { "retry_after": 1365, "scope": "register", "limit": 10, "window_seconds": 3600 },
    "request_id": "b3a02eaa192e4bc3bb65f1c9c455ab62"
  }
}
```

### 1.10 Cache headers

**`GET /v/{tag_code}`** carries `Cache-Control: public, max-age=60` and a strong
`ETag` over the payload:

```
Cache-Control: public, max-age=60
ETag: "278e7bf88fc46bcb3bb43a7f11c7aeb2"
```

Send that value back as `If-None-Match` and an unchanged record answers `304 Not
Modified` with no body and the same two headers. Sixty seconds is deliberate:
trust level and dispute state change on the next read, and somebody standing in a
shop deserves the current answer.

`chain_checked_at` is **excluded** from the ETag computation. It moves on every
request by construction, and including it would produce a new ETag every time.

**`POST /v/{tag_code}/scan`** is `Cache-Control: no-store` — it is a write, and
the claim block differs per device. It also carries `X-Scan-Recorded: true|false`
so a client can tell a fresh scan from a deduplicated retry without diffing the
payload.

**`GET {API_PREFIX}/items/{item_id}/tag/qr`** is
`Cache-Control: public, max-age=31536000, immutable`. A tag code is immutable
once bound and the payload is derived from it, so the image never changes. It
also carries `Content-Disposition: inline; filename="<TAG_CODE>.png"`.

**`GET {API_PREFIX}/media/{media_id}/raw`** is the same year-long immutable
policy, plus `ETag: "<sha256>"`, `X-Content-Type-Options: nosniff` and
`X-Sutradhar-Tier: MIRROR|BLOB` naming which copy served the bytes.

**Every route under `{API_PREFIX}/auth`** carries `Cache-Control: no-store` and
`Pragma: no-cache`. So do the OAuth routes.

---

## 2. Auth

### 2.1 Token model

**Access token** — an Ed25519-signed JWT (`alg: EdDSA`), TTL **900 seconds**
(15 minutes), `iss: sutradhar`, `aud: sutradhar/api`. Sent as
`Authorization: Bearer <token>`. Claims are `sub` (user id), `role`, `iss`,
`aud`, `iat`, `exp`, `jti`, `ver`. Stateless: verifying it costs no database
round trip. Do not parse it for authorisation decisions — read `role` from
`GET {API_PREFIX}/auth/me` instead, which reflects the live account.

**Refresh token** — 32 random bytes, **not** a JWT. Delivered as an
`HttpOnly` cookie named `sutradhar_rt`, `SameSite=Lax`, `Path=/api/v1/auth`,
`Max-Age=2592000` (30 days), `Secure` when `REFRESH_COOKIE_SECURE` is on. It is
unreadable from JavaScript by design. Only its SHA-256 is stored server-side.

Refresh **rotates**: every successful `POST {API_PREFIX}/auth/refresh` issues a
new token and invalidates the presented one. Presenting an already-rotated token
is treated as theft — the whole family, including the successor just issued, is
revoked and the caller gets `REFRESH_TOKEN_REUSED` (401). Both parties are logged
out; that is the intended outcome.

Non-browser clients that have no cookie jar may send `{"refresh_token": "..."}`
in the body of `/auth/refresh` and `/auth/logout` instead. The cookie wins when
both are present.

### 2.2 Email and password flow

1. `POST {API_PREFIX}/auth/register` with email, password, display name, and optionally `role`, `region`, `org_name`. Returns `201` and the user. **It does not log the caller in** — no token, no cookie.
2. `POST {API_PREFIX}/auth/login` with email and password. Returns `200` with `access_token`, `expires_in`, the user, and sets the refresh cookie.
3. Send `Authorization: Bearer <access_token>` on every authenticated request.
4. When a call returns `401 TOKEN_EXPIRED`, call `POST {API_PREFIX}/auth/refresh` (body `{}` is fine — the cookie carries the credential), then retry the original request with the new access token.
5. `POST {API_PREFIX}/auth/logout` revokes the presented token's family and clears the cookie. It returns `204` even when no token was presented — a caller who wanted to be logged out is logged out.
6. `POST {API_PREFIX}/auth/logout-all` revokes every family for the authenticated user. Requires a bearer token.

Passwords are hashed with argon2id and are never returned by any endpoint.

### 2.3 Google OAuth flow

**Google is the only OAuth provider.** There is no second provider, configured or
planned, and `GET {API_PREFIX}/auth/oauth/providers` will only ever list `google`.

1. **Ask what is available.** `GET {API_PREFIX}/auth/oauth/providers` → `200`, always. Render the Google button only when `enabled` is `true`.

   ```json
   { "data": [ { "provider": "google", "enabled": true } ] }
   ```

   With credentials absent it is still `200`, with `"enabled": false`.

2. **Send the browser to `GET {API_PREFIX}/auth/oauth/google/start`**, optionally with `?return_to=<url>`. This is a **full-page navigation, not an XHR** — it answers `302`. `return_to` must be on the allowlisted origin set or the request is `VALIDATION_FAILED`.

   The response is `302` to `https://accounts.google.com/o/oauth2/v2/auth` with
   `client_id`, `redirect_uri`, `response_type=code`, `scope=openid email profile`,
   `state`, `code_challenge`, `code_challenge_method=S256`, `access_type=online`,
   `prompt=select_account`. The `state` is a signed, single-use value carrying the
   PKCE verifier and the `return_to`. Nothing is written to the database yet.

   If Google is not configured this step is `503 OAUTH_PROVIDER_UNAVAILABLE`.

3. **Google redirects the browser back to `GET {API_PREFIX}/auth/oauth/google/callback`** with `code` and `state` — or with `error` if the user cancelled. **This endpoint never returns JSON.** Every outcome is a `302` to a frontend URL, and the provider's own error text is never propagated.

   The server verifies the state signature and age, spends the nonce (so a
   replayed callback never reaches Google), exchanges the code, and verifies the
   ID token against Google's JWKS.

4. **The callback then branches on whether it has seen this identity before.**

   * **Known identity, or a local account with the same verified email** — a full session is created. `302` to `return_to`, or to `FRONTEND_POST_LOGIN_URL`, with the refresh cookie set. The frontend then calls `POST {API_PREFIX}/auth/refresh` to obtain its first access token.
   * **New identity** — `302` to `FRONTEND_COMPLETION_URL?pending_token=<jwt>`.
     ```
     Location: http://localhost:3000/auth/complete?pending_token=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
     ```
   * **User cancelled** — `302` to `FRONTEND_AUTH_ERROR_URL?error=provider_denied`.
   * **Suspended account** — `302` to `FRONTEND_AUTH_ERROR_URL?error=account_suspended`.
   * **Missing `code` or `state`** — `302` to `FRONTEND_AUTH_ERROR_URL?error=invalid_request`.
   * **Bad, expired or replayed `state`** — `400 OAUTH_STATE_INVALID` as JSON, not a redirect. A forged state is not a user outcome to route somewhere. The full list of JSON failures on this route is in its own entry below; every one of them describes a broken or hostile request rather than something the person did.

5. **Completion.** `POST {API_PREFIX}/auth/oauth/complete` with the pending token, a **role**, and a display name. Returns `200` with an access token and sets the refresh cookie. This is where the account is created.

> **The callback issues NO session for a new identity, and this is not an
> oversight.** A Google profile carries a name and an email. It does not carry a
> **role**, and the entire trust model of this system turns on the
> CONSUMER/WEAVER distinction — a weaver registers items other people rely on.
> Role cannot be inferred from a provider, cannot be safely defaulted, and must
> never be taken from something the client controls. So there is nothing the
> callback could create an account *as*. The pending token is a ticket that says
> "this Google identity has been verified" and carries no authority at all: it is
> HS256 with a different key, its audience is `sutradhar/pending`, it holds no
> role and no user id, it is single-use, and it expires in 600 seconds. It cannot
> be used as a bearer token — the signature and the audience both refuse it.

The frontend's completion screen must therefore ask for a role before it can
finish sign-up. Only `CONSUMER` and `WEAVER` may be offered; anything else is
`ROLE_NOT_SELF_ASSIGNABLE` (403).

### 2.4 Account-linking rules

* **Resolution is by provider subject, never by email.** The provider's `sub` is immutable; the email attached to it is not. Google recycles addresses for deleted Workspace accounts, and resolving by email would hand the next holder of an address somebody else's account. A new person is a new subject whatever address they arrive with.
* **An unverified provider email is refused outright** with `PROVIDER_EMAIL_UNVERIFIED` (400). It does not fall through to account creation, and it cannot confirm whether a local account exists.
* **A first-time Google sign-in whose verified email matches an existing password account links to that account.** One person, one account — creating a second user here would leave one of them owning the person's items.
* **A provider never rewrites `users.email`.** A changed Google address is recorded on the OAuth identity only.
* **A second Google subject presenting an already-linked account's email is refused** with `OAUTH_IDENTITY_LINKED` (409). That is the recycled-address case, and linking would be a takeover.
* An OAuth-only account has no password. Attempting password login against it fails as `INVALID_CREDENTIALS`, identically to a wrong password.

### 2.5 Roles

| Role | Meaning |
|---|---|
| `CONSUMER` | Scans and reads. Carries no authority. The default. |
| `WEAVER` | Registers items, splits them, issues tags for their own work. |
| `COOP_OFFICER` | Attests to other people's items; issues tags in bulk. |
| `INSPECTOR` | Attests to other people's items. |
| `ADMIN` | Everything. Grants roles, manages the catalogue, flags actors. |

**Self-assignable at registration: `CONSUMER` and `WEAVER`, and nothing else.**
`COOP_OFFICER`, `INSPECTOR` and `ADMIN` must be granted by an existing admin.
Requesting one at registration or at OAuth completion is
`ROLE_NOT_SELF_ASSIGNABLE` (403).

A self-declared `WEAVER` is created with `status: PENDING_VERIFICATION`, not
`ACTIVE`. Claiming the role grants nothing until a human verifies it — and a
`PENDING_VERIFICATION` account's attestations do not raise anybody's trust level.
`CONSUMER` registrations are `ACTIVE` immediately.

`ADMIN` passes every `require_role` check implicitly.

Account statuses are `PENDING_VERIFICATION`, `ACTIVE`, `SUSPENDED`. A `SUSPENDED`
account's bearer token is rejected with `ACCOUNT_SUSPENDED` (401) before any route
body runs.

### 2.6 Provider unavailability

Absent `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` is **not an outage**. The
application boots, `/readyz` reports `google_oauth` as `unconfigured` rather than
`down`, `GET {API_PREFIX}/auth/oauth/providers` answers `200` with
`"enabled": false`, and `GET {API_PREFIX}/auth/oauth/google/start` answers `503`:

```json
{
  "error": {
    "code": "OAUTH_PROVIDER_UNAVAILABLE",
    "message": "google sign-in is not configured",
    "details": { "provider": "google" },
    "request_id": "a84bd54d5df444449fba3773b23bfc45"
  }
}
```

Drive the button off `/auth/oauth/providers`, and treat `503` on `/start` as the
same condition arriving late.

---

## 3. Endpoints

44 operations. Auth column values: `public` (no credential), `bearer` (any valid
access token), `bearer + role(X)` (that role or `ADMIN`), `pending-token` (the
OAuth completion ticket, in the body).

### 3.1 Probes

---

#### `GET /healthz`

**Auth:** public · **Rate limit:** none · **Idempotency:** n/a

**Request:** no parameters.

**Response 200**

```json
{ "status": "ok" }
```

Always `ok`. Touches no dependency.

**Errors:** none.

---

#### `GET /readyz`

**Auth:** public · **Rate limit:** none · **Idempotency:** n/a

**Request:** no parameters.

**Response 200 / 503** — the body is identical either way.

| Field | Type | Notes |
|---|---|---|
| `status` | string | `ok` \| `degraded` \| `down`, rolled up across checks. `unconfigured` never contributes. |
| `checks` | object | Keys `postgres`, `chain_rpc`, `anchoring`, `pinata`, `google_oauth`, `scheduler`. Each is `{"status": "ok"\|"unconfigured"\|"degraded"\|"down", "detail": string}`. |
| `unready` | string[] | Names of *required* checks that are down. Required set is exactly `["postgres"]`. |

**The status code is a routing decision, not a summary.** `503` only when
PostgreSQL is unreachable; `200` in every other case, however degraded. A chain
node having a bad afternoon must not take a working instance out of rotation.
Note in the example below that `status` is `down` and the response is still
`200`, because `unready` is empty.

```json
{
  "status": "down",
  "checks": {
    "postgres": { "status": "ok", "detail": "select 1 succeeded" },
    "chain_rpc": { "status": "down", "detail": "ConnectError: All connection attempts failed" },
    "anchoring": { "status": "degraded", "detail": "no relayer key; the outbox queues but never sends" },
    "pinata": { "status": "unconfigured", "detail": "PINATA_JWT absent; local mirror only" },
    "google_oauth": { "status": "unconfigured", "detail": "GOOGLE_CLIENT_ID/SECRET absent; Google sign-in disabled" },
    "scheduler": { "status": "down", "detail": "scheduler enabled but not running" }
  },
  "unready": []
}
```

**Errors:** `SERVICE_UNAVAILABLE` (503) — via the global handler when a database
connection cannot be opened at all.

---

### 3.2 Auth

---

#### `POST {API_PREFIX}/auth/register`

**Auth:** public · **Rate limit:** 10 / hour / IP · **Idempotency:** n/a
**Response headers:** `Cache-Control: no-store`

**Request body**

| Field | Type | Required | Constraints |
|---|---|---|---|
| `email` | string | yes | valid email address |
| `password` | string | yes | 1–256 characters |
| `display_name` | string | yes | 1–120 characters |
| `role` | string | no | `CONSUMER` or `WEAVER`. Absent means `CONSUMER`. |
| `region` | string \| null | no | ≤ 120 characters |
| `org_name` | string \| null | no | ≤ 200 characters |

Unknown fields are ignored.

**Response 201**

```json
{
  "id": "01a0485f-d0bd-7601-bdcd-368ca29b8543",
  "email": "weaver-9ecbc9bd@example.com",
  "display_name": "Kanubhai Patel",
  "role": "WEAVER",
  "status": "PENDING_VERIFICATION",
  "region": "Gujarat",
  "org_name": "Patan Weavers Co-operative",
  "created_at": "2026-08-28T12:37:14.044581Z",
  "last_login_at": null
}
```

No token and no cookie: registration does not log the caller in.

**Errors**

| Code | Status | Trigger |
|---|---|---|
| `VALIDATION_FAILED` | 422 | Body fails schema validation. |
| `ROLE_NOT_SELF_ASSIGNABLE` | 403 | `role` is `COOP_OFFICER`, `INSPECTOR` or `ADMIN`. |
| `EMAIL_ALREADY_REGISTERED` | 409 | An account already holds this email (case-insensitive). |
| `RATE_LIMITED` | 429 | More than 10 registrations from this IP in an hour. |
| `SERVICE_UNAVAILABLE` | 503 | Database unreachable. |
| `INTERNAL_ERROR` | 500 | Unhandled exception. |

---

#### `POST {API_PREFIX}/auth/login`

**Auth:** public · **Rate limit:** 5 / min / IP+email and 20 / min / IP · **Idempotency:** n/a
**Response headers:** `Cache-Control: no-store`, `Set-Cookie: sutradhar_rt=...`

**Request body**

| Field | Type | Required | Constraints |
|---|---|---|---|
| `email` | string | yes | valid email address |
| `password` | string | yes | 1–256 characters |

**Response 200**

```json
{
  "access_token": "eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIwMWEwNDg1Zi1kMGJkLTc2MDEtYmRjZC0zNjhjYTI5Yjg1NDMiLCJyb2xlIjoiV0VBVkVSIiwiaXNzIjoic3V0cmFkaGFyIiwiYXVkIjoic3V0cmFkaGFyL2FwaSIsImlhdCI6MTc4NzkyMDYzNCwiZXhwIjoxNzg3OTIxNTM0LCJqdGkiOiJhZmQ2ZjJlNWVlZjQ0MmE3YWE1NjQ2Zjg1ODdhYTYxYSIsInZlciI6MX0.P7FBHGAJvQRNuLoowh7bEYijVSoV8_b4wuS9w7DcCQ2nkaTLcD6zZR1_RNLH6XS1sUxJEbuC2VhWq9XfP5yFAg",
  "token_type": "bearer",
  "expires_in": 900,
  "user": {
    "id": "01a0485f-d0bd-7601-bdcd-368ca29b8543",
    "email": "weaver-9ecbc9bd@example.com",
    "display_name": "Kanubhai R. Patel",
    "role": "WEAVER",
    "status": "PENDING_VERIFICATION",
    "region": "Patan, Gujarat",
    "org_name": "Patan Weavers Co-operative",
    "created_at": "2026-08-28T12:37:14.044581Z",
    "last_login_at": "2026-08-28T12:37:14.268547Z"
  }
}
```

**Errors**

| Code | Status | Trigger |
|---|---|---|
| `VALIDATION_FAILED` | 422 | Body fails schema validation. |
| `INVALID_CREDENTIALS` | 401 | No such email, wrong password, or an OAuth-only account with no password. All three are indistinguishable, in wording and in timing. |
| `ACCOUNT_SUSPENDED` | 403 | The account is `SUSPENDED`. |
| `RATE_LIMITED` | 429 | Either login limiter exceeded. |
| `SERVICE_UNAVAILABLE` | 503 | Database unreachable. |
| `INTERNAL_ERROR` | 500 | Unhandled exception. |

---

#### `POST {API_PREFIX}/auth/refresh`

**Auth:** public (the refresh token is the credential) · **Rate limit:** 30 / min / user · **Idempotency:** n/a
**Response headers:** `Cache-Control: no-store`, `Set-Cookie: sutradhar_rt=...` (rotated)

**Request body**

| Field | Type | Required | Constraints |
|---|---|---|---|
| `refresh_token` | string \| null | no | Fallback for clients with no cookie jar. The cookie wins when both are present. |

**Response 200** — identical shape to `POST /auth/login`.

```json
{
  "access_token": "eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer",
  "expires_in": 900,
  "user": {
    "id": "01a0485f-d0bd-7601-bdcd-368ca29b8543",
    "email": "weaver-9ecbc9bd@example.com",
    "display_name": "Kanubhai R. Patel",
    "role": "WEAVER",
    "status": "PENDING_VERIFICATION",
    "region": "Patan, Gujarat",
    "org_name": "Patan Weavers Co-operative",
    "created_at": "2026-08-28T12:37:14.044581Z",
    "last_login_at": "2026-08-28T12:37:14.268547Z"
  }
}
```

**Errors**

| Code | Status | Trigger |
|---|---|---|
| `INVALID_REFRESH_TOKEN` | 401 | No token presented, or it matches no record. |
| `REFRESH_TOKEN_EXPIRED` | 401 | The token is past its 30-day life. |
| `REFRESH_TOKEN_REUSED` | 401 | An already-rotated token was presented. The entire family is revoked. |
| `RATE_LIMITED` | 429 | Limiter exceeded. |
| `SERVICE_UNAVAILABLE` | 503 | Database unreachable. |
| `INTERNAL_ERROR` | 500 | Unhandled exception. |

---

#### `POST {API_PREFIX}/auth/logout`

**Auth:** public (the cookie is the credential) · **Rate limit:** none · **Idempotency:** n/a
**Response headers:** `Cache-Control: no-store`, cookie cleared

**Request body**

| Field | Type | Required | Constraints |
|---|---|---|---|
| `refresh_token` | string \| null | no | Body fallback, as for `/refresh`. |

**Response 204** — no body.

Idempotent by design: an absent or unknown token is still `204`. The caller
wanted to be logged out, and afterwards they are.

**Errors:** `SERVICE_UNAVAILABLE` (503), `INTERNAL_ERROR` (500).

---

#### `POST {API_PREFIX}/auth/logout-all`

**Auth:** bearer · **Rate limit:** none · **Idempotency:** n/a
**Response headers:** `Cache-Control: no-store`, cookie cleared

**Request:** no body.

**Response 204** — no body. Every refresh family for this user is revoked.

**Errors:** `UNAUTHENTICATED` (401), `TOKEN_EXPIRED` (401), `TOKEN_INVALID` (401),
`ACCOUNT_SUSPENDED` (401), `SERVICE_UNAVAILABLE` (503), `INTERNAL_ERROR` (500).

---

#### `GET {API_PREFIX}/auth/me`

**Auth:** bearer · **Rate limit:** none · **Idempotency:** n/a
**Response headers:** `Cache-Control: no-store`

**Request:** no parameters.

**Response 200**

```json
{
  "id": "01a0485f-d0bd-7601-bdcd-368ca29b8543",
  "email": "weaver-9ecbc9bd@example.com",
  "display_name": "Kanubhai Patel",
  "role": "WEAVER",
  "status": "PENDING_VERIFICATION",
  "region": "Gujarat",
  "org_name": "Patan Weavers Co-operative",
  "created_at": "2026-08-28T12:37:14.044581Z",
  "last_login_at": "2026-08-28T12:37:14.268547Z"
}
```

**Errors**

| Code | Status | Trigger |
|---|---|---|
| `UNAUTHENTICATED` | 401 | No `Authorization` header, or a non-bearer scheme. |
| `TOKEN_EXPIRED` | 401 | The access token is past `exp`. |
| `TOKEN_INVALID` | 401 | Bad signature, wrong audience, wrong issuer, malformed, or the subject no longer exists. A pending token presented here lands under this code. |
| `ACCOUNT_SUSPENDED` | 401 | The account is `SUSPENDED`. |
| `SERVICE_UNAVAILABLE` | 503 | Database unreachable. |
| `INTERNAL_ERROR` | 500 | Unhandled exception. |

---

#### `PATCH {API_PREFIX}/auth/me`

**Auth:** bearer · **Rate limit:** none · **Idempotency:** n/a
**Response headers:** `Cache-Control: no-store`

**Request body** — exactly two mutable fields.

| Field | Type | Required | Constraints |
|---|---|---|---|
| `display_name` | string \| null | no | 1–120 characters |
| `region` | string \| null | no | ≤ 120 characters |

`role`, `status` and `email` are **absent from the schema**, not
optional-and-ignored. A body carrying them is accepted and those keys are
dropped; no code path could apply them. Escalation through this endpoint is
structurally impossible rather than defended against.

**Response 200** — the updated user, same shape as `GET /auth/me`.

```json
{
  "id": "01a0485f-d0bd-7601-bdcd-368ca29b8543",
  "email": "weaver-9ecbc9bd@example.com",
  "display_name": "Kanubhai R. Patel",
  "role": "WEAVER",
  "status": "PENDING_VERIFICATION",
  "region": "Patan, Gujarat",
  "org_name": "Patan Weavers Co-operative",
  "created_at": "2026-08-28T12:37:14.044581Z",
  "last_login_at": "2026-08-28T12:37:14.268547Z"
}
```

**Errors:** `VALIDATION_FAILED` (422), `UNAUTHENTICATED` (401),
`TOKEN_EXPIRED` (401), `TOKEN_INVALID` (401), `ACCOUNT_SUSPENDED` (401),
`SERVICE_UNAVAILABLE` (503), `INTERNAL_ERROR` (500).

---

### 3.3 OAuth

---

#### `GET {API_PREFIX}/auth/oauth/providers`

**Auth:** public · **Rate limit:** none · **Idempotency:** n/a
**Response headers:** `Cache-Control: no-store`

**Request:** no parameters.

**Response 200** — `{"data": [...]}`, no pagination. Always `200`, even when
nothing is configured: the frontend asks this to decide whether to draw a button,
and an error would be a worse answer than `enabled: false`.

| Field | Type | Notes |
|---|---|---|
| `data[].provider` | string | Always `google`. It is the only member. |
| `data[].enabled` | bool | True only when both client credentials are present. |

```json
{ "data": [ { "provider": "google", "enabled": true } ] }
```

Unconfigured:

```json
{ "data": [ { "provider": "google", "enabled": false } ] }
```

**Errors:** `SERVICE_UNAVAILABLE` (503), `INTERNAL_ERROR` (500).

---

#### `GET {API_PREFIX}/auth/oauth/google/start`

**Auth:** public · **Rate limit:** 10 / min / IP · **Idempotency:** n/a
**Response headers:** `Cache-Control: no-store`

**Request query**

| Parameter | Type | Required | Constraints |
|---|---|---|---|
| `return_to` | string \| null | no | Must be an allowlisted origin. Carried through the flow and used as the post-login destination. |

**Response 302** — no body. `Location` is Google's authorization endpoint:

```
Location: https://accounts.google.com/o/oauth2/v2/auth?client_id=test-client-id.apps.googleusercontent.com&redirect_uri=http%3A%2F%2Flocalhost%3A8000%2Fapi%2Fv1%2Fauth%2Foauth%2Fgoogle%2Fcallback&response_type=code&scope=openid+email+profile&state=eyJub25jZSI6Ik1RbGtEWkw1OUJiS19nRE96S0FzU01sYzgxbURRZV9sIiwidiI6InhGR1lpSDJ6aUZ6VVFqTmp3ZHZORTVwQW10QW1ud2xpUElORFRWdlVjYWsifQ.apF5dQ.4hglYM2HtcFjXQ4gDCSM5dEcLS8&code_challenge=zrLMEKeDeJjYIXUlY0Bv5HYw8Ivl7hkLQM2Mb1X1t64&code_challenge_method=S256&access_type=online&prompt=select_account
```

Navigate the browser to this endpoint; do not fetch it.

**Errors**

| Code | Status | Trigger |
|---|---|---|
| `VALIDATION_FAILED` | 422 | `return_to` is not an allowed origin. |
| `OAUTH_PROVIDER_UNAVAILABLE` | 503 | Google client credentials are absent. |
| `RATE_LIMITED` | 429 | More than 10 starts from this IP in a minute. |
| `SERVICE_UNAVAILABLE` | 503 | Database unreachable. |
| `INTERNAL_ERROR` | 500 | Unhandled exception. |

---

#### `GET {API_PREFIX}/auth/oauth/google/callback`

**Auth:** public (Google's redirect) · **Rate limit:** none · **Idempotency:** n/a
**Response headers:** `Cache-Control: no-store`; a refresh cookie on the session branch

**Request query**

| Parameter | Type | Required | Constraints |
|---|---|---|---|
| `code` | string \| null | no | Google's authorization code. Required for success. |
| `state` | string \| null | no | The signed state from `/start`. Required for success. |
| `error` | string \| null | no | Set by Google when the user refused. |

**Response 302** — never JSON on any user-reachable outcome. Destinations, in the
order the handler decides them:

| Condition | `Location` |
|---|---|
| `error` present | `{FRONTEND_AUTH_ERROR_URL}?error=provider_denied` |
| `code` or `state` missing | `{FRONTEND_AUTH_ERROR_URL}?error=invalid_request` |
| New identity | `{FRONTEND_COMPLETION_URL}?pending_token=<jwt>` (no cookie, no token) |
| Suspended account | `{FRONTEND_AUTH_ERROR_URL}?error=account_suspended` |
| Known or linkable identity | `return_to`, else `{FRONTEND_POST_LOGIN_URL}`; refresh cookie set |

New-identity example:

```
Location: http://localhost:3000/auth/complete?pending_token=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJqdGkiOiJmMjJjY2VmMC02OTkyLTQ4YTAtOTRhZC0wMmNhMzQ0NTljODkiLCJwcm92aWRlciI6IkdPT0dMRSIsInByb3ZpZGVyX3N1YmplY3QiOiIzZTU4ZGZkOTk2ZjA0NWViOTg1NGE1OGM4YTI5NWViYyIsInByb3ZpZGVyX2VtYWlsIjoiZy1kYjIzYTUzNUBnbWFpbC5leGFtcGxlLmNvbSIsImlzcyI6InN1dHJhZGhhciIsImF1ZCI6InN1dHJhZGhhci9wZW5kaW5nIiwiaWF0IjoxNzg3OTE4NzA5LCJleHAiOjE3ODc5MTkzMDl9.VGaTFV31lmmYebKwVJxiNksUU48Dv8Sd3kbUGYYZ3II
```

Refusal example:

```
Location: http://localhost:3000/auth/error?error=provider_denied
```

**Errors** — every JSON failure on this route. Anything not listed here is one
of the redirects above, not an error body:

| Code | Status | Trigger |
|---|---|---|
| `OAUTH_STATE_INVALID` | 400 | State is missing a signature, forged, expired past its max age, or its nonce has already been spent. |
| `PROVIDER_EMAIL_UNVERIFIED` | 400 | Google did not assert that the email is verified. |
| `OAUTH_IDENTITY_LINKED` | 409 | A different Google subject presented the verified email of an account already linked to Google. |
| `OAUTH_PROVIDER_UNAVAILABLE` | 503 | Google credentials absent, or the token exchange or JWKS fetch failed. |
| `SERVICE_UNAVAILABLE` | 503 | Database unreachable. |
| `INTERNAL_ERROR` | 500 | Unhandled exception. |

---

#### `POST {API_PREFIX}/auth/oauth/complete`

**Auth:** pending-token · **Rate limit:** 5 / min / pending-token `jti` · **Idempotency:** n/a
**Response headers:** `Cache-Control: no-store`, `Set-Cookie: sutradhar_rt=...`

**Request body** — unknown fields are ignored. **Email is deliberately absent:**
it comes from the verified provider identity, never from the client.

| Field | Type | Required | Constraints |
|---|---|---|---|
| `pending_token` | string | yes | ≥ 1 character. The token from the callback redirect. |
| `role` | string | yes | `CONSUMER` or `WEAVER`. Re-validated here because the pending token carries no role. |
| `display_name` | string | yes | 1–120 characters |
| `region` | string \| null | no | ≤ 120 characters |
| `org_name` | string \| null | no | ≤ 200 characters |

**Response 200** — same shape as login.

```json
{
  "access_token": "eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer",
  "expires_in": 900,
  "user": {
    "id": "01a04842-733b-7852-9c73-b444494295fd",
    "email": "g-db23a535@gmail.example.com",
    "display_name": "Meera Shah",
    "role": "CONSUMER",
    "status": "ACTIVE",
    "region": "Maharashtra",
    "org_name": null,
    "created_at": "2026-08-28T12:05:09.561385Z",
    "last_login_at": null
  }
}
```

**Errors**

| Code | Status | Trigger |
|---|---|---|
| `VALIDATION_FAILED` | 422 | Body fails schema validation, `role` is not a known role. |
| `TOKEN_EXPIRED` | 401 | The pending token is past its 600-second life. |
| `TOKEN_INVALID` | 401 | Bad signature, wrong audience, or malformed. |
| `PENDING_TOKEN_CONSUMED` | 401 | The token has already been spent. Exactly one of two concurrent completions wins. |
| `ROLE_NOT_SELF_ASSIGNABLE` | 403 | `role` is `COOP_OFFICER`, `INSPECTOR` or `ADMIN`. |
| `OAUTH_IDENTITY_LINKED` | 409 | A second identity linked this provider subject between mint and burn. |
| `EMAIL_ALREADY_REGISTERED` | 409 | A local account already holds the provider email. |
| `RATE_LIMITED` | 429 | More than 5 attempts on one pending token in a minute. |
| `SERVICE_UNAVAILABLE` | 503 | Database unreachable. |
| `INTERNAL_ERROR` | 500 | Unhandled exception. |

---

### 3.4 Categories

The GI catalogue is **public reference data**. A Geographical Indication is
published by the GI registry, and the frontend renders a category page before
anybody signs in. Writes are admin-only and live under `/admin/categories`.

---

#### `GET {API_PREFIX}/categories`

**Auth:** public · **Rate limit:** none · **Idempotency:** n/a

**Request query**

| Parameter | Type | Required | Default | Constraints |
|---|---|---|---|---|
| `limit` | int | no | 20 | ≤ 100 |
| `offset` | int | no | 0 | ≥ 0 |
| `include_inactive` | bool | no | false | Include retired categories. |

**Response 200** — collection envelope with **offset** pagination, ordered by slug.

```json
{
  "data": [
    {
      "id": "01a04842-73a9-7e20-a259-ed2d8c1f0bbd",
      "slug": "patola-silk",
      "display_name": "Patan Patola Silk",
      "is_textile": true,
      "quantity_unit": "metre",
      "schema_version": 1,
      "is_active": true,
      "created_at": "2026-08-28T12:05:09.671118Z"
    },
    {
      "id": "01a04842-73ad-75b3-a1b3-8eab62bd398f",
      "slug": "sambalpuri-bandha",
      "display_name": "Sambalpuri Bandha",
      "is_textile": true,
      "quantity_unit": "metre",
      "schema_version": 1,
      "is_active": true,
      "created_at": "2026-08-28T12:05:09.671118Z"
    },
    {
      "id": "01a04842-739a-7343-b261-70e4526203a7",
      "slug": "kolhapuri-chappal",
      "display_name": "Kolhapuri Chappal",
      "is_textile": false,
      "quantity_unit": "pair",
      "schema_version": 1,
      "is_active": true,
      "created_at": "2026-08-28T12:05:09.654545Z"
    }
  ],
  "pagination": { "next_offset": null, "limit": 20 }
}
```

Summaries carry no `attribute_schema`. Fetch one category for that.

**Errors:** `VALIDATION_FAILED` (422), `SERVICE_UNAVAILABLE` (503),
`INTERNAL_ERROR` (500).

---

#### `GET {API_PREFIX}/categories/{slug}`

**Auth:** public · **Rate limit:** none · **Idempotency:** n/a

**Request path:** `slug` — lowercase hyphenated, e.g. `patola-silk`.

**Response 200** — the **latest active** version, with its JSON Schema.

| Field | Type | Notes |
|---|---|---|
| `id` | uuid | |
| `slug` | string | |
| `display_name` | string | |
| `is_textile` | bool | Kolhapuri chappals are a GI and are not a textile. |
| `quantity_unit` | string | The only unit items in this category may use. |
| `schema_version` | int | |
| `is_active` | bool | |
| `created_at` | datetime | |
| `attribute_schema` | object | JSON Schema Draft 2020-12, `additionalProperties: false`. |

```json
{
  "id": "01a04842-73a9-7e20-a259-ed2d8c1f0bbd",
  "slug": "patola-silk",
  "display_name": "Patan Patola Silk",
  "is_textile": true,
  "quantity_unit": "metre",
  "schema_version": 1,
  "is_active": true,
  "created_at": "2026-08-28T12:05:09.671118Z",
  "attribute_schema": {
    "$id": "https://sutradhar.local/schemas/patola-silk/1",
    "type": "object",
    "title": "Patan Patola Silk",
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "required": ["warp_count", "weft_count", "dye_type", "double_ikat", "loom_type", "weave_days", "gi_registration_no"],
    "properties": {
      "dye_type": { "enum": ["natural", "synthetic"], "type": "string" },
      "loom_type": { "enum": ["pit", "frame"], "type": "string" },
      "warp_count": { "type": "integer", "maximum": 10000, "minimum": 1, "description": "Warp threads per inch." }
    }
  }
}
```

Drive a registration form off `attribute_schema`. Because it sets
`additionalProperties: false`, a field the form invents is rejected.

**Errors**

| Code | Status | Trigger |
|---|---|---|
| `CATEGORY_NOT_FOUND` | 404 | No such slug, **or the category has been retired** (`is_active: false`). |
| `SERVICE_UNAVAILABLE` | 503 | Database unreachable. |
| `INTERNAL_ERROR` | 500 | Unhandled exception. |

```json
{
  "error": {
    "code": "CATEGORY_NOT_FOUND",
    "message": "no category with slug 'banarasi-brocade'",
    "details": null,
    "request_id": "8735eb60ec1144b893b9be22d9eac752"
  }
}
```

---

#### `GET {API_PREFIX}/categories/{slug}/versions`

**Auth:** public · **Rate limit:** none · **Idempotency:** n/a

**Request path:** `slug`.

**Response 200** — **not** the standard collection envelope: `{"slug": ..., "data": [...]}`,
oldest first, no pagination. Entries are summaries without schemas.

```json
{
  "slug": "patola-silk",
  "data": [
    {
      "id": "01a0485f-da04-7953-ada5-c7867a47d45f",
      "slug": "patola-silk",
      "display_name": "Patan Patola Silk",
      "is_textile": true,
      "quantity_unit": "metre",
      "schema_version": 1,
      "is_active": true,
      "created_at": "2026-08-28T12:37:16.413901Z"
    }
  ]
}
```

**Errors:** `CATEGORY_NOT_FOUND` (404), `SERVICE_UNAVAILABLE` (503),
`INTERNAL_ERROR` (500).

---

#### `GET {API_PREFIX}/categories/{slug}/v/{version}`

**Auth:** public · **Rate limit:** none · **Idempotency:** n/a

**Request path:** `slug`; `version` — integer.

**Response 200** — identical shape to `GET /categories/{slug}`.

**Retired versions still resolve here, and that is the point.** Existing items are
pinned to the version they were registered under, and a verification page that
404'd because a category was later retired would be a broken provenance record.
Use this endpoint when rendering an item whose `category_schema_version` is not
the current one.

**Errors**

| Code | Status | Trigger |
|---|---|---|
| `CATEGORY_NOT_FOUND` | 404 | No such slug at any version. |
| `CATEGORY_VERSION_NOT_FOUND` | 404 | The slug exists but not at that version. |
| `SERVICE_UNAVAILABLE` | 503 | Database unreachable. |
| `INTERNAL_ERROR` | 500 | Unhandled exception. |

---

#### `POST {API_PREFIX}/categories/{slug}/validate`

**Auth:** public · **Rate limit:** 60 / min / IP · **Idempotency:** n/a

Dry-run an attribute payload against a category's schema. **Writes nothing.**
Rate limited unlike the reads beside it, because it is the only unauthenticated
endpoint in the system that does real work on a caller-supplied body.

**Request path:** `slug`. **Request body:**

| Field | Type | Required | Constraints |
|---|---|---|---|
| `attributes` | object | yes | The candidate attribute payload. |
| `schema_version` | int \| null | no | Validate against a pinned version instead of the latest. |

**Response 200**

```json
{ "valid": true, "slug": "patola-silk", "schema_version": 1 }
```

`valid` is always `true` on a `200` — a failure is an error response, not
`valid: false`.

**Errors**

| Code | Status | Trigger |
|---|---|---|
| `ATTRIBUTE_VALIDATION_FAILED` | 422 | The attributes do not satisfy the schema. `details.errors` is a list of `{path, message}`. |
| `CATEGORY_NOT_FOUND` | 404 | No such slug. |
| `CATEGORY_VERSION_NOT_FOUND` | 404 | `schema_version` names a version that does not exist. |
| `CATEGORY_RETIRED` | 422 | The category is inactive and no version was pinned. |
| `RATE_LIMITED` | 429 | More than 60 validations from this IP in a minute. |
| `SERVICE_UNAVAILABLE` | 503 | Database unreachable. |
| `INTERNAL_ERROR` | 500 | Unhandled exception. |

```json
{
  "error": {
    "code": "ATTRIBUTE_VALIDATION_FAILED",
    "message": "attributes do not satisfy this category's schema",
    "details": {
      "errors": [
        { "path": "/weft_count", "message": "'weft_count' is required" },
        { "path": "/dye_type", "message": "'dye_type' is required" },
        { "path": "/warp_count", "message": "'one hundred' is not of type 'integer'" }
      ]
    },
    "request_id": "8950997c642e4e8382f85534cc127195"
  }
}
```

`details.errors[].path` is a JSON Pointer into the submitted `attributes` object.
Map it onto the form field to show the message in place.

---

### 3.5 Categories — admin

---

#### `POST {API_PREFIX}/admin/categories`

**Auth:** bearer + role(ADMIN) · **Rate limit:** none · **Idempotency:** optional

Creates a category at version 1, usable on the very next request — the registry
is reloaded inside the request that caused the change, with no restart.

**Request body**

| Field | Type | Required | Constraints |
|---|---|---|---|
| `slug` | string | yes | 2–64 chars, `^[a-z0-9]+(-[a-z0-9]+)*$` |
| `display_name` | string | yes | 1–120 characters |
| `is_textile` | bool | no | Defaults to `true`. |
| `quantity_unit` | string | yes | 1–32 characters |
| `attribute_schema` | object | yes | A valid JSON Schema Draft 2020-12 document. |

`schema_version` is absent on purpose: v1 is implied, and a caller-chosen
starting version would allow gaps that make "the previous version" ambiguous.

**Response 201** — a category detail object.

```json
{
  "id": "01a04842-7663-7eb2-9440-5773a15138d2",
  "slug": "banarasi-brocade",
  "display_name": "Banarasi Brocade",
  "is_textile": true,
  "quantity_unit": "metre",
  "schema_version": 1,
  "is_active": true,
  "created_at": "2026-08-28T12:05:10.371003Z",
  "attribute_schema": {
    "type": "object",
    "additionalProperties": false,
    "required": ["zari_type"],
    "properties": {
      "zari_type": { "type": "string", "enum": ["real", "tested"] },
      "loom_type": { "type": "string" }
    }
  }
}
```

**Errors**

| Code | Status | Trigger |
|---|---|---|
| `VALIDATION_FAILED` | 422 | Body fails schema validation, including the slug pattern. |
| `INVALID_CATEGORY_SCHEMA` | 422 | `attribute_schema` is not a valid Draft 2020-12 document. `details.errors` is a list of `{path, message}`. |
| `CATEGORY_SLUG_EXISTS` | 409 | A category already holds this slug. |
| `IDEMPOTENCY_KEY_REUSED` | 409 | The supplied key was used for a different body. |
| `UNAUTHENTICATED` / `TOKEN_EXPIRED` / `TOKEN_INVALID` / `ACCOUNT_SUSPENDED` | 401 | Credential problems. |
| `INSUFFICIENT_ROLE` | 403 | Caller is not `ADMIN`. |
| `SERVICE_UNAVAILABLE` | 503 | Database unreachable. |
| `INTERNAL_ERROR` | 500 | Unhandled exception. |

```json
{
  "error": {
    "code": "CATEGORY_SLUG_EXISTS",
    "message": "category 'banarasi-brocade' already exists; publish a new version instead",
    "details": { "slug": "banarasi-brocade" },
    "request_id": "75ad7723e8e949498131726a61f52f57"
  }
}
```

---

#### `POST {API_PREFIX}/admin/categories/{slug}/versions`

**Auth:** bearer + role(ADMIN) · **Rate limit:** none · **Idempotency:** optional

Publishes v(n+1) and reports what it changes. **Existing items are unaffected** —
each is pinned to the version it was registered under.

**Request path:** `slug`. **Request body:**

| Field | Type | Required | Constraints |
|---|---|---|---|
| `attribute_schema` | object | yes | A valid JSON Schema Draft 2020-12 document. Only the schema changes. |

**Response 200**

| Field | Type | Notes |
|---|---|---|
| `category` | object | The new version, as a category detail. |
| `diff.added` | string[] | Property names present now and not before. |
| `diff.removed` | string[] | Property names gone. |
| `diff.type_changed` | object[] | `{property, from, to}` entries. |
| `diff.newly_required` | string[] | Names newly in `required`. |
| `diff.no_longer_required` | string[] | Names dropped from `required`. |
| `breaking` | bool | True when `removed`, `type_changed` or `newly_required` is non-empty — i.e. an item valid under the old version might now fail. |

```json
{
  "category": {
    "id": "01a04842-7663-7eb2-9440-5773a15138d2",
    "slug": "banarasi-brocade",
    "display_name": "Banarasi Brocade",
    "is_textile": true,
    "quantity_unit": "metre",
    "schema_version": 2,
    "is_active": true,
    "created_at": "2026-08-28T12:05:10.371003Z",
    "attribute_schema": {
      "type": "object",
      "additionalProperties": false,
      "required": ["zari_type", "motif"],
      "properties": {
        "zari_type": { "type": "string", "enum": ["real", "tested"] },
        "motif": { "type": "string" }
      }
    }
  },
  "diff": {
    "added": ["motif"],
    "removed": ["loom_type"],
    "type_changed": [],
    "newly_required": ["motif"],
    "no_longer_required": []
  },
  "breaking": true
}
```

**Errors:** `VALIDATION_FAILED` (422), `INVALID_CATEGORY_SCHEMA` (422),
`CATEGORY_NOT_FOUND` (404), `IDEMPOTENCY_KEY_REUSED` (409),
`UNAUTHENTICATED` / `TOKEN_EXPIRED` / `TOKEN_INVALID` / `ACCOUNT_SUSPENDED` (401),
`INSUFFICIENT_ROLE` (403), `SERVICE_UNAVAILABLE` (503), `INTERNAL_ERROR` (500).

---

#### `PATCH {API_PREFIX}/admin/categories/{slug}`

**Auth:** bearer + role(ADMIN) · **Rate limit:** none · **Idempotency:** n/a

**Request path:** `slug`. **Request body:**

| Field | Type | Required | Constraints |
|---|---|---|---|
| `display_name` | string \| null | no | 1–120 characters |
| `is_active` | bool \| null | no | Setting `false` retires the category. |

`slug` and `attribute_schema` are **absent from the schema**, not optional. A
slug is referenced by URLs and printed tags; a schema change is a new version by
definition. Neither is reachable through any shape of request body.

**Response 200** — the updated category detail.

```json
{
  "id": "01a04842-7663-7eb2-9440-5773a15138d2",
  "slug": "banarasi-brocade",
  "display_name": "Banarasi Brocade (GI)",
  "is_textile": true,
  "quantity_unit": "metre",
  "schema_version": 2,
  "is_active": false,
  "created_at": "2026-08-28T12:05:10.371003Z",
  "attribute_schema": { "type": "object", "additionalProperties": false, "required": ["zari_type", "motif"], "properties": { "zari_type": { "type": "string", "enum": ["real", "tested"] }, "motif": { "type": "string" } } }
}
```

Retiring a category makes `GET /categories/{slug}` answer `CATEGORY_NOT_FOUND`
and blocks new registrations with `CATEGORY_RETIRED`. Pinned-version reads keep
working.

**Errors:** `VALIDATION_FAILED` (422), `CATEGORY_NOT_FOUND` (404),
`UNAUTHENTICATED` / `TOKEN_EXPIRED` / `TOKEN_INVALID` / `ACCOUNT_SUSPENDED` (401),
`INSUFFICIENT_ROLE` (403), `SERVICE_UNAVAILABLE` (503), `INTERNAL_ERROR` (500).

---

### 3.6 Items

---

#### `POST {API_PREFIX}/items`

**Auth:** bearer + role(WEAVER, COOP_OFFICER) · **Rate limit:** none · **Idempotency:** required

Registers an item. The item row, its `REGISTERED` event and its outbox job commit
together or not at all.

**Request body**

| Field | Type | Required | Constraints |
|---|---|---|---|
| `category_slug` | string | yes | 1–64 characters. Must name an active category. |
| `attributes` | object | yes | Must satisfy that category's current JSON Schema. |
| `quantity` | string (decimal) | yes | Greater than 0. Quantised to 4dp. |
| `quantity_unit` | string | yes | 1–32 characters. Must equal the category's `quantity_unit`. |
| `parent_id` | uuid \| null | no | Register directly under an existing item. |

**Response 201**

| Field | Type | Notes |
|---|---|---|
| `id` | uuid | |
| `category_id` | uuid | |
| `category_schema_version` | int | The version this item is pinned to, forever. |
| `parent_id` | uuid \| null | |
| `registered_by` | uuid | |
| `quantity` | string | 4dp |
| `quantity_unit` | string | |
| `item_hash` | string | `0x`-prefixed keccak256 of the frozen preimage. |
| `tag_code` | string \| null | `null` until a tag is issued. |
| `status` | string | `PENDING` \| `CONFIRMED` \| `FAILED`. See §5.1. |
| `dispute_status` | string | `NONE` \| `DISPUTED` |
| `created_at` | datetime | |

```json
{
  "id": "01a0483f-adf5-74d3-85f1-56a259a90e59",
  "category_id": "01a0483f-aacf-7783-b0a8-e6a01ce3f941",
  "category_schema_version": 1,
  "parent_id": null,
  "registered_by": "01a0483f-a098-7250-be10-6c204077e5f8",
  "quantity": "12.0000",
  "quantity_unit": "metre",
  "item_hash": "0xc8fce323271190ee3926bfa832179e9e49b1f3e42c4e891278d051056af52ef8",
  "tag_code": null,
  "status": "PENDING",
  "dispute_status": "NONE",
  "created_at": "2026-08-28T12:02:07.988707Z"
}
```

**Errors**

| Code | Status | Trigger |
|---|---|---|
| `VALIDATION_FAILED` | 422 | Body fails schema validation, or `Idempotency-Key` is absent. |
| `ATTRIBUTE_VALIDATION_FAILED` | 422 | Attributes do not satisfy the category schema. `details.errors` as for `/validate`. |
| `QUANTITY_UNIT_MISMATCH` | 422 | `quantity_unit` is not the category's unit. `details` carries `expected` and `received`. |
| `MAX_DEPTH_EXCEEDED` | 422 | `parent_id` would put the item deeper than 5 levels. |
| `CATEGORY_NOT_FOUND` | 404 | No such `category_slug`. |
| `CATEGORY_RETIRED` | 422 | The category is inactive. |
| `ITEM_NOT_FOUND` | 404 | `parent_id` names no item. |
| `MASS_BALANCE_EXCEEDED` | 409 | The parent has less unallocated quantity than requested. |
| `IDEMPOTENCY_KEY_REUSED` | 409 | The key was used for a different body. |
| `UNAUTHENTICATED` / `TOKEN_EXPIRED` / `TOKEN_INVALID` / `ACCOUNT_SUSPENDED` | 401 | Credential problems. |
| `INSUFFICIENT_ROLE` | 403 | Caller is `CONSUMER` or `INSPECTOR`. |
| `SERVICE_UNAVAILABLE` | 503 | Database unreachable. |
| `INTERNAL_ERROR` | 500 | Unhandled exception. |

```json
{
  "error": {
    "code": "QUANTITY_UNIT_MISMATCH",
    "message": "category 'patola-silk' is measured in metre, not kilogram",
    "details": { "expected": "metre", "received": "kilogram" },
    "request_id": "70c3cca50ce54af08931925012cd9a18"
  }
}
```

---

#### `POST {API_PREFIX}/items/{item_id}/split`

**Auth:** bearer + role(WEAVER, COOP_OFFICER) · **Rate limit:** none · **Idempotency:** required

Cuts an item into children. Mass balance is enforced across all children at once,
under a row lock, so two concurrent splits cannot both succeed against the same
remaining quantity.

**Request path:** `item_id` — uuid. **Request body:**

| Field | Type | Required | Constraints |
|---|---|---|---|
| `children` | array | yes | 1–50 entries |
| `children[].attributes` | object | yes | Validated against the parent's pinned category version. |
| `children[].quantity` | string (decimal) | yes | Greater than 0, quantised to 4dp. |
| `children[].quantity_unit` | string \| null | no | Defaults to the parent's unit. |

**Response 200**

| Field | Type | Notes |
|---|---|---|
| `parent_id` | uuid | |
| `children` | object[] | Item summaries, same shape as `POST /items`. |
| `parent_quantity` | string | The parent's own quantity, 4dp. |
| `allocated` | string | Total allocated to children after this split. |
| `remaining` | string | `parent_quantity - allocated`. |
| `max_depth` | int | `5`. The tree depth ceiling, echoed so a client can render it. |

```json
{
  "parent_id": "01a04842-76c0-72c1-b417-0be953c395e1",
  "children": [
    {
      "id": "01a04842-7809-72d3-9197-003dfc1a4bdb",
      "category_id": "01a04842-73a9-7e20-a259-ed2d8c1f0bbd",
      "category_schema_version": 1,
      "parent_id": "01a04842-76c0-72c1-b417-0be953c395e1",
      "registered_by": "01a04842-6ae0-7ab1-a0ff-f252643a7323",
      "quantity": "5.5000",
      "quantity_unit": "metre",
      "item_hash": "0xdf1080eb948807ad5f4a5f09c53c2625ea8d92d474de95ca9b3397e4990184bb",
      "tag_code": null,
      "status": "PENDING",
      "dispute_status": "NONE",
      "created_at": "2026-08-28T12:05:10.792505Z"
    },
    {
      "id": "01a04842-780d-78e2-9a4d-1f5ecc3b3e04",
      "category_id": "01a04842-73a9-7e20-a259-ed2d8c1f0bbd",
      "category_schema_version": 1,
      "parent_id": "01a04842-76c0-72c1-b417-0be953c395e1",
      "registered_by": "01a04842-6ae0-7ab1-a0ff-f252643a7323",
      "quantity": "6.0000",
      "quantity_unit": "metre",
      "item_hash": "0x74cefdfadb90b1511337b82cc2b7e528262326ca7ef4c603b461177f947b670a",
      "tag_code": null,
      "status": "PENDING",
      "dispute_status": "NONE",
      "created_at": "2026-08-28T12:05:10.797209Z"
    }
  ],
  "parent_quantity": "12.0000",
  "allocated": "11.5000",
  "remaining": "0.5000",
  "max_depth": 5
}
```

**Errors:** as for `POST /items`, plus `ITEM_NOT_FOUND` (404) for the parent.
`MASS_BALANCE_EXCEEDED` (409) carries `parent_id`, `parent_quantity`,
`already_allocated`, `remaining` and `requested` in `details` — see §1.3 for the
full body.

---

#### `GET {API_PREFIX}/items`

**Auth:** bearer · **Rate limit:** none · **Idempotency:** n/a

**Request query**

| Parameter | Type | Required | Default | Constraints |
|---|---|---|---|---|
| `category_slug` | string \| null | no | — | An unknown slug returns an empty page rather than a 404. |
| `status` | string \| null | no | — | `PENDING` \| `CONFIRMED` \| `FAILED` |
| `registered_by` | uuid \| null | no | — | |
| `cursor` | string \| null | no | — | Opaque `next_cursor` from a previous page. |
| `limit` | int | no | 20 | ≤ 100 |

**Response 200** — collection envelope with a **keyset cursor**, newest first.
See §1.2 for the full body; `pagination` is `{"next_cursor": null, "limit": 20}`
at the end of the collection and carries an opaque string otherwise:

```json
{
  "pagination": {
    "limit": 1,
    "next_cursor": "mVIM3I4gDWH0dztq1gmvq3siaWQiOiIwMWEwNDgzZi1hZjU1LTcyNjAtOTA0Yi0xYTAzMzZkODAyOTAiLCJrIjoiMjAyNi0wOC0yOFQxMjowMjowOC4zMzkxNjlaIn0"
  }
}
```

**Errors:** `VALIDATION_FAILED` (422), `INVALID_CURSOR` (422),
`UNAUTHENTICATED` / `TOKEN_EXPIRED` / `TOKEN_INVALID` / `ACCOUNT_SUSPENDED` (401),
`SERVICE_UNAVAILABLE` (503), `INTERNAL_ERROR` (500).

---

#### `GET {API_PREFIX}/items/{item_id}`

**Auth:** bearer · **Rate limit:** none · **Idempotency:** n/a

**Request path:** `item_id` — uuid.

**Response 200** — every field of the item summary, plus:

| Field | Type | Notes |
|---|---|---|
| `category_slug` | string | |
| `attributes` | object | **Full** attributes. Unlike the public view, nothing is withheld. |
| `remaining_quantity` | string | Unallocated quantity after children, 4dp. |
| `ancestry` | object[] | Root first; **the item itself is excluded**. |
| `children` | object[] | Direct children only. |
| `chain` | object | `{status, anchored, tx_hash, block_number, confirmations}` |

Tree nodes in `ancestry` and `children` are
`{id, parent_id, depth, quantity, quantity_unit, item_hash, tag_code, status}`.

```json
{
  "id": "01a0483f-adf5-74d3-85f1-56a259a90e59",
  "category_id": "01a0483f-aacf-7783-b0a8-e6a01ce3f941",
  "category_schema_version": 1,
  "category_slug": "patola-silk",
  "parent_id": null,
  "registered_by": "01a0483f-a098-7250-be10-6c204077e5f8",
  "quantity": "12.0000",
  "quantity_unit": "metre",
  "item_hash": "0xc8fce323271190ee3926bfa832179e9e49b1f3e42c4e891278d051056af52ef8",
  "tag_code": null,
  "status": "PENDING",
  "dispute_status": "NONE",
  "created_at": "2026-08-28T12:02:07.988707Z",
  "attributes": {
    "double_ikat": true,
    "dye_type": "natural",
    "gi_registration_no": "GI-00232",
    "loom_type": "pit",
    "warp_count": 120,
    "weave_days": 210,
    "weft_count": 116
  },
  "remaining_quantity": "0.5000",
  "ancestry": [],
  "children": [
    {
      "id": "01a0483f-af51-70f1-a230-308b08184481",
      "parent_id": "01a0483f-adf5-74d3-85f1-56a259a90e59",
      "depth": 2,
      "quantity": "5.5000",
      "quantity_unit": "metre",
      "item_hash": "0x6a467b0c14908608b289115482fd09948d86a164225c652bdf648a0d2bbba300",
      "tag_code": null,
      "status": "PENDING"
    },
    {
      "id": "01a0483f-af55-7260-904b-1a0336d80290",
      "parent_id": "01a0483f-adf5-74d3-85f1-56a259a90e59",
      "depth": 2,
      "quantity": "6.0000",
      "quantity_unit": "metre",
      "item_hash": "0xc311f50ba5e9fa55034e986b93b23fef5b4ed838685dbc744ffbeb4749ef5485",
      "tag_code": null,
      "status": "PENDING"
    }
  ],
  "chain": {
    "status": "PENDING",
    "anchored": false,
    "tx_hash": null,
    "block_number": null,
    "confirmations": 0
  }
}
```

**Errors:** `ITEM_NOT_FOUND` (404),
`UNAUTHENTICATED` / `TOKEN_EXPIRED` / `TOKEN_INVALID` / `ACCOUNT_SUSPENDED` (401),
`SERVICE_UNAVAILABLE` (503), `INTERNAL_ERROR` (500).

---

#### `GET {API_PREFIX}/items/{item_id}/tree`

**Auth:** bearer · **Rate limit:** none · **Idempotency:** n/a

**Request path:** `item_id` — uuid.

**Response 200** — a **bare JSON array**, not a collection envelope. The full
subtree including the item itself, depth-annotated, from one recursive query.

```json
[
  {
    "id": "01a04861-a84b-7b11-98c1-a4c84c9938aa",
    "parent_id": null,
    "depth": 1,
    "quantity": "12.0000",
    "quantity_unit": "metre",
    "item_hash": "0xee662780c61cb1f5ff8680a89c46e3b3d0523c768dc5728b50f07bae07dcdc97",
    "tag_code": null,
    "status": "PENDING"
  },
  {
    "id": "01a04861-a926-7131-a1a9-ab8a447dd2b6",
    "parent_id": "01a04861-a84b-7b11-98c1-a4c84c9938aa",
    "depth": 2,
    "quantity": "5.5000",
    "quantity_unit": "metre",
    "item_hash": "0x72c615e9a9d7f9c0da50e0052d053037542c8f263eca865998ec978c5e52d5d4",
    "tag_code": null,
    "status": "PENDING"
  }
]
```

`depth` is absolute within the whole tree, not relative to the requested item.

**Errors:** `ITEM_NOT_FOUND` (404),
`UNAUTHENTICATED` / `TOKEN_EXPIRED` / `TOKEN_INVALID` / `ACCOUNT_SUSPENDED` (401),
`SERVICE_UNAVAILABLE` (503), `INTERNAL_ERROR` (500).

---

#### `GET {API_PREFIX}/items/{item_id}/events`

**Auth:** bearer · **Rate limit:** none · **Idempotency:** n/a

**Request path:** `item_id` — uuid. **Request query:**

| Parameter | Type | Required | Default | Constraints |
|---|---|---|---|---|
| `limit` | int | no | 50 | ≤ 100 |
| `offset` | int | no | 0 | ≥ 0 |

**Response 200** — collection envelope with **offset** pagination, **oldest
first**. Each event is
`{id, item_id, event_type, actor_id, payload, payload_hash, created_at}`.

`event_type` is one of `REGISTERED`, `SPLIT`, `ATTESTED`, `ANCHORED`, `DISPUTED`,
`CLAIMED`, `TAG_ISSUED`, `REORGED`, `ANCHOR_FAILED`, `DISPUTE_CLEARED`.

`payload` is free-form per event type and **is not part of this contract**. Do
not branch on its keys; the shape shown is illustrative.

```json
{
  "data": [
    {
      "id": "01a04861-a865-70a3-9ef2-965f2b8db29f",
      "item_id": "01a04861-a84b-7b11-98c1-a4c84c9938aa",
      "event_type": "REGISTERED",
      "actor_id": "01a04861-a77d-77f0-b757-22ed02c1bff5",
      "payload": {
        "preimage": {
          "v": 1,
          "item_id": "01a04861-a84b-7b11-98c1-a4c84c9938aa",
          "quantity": "12.0000",
          "parent_id": null,
          "attributes": { "dye_type": "natural", "loom_type": "pit", "warp_count": 120, "weave_days": 210, "weft_count": 116, "double_ikat": true, "gi_registration_no": "GI-00232" },
          "category_slug": "patola-silk",
          "quantity_unit": "metre",
          "registered_at": "2026-08-28T12:39:14.761862Z",
          "registered_by_hash": "0xcbcd2c620b1b4a69f0e2be0046d23667b94981a727dd36c98f0518a18ebc0058",
          "category_schema_version": 1
        },
        "item_hash": "0xee662780c61cb1f5ff8680a89c46e3b3d0523c768dc5728b50f07bae07dcdc97"
      },
      "payload_hash": "0x5dc318aa4ad840077f21096ea1888eea6f5eb44777b33fe2ff8d7172eeb300b4",
      "created_at": "2026-08-28T12:39:14.780102Z"
    },
    {
      "id": "01a04861-a92c-7152-9189-d78cb455fafb",
      "item_id": "01a04861-a84b-7b11-98c1-a4c84c9938aa",
      "event_type": "SPLIT",
      "actor_id": "01a04861-a77d-77f0-b757-22ed02c1bff5",
      "payload": {
        "children": [
          { "item_id": "01a04861-a926-7131-a1a9-ab8a447dd2b6", "quantity": "5.5000", "item_hash": "0x72c615e9a9d7f9c0da50e0052d053037542c8f263eca865998ec978c5e52d5d4" },
          { "item_id": "01a04861-a927-7b40-9453-2eabbfd208b4", "quantity": "6.0000", "item_hash": "0x4181a60e4bab57bfa3f930393536ca5d587c44f5c9b7b88f89eee60e4243a622" }
        ],
        "parent_hash": "0xee662780c61cb1f5ff8680a89c46e3b3d0523c768dc5728b50f07bae07dcdc97"
      },
      "payload_hash": "0xb037ec937d5ab1548c190d2c72c4cb8b1aa71c1ef4395e959d90142c6f6b5023",
      "created_at": "2026-08-28T12:39:14.977076Z"
    }
  ],
  "pagination": { "next_offset": null, "limit": 50 }
}
```

**Errors:** `VALIDATION_FAILED` (422), `ITEM_NOT_FOUND` (404),
`UNAUTHENTICATED` / `TOKEN_EXPIRED` / `TOKEN_INVALID` / `ACCOUNT_SUSPENDED` (401),
`SERVICE_UNAVAILABLE` (503), `INTERNAL_ERROR` (500).

---

### 3.7 Attestations and trust

---

#### `POST {API_PREFIX}/items/{item_id}/attestations`

**Auth:** bearer + role(WEAVER, COOP_OFFICER, INSPECTOR) · **Rate limit:** none · **Idempotency:** n/a

One attestation per actor per item, enforced by a database constraint rather than
a check-then-write.

**Request path:** `item_id` — uuid. **Request body** — unknown fields are
**rejected** (`extra: forbid`), unlike most other endpoints here.

| Field | Type | Required | Constraints |
|---|---|---|---|
| `statement` | object | yes | Non-empty. At most 50 keys and 16384 bytes of JSON. Otherwise free-form. |

The statement is deliberately unvalidated beyond those bounds: an inspector's
notes, a co-op's ledger reference and a weaver's loom details have nothing in
common, and one schema over all three would either exclude real evidence or
validate nothing.

**Response 201**

| Field | Type | Notes |
|---|---|---|
| `id` | uuid | |
| `item_id` | uuid | |
| `attestor_ref` | string | Salted identity digest. Stable across reads, meaningless without the subject's salt. This is also the value anchored on chain. **No name, email or user id is ever returned.** |
| `attestor_role` | string | The role held **when the attestation was made**, not the role held now. |
| `attestor_fraud_flagged` | bool | Surfaced so a reader can see why an attestation is present but not counting. |
| `statement` | object | As submitted. |
| `statement_hash` | string | `0x`-prefixed keccak256 of the canonical statement. |
| `created_at` | datetime | |

```json
{
  "id": "01a04842-7abc-7542-8dc7-762a741ad3ec",
  "item_id": "01a04842-76c0-72c1-b417-0be953c395e1",
  "attestor_ref": "0xbaad0f4efcff13477223efac0d694f3b0ce8bcabe8e8e3b10b5e1b3f98e3b5e5",
  "attestor_role": "COOP_OFFICER",
  "attestor_fraud_flagged": false,
  "statement": {
    "inspected_on": "2026-08-20",
    "ledger_ref": "PWC/2026/0412",
    "notes": "Double ikat confirmed against co-operative register."
  },
  "statement_hash": "0xa7b0be1c6fe045bf09bb3c2863e6f1a8e86740cd028ecc9fe3b9d9e996a6d9bd",
  "created_at": "2026-08-28T12:05:11.481648Z"
}
```

**Errors**

| Code | Status | Trigger |
|---|---|---|
| `VALIDATION_FAILED` | 422 | Empty statement, too many keys, too many bytes, or an unknown field. |
| `ITEM_NOT_FOUND` | 404 | No such item. |
| `ACTOR_FRAUD_FLAGGED` | 403 | The caller is fraud-flagged. |
| `INSUFFICIENT_ROLE` | 403 | The caller's role is not `WEAVER`, `COOP_OFFICER` or `INSPECTOR`. `details.required` lists the permitted roles. |
| `DUPLICATE_ATTESTATION` | 409 | This actor has already attested to this item. `details.item_id` is present. |
| `UNAUTHENTICATED` / `TOKEN_EXPIRED` / `TOKEN_INVALID` / `ACCOUNT_SUSPENDED` | 401 | Credential problems. |
| `SERVICE_UNAVAILABLE` | 503 | Database unreachable. |
| `INTERNAL_ERROR` | 500 | Unhandled exception. |

```json
{
  "error": {
    "code": "ACTOR_FRAUD_FLAGGED",
    "message": "a fraud-flagged actor may not record attestations",
    "details": null,
    "request_id": "46526a41ea0c4fff925ec53a835bcca8"
  }
}
```

---

#### `GET {API_PREFIX}/items/{item_id}/attestations`

**Auth:** bearer · **Rate limit:** none · **Idempotency:** n/a

**Request path:** `item_id` — uuid. **Request query:**

| Parameter | Type | Required | Default | Constraints |
|---|---|---|---|---|
| `cursor` | string \| null | no | — | Opaque keyset cursor. |
| `limit` | int | no | 20 | 1–100 |

**Response 200** — **not** the standard collection envelope. This one is
`{"items": [...], "next_cursor": ...}`, newest first.

```json
{
  "items": [
    {
      "id": "01a04842-7abc-7542-8dc7-762a741ad3ec",
      "item_id": "01a04842-76c0-72c1-b417-0be953c395e1",
      "attestor_ref": "0xbaad0f4efcff13477223efac0d694f3b0ce8bcabe8e8e3b10b5e1b3f98e3b5e5",
      "attestor_role": "COOP_OFFICER",
      "attestor_fraud_flagged": false,
      "statement": {
        "notes": "Double ikat confirmed against co-operative register.",
        "ledger_ref": "PWC/2026/0412",
        "inspected_on": "2026-08-20"
      },
      "statement_hash": "0xa7b0be1c6fe045bf09bb3c2863e6f1a8e86740cd028ecc9fe3b9d9e996a6d9bd",
      "created_at": "2026-08-28T12:05:11.481648Z"
    }
  ],
  "next_cursor": null
}
```

**Errors:** `VALIDATION_FAILED` (422), `INVALID_CURSOR` (422),
`ITEM_NOT_FOUND` (404),
`UNAUTHENTICATED` / `TOKEN_EXPIRED` / `TOKEN_INVALID` / `ACCOUNT_SUSPENDED` (401),
`SERVICE_UNAVAILABLE` (503), `INTERNAL_ERROR` (500).

---

#### `GET {API_PREFIX}/items/{item_id}/trust`

**Auth:** bearer · **Rate limit:** none · **Idempotency:** n/a

**Request path:** `item_id` — uuid.

**Response 200** — derived on every read from the attestation and dispute sets.
Nothing is cached and nothing is stored, so a fraud flag applied a second ago is
already reflected here.

| Field | Type | Notes |
|---|---|---|
| `item_id` | uuid | |
| `level` | string | `SELF_DECLARED` \| `CO_OP_ATTESTED` \| `INSPECTED` \| `DISPUTED`. See §5.2. |
| `contributing_roles` | string[] | Which independent roles lifted the level. Empty at `SELF_DECLARED` and empty at `DISPUTED`. |
| `attestation_count` | int | All attestations, including ones that do not count. |
| `distinct_attestor_count` | int | Unique attestors. |
| `dispute_reason` | string \| null | Why the record is contested. `null` is the common case and does **not** mean "fine"; it means nobody has raised anything. |
| `flagged_attestor_count` | int | Lets a reader tell "nobody vouched" from "people vouched and were disqualified". |

```json
{
  "item_id": "01a04842-76c0-72c1-b417-0be953c395e1",
  "level": "CO_OP_ATTESTED",
  "contributing_roles": ["COOP_OFFICER"],
  "attestation_count": 1,
  "distinct_attestor_count": 1,
  "dispute_reason": null,
  "flagged_attestor_count": 0
}
```

The same item after its attestor was fraud-flagged:

```json
{
  "item_id": "01a04842-76c0-72c1-b417-0be953c395e1",
  "level": "DISPUTED",
  "contributing_roles": [],
  "attestation_count": 1,
  "distinct_attestor_count": 1,
  "dispute_reason": "1 attestor(s) on this record are fraud-flagged; their attestations no longer contribute",
  "flagged_attestor_count": 1
}
```

**Errors:** `ITEM_NOT_FOUND` (404),
`UNAUTHENTICATED` / `TOKEN_EXPIRED` / `TOKEN_INVALID` / `ACCOUNT_SUSPENDED` (401),
`SERVICE_UNAVAILABLE` (503), `INTERNAL_ERROR` (500).

---

#### `POST {API_PREFIX}/admin/actors/{user_id}/fraud-flag`

**Auth:** bearer + role(ADMIN) · **Rate limit:** none · **Idempotency:** n/a

Flags an actor and disputes **everything they registered**, in one transaction.
Items they merely *attested to* are not touched — those drop on the next read,
because trust is derived and a flagged attestor stops counting.

**Request path:** `user_id` — uuid. **Request body** — unknown fields rejected.

| Field | Type | Required | Constraints |
|---|---|---|---|
| `reason` | string | yes | 8–1000 characters. Shown on every item it touches. |

**Response 200**

| Field | Type | Notes |
|---|---|---|
| `actor_id` | uuid | |
| `fraud_flagged` | bool | `true` here. |
| `items_affected` | int | Items registered by the actor that became disputed. |
| `attestations_affected` | int | Attestations by the actor that stopped counting. |
| `already_in_state` | bool | `true` when the actor was already flagged and nothing moved. |

```json
{
  "actor_id": "01a04842-79b7-7621-b2c9-6fcfbf497fe9",
  "fraud_flagged": true,
  "items_affected": 0,
  "attestations_affected": 1,
  "already_in_state": false
}
```

**Errors:** `VALIDATION_FAILED` (422), `USER_NOT_FOUND` (404),
`UNAUTHENTICATED` / `TOKEN_EXPIRED` / `TOKEN_INVALID` / `ACCOUNT_SUSPENDED` (401),
`INSUFFICIENT_ROLE` (403), `SERVICE_UNAVAILABLE` (503), `INTERNAL_ERROR` (500).

```json
{
  "error": {
    "code": "USER_NOT_FOUND",
    "message": "no user with id 02fc823f-0746-439d-8e2e-b1a01e233836",
    "details": null,
    "request_id": "a9c0f0cb71a04b9a9baed2e1d80ee644"
  }
}
```

---

#### `POST {API_PREFIX}/admin/actors/{user_id}/fraud-clear`

**Auth:** bearer + role(ADMIN) · **Rate limit:** none · **Idempotency:** n/a

Reverses exactly what the flag did, and nothing else. An item independently
disputed by an inspector **stays disputed**: that finding is not this flag's to
lift.

**Request path:** `user_id` — uuid. **Request body:** identical to `fraud-flag`.

**Response 200** — identical shape, with `fraud_flagged: false`.

```json
{
  "actor_id": "01a04842-79b7-7621-b2c9-6fcfbf497fe9",
  "fraud_flagged": false,
  "items_affected": 0,
  "attestations_affected": 1,
  "already_in_state": false
}
```

**Errors:** identical to `fraud-flag`.

---

### 3.8 Media

---

#### `POST {API_PREFIX}/media`

**Auth:** bearer + role(WEAVER, COOP_OFFICER, INSPECTOR) · **Rate limit:** none · **Idempotency:** n/a

**Request:** `multipart/form-data` with a single part.

| Part | Type | Required | Constraints |
|---|---|---|---|
| `file` | file | yes | ≤ 5242880 bytes (5 MiB). Sniffed by magic bytes; the client's declared content type is ignored. Accepted: `image/jpeg`, `image/png`, `image/webp`, `video/mp4`. Files ≤ 2097152 bytes (2 MiB) additionally get a database blob copy. |

**Response 201**

| Field | Type | Notes |
|---|---|---|
| `id` | uuid | |
| `sha256` | string | 64 hex characters, no `0x` prefix. The integrity proof. |
| `byte_size` | int | |
| `content_type` | string | The **sniffed** type, not the declared one. |
| `cid` | string \| null | IPFS content id. `null` until a pin succeeds, which is a normal steady state. |
| `pin_status` | string | `PIN_PENDING` \| `PINNED` \| `PIN_FAILED` |
| `created_at` | datetime | |
| `tiers` | object[] | Every place these bytes can be read from, **best first**. Each is `{tier, url, durable}` with `tier` in `IPFS`, `MIRROR`, `BLOB`. |
| `primary_tier` | string \| null | The first entry of `tiers`, repeated for callers that only want one. |
| `durable` | bool | True when at least one tier survives a redeploy. |

**`201` is returned even when pinning fails or is switched off.** The SHA-256 is
the integrity proof and it is already committed; the CID only records where a
copy happens to live.

```json
{
  "id": "01a04861-b078-7bc2-989f-5fc1292a0893",
  "sha256": "d5450104e18d2808f4b5a73c12627cb1124073406bae8adaa893d0a48ba6a418",
  "byte_size": 76,
  "content_type": "image/jpeg",
  "cid": "bafyd5450104e18d2808f4b5a73c12627cb1124073406bae8a",
  "pin_status": "PINNED",
  "created_at": "2026-08-28T12:39:16.854931Z",
  "tiers": [
    { "tier": "IPFS", "url": "https://gateway.pinata.cloud/ipfs/bafyd5450104e18d2808f4b5a73c12627cb1124073406bae8a", "durable": false },
    { "tier": "MIRROR", "url": "http://localhost:8000/api/v1/media/01a04861-b078-7bc2-989f-5fc1292a0893/raw?tier=MIRROR", "durable": false },
    { "tier": "BLOB", "url": "http://localhost:8000/api/v1/media/01a04861-b078-7bc2-989f-5fc1292a0893/raw?tier=BLOB", "durable": true }
  ],
  "primary_tier": "IPFS",
  "durable": true
}
```

**Errors**

| Code | Status | Trigger |
|---|---|---|
| `VALIDATION_FAILED` | 422 | No file part, or the file is empty. |
| `UNSUPPORTED_MEDIA_TYPE` | 415 | The bytes are not one of the four accepted types, whatever the client declared. `details.allowed` lists them. |
| `MEDIA_TOO_LARGE` | 413 | Larger than 5 MiB. Detected while streaming, so the whole file is never buffered. `details.limit_bytes` is present. |
| `STORAGE_BUDGET_EXCEEDED` | 507 | A storage budget is exhausted. Does **not** clear with time — see §4. |
| `UNAUTHENTICATED` / `TOKEN_EXPIRED` / `TOKEN_INVALID` / `ACCOUNT_SUSPENDED` | 401 | Credential problems. |
| `INSUFFICIENT_ROLE` | 403 | Caller is a `CONSUMER`. |
| `SERVICE_UNAVAILABLE` | 503 | Database unreachable. |
| `INTERNAL_ERROR` | 500 | Unhandled exception. |

```json
{
  "error": {
    "code": "UNSUPPORTED_MEDIA_TYPE",
    "message": "file type not accepted; the bytes are not jpeg, png, webp or mp4",
    "details": { "allowed": ["image/jpeg", "image/png", "image/webp", "video/mp4"] },
    "request_id": "2e49e557cd4e46d6a80d704dfa1b3c3f"
  }
}
```

---

#### `GET {API_PREFIX}/media/{media_id}`

**Auth:** bearer · **Rate limit:** none · **Idempotency:** n/a

**Request path:** `media_id` — uuid.

**Response 200** — identical shape to the upload response, with tiers recomputed
as of now.

**Errors:** `NOT_FOUND` (404),
`UNAUTHENTICATED` / `TOKEN_EXPIRED` / `TOKEN_INVALID` / `ACCOUNT_SUSPENDED` (401),
`SERVICE_UNAVAILABLE` (503), `INTERNAL_ERROR` (500).

---

#### `GET {API_PREFIX}/media/{media_id}/raw`

**Auth:** bearer · **Rate limit:** none · **Idempotency:** n/a

**Request path:** `media_id` — uuid. **Request query:**

| Parameter | Type | Required | Constraints |
|---|---|---|---|
| `tier` | string \| null | no | `MIRROR` or `BLOB`. **Advisory only**: the mirror may have been wiped by a redeploy since the row was written, so a request for it still falls back to the blob rather than failing on a tier that used to exist. |

**Response 200** — the raw bytes.

```
Content-Type: image/jpeg
Content-Disposition: inline; filename="d5450104e18d2808f4b5a73c12627cb1124073406bae8adaa893d0a48ba6a418"
ETag: "d5450104e18d2808f4b5a73c12627cb1124073406bae8adaa893d0a48ba6a418"
Cache-Control: public, max-age=31536000, immutable
X-Content-Type-Options: nosniff
X-Sutradhar-Tier: MIRROR
```

**Errors**

| Code | Status | Trigger |
|---|---|---|
| `NOT_FOUND` | 404 | No such media row, **or** both local tiers are gone. `details` carries `media_id` and `sha256` in the second case — an honest 404 rather than a redirect to a gateway that may also be dead. |
| `UNAUTHENTICATED` / `TOKEN_EXPIRED` / `TOKEN_INVALID` / `ACCOUNT_SUSPENDED` | 401 | Credential problems. |
| `SERVICE_UNAVAILABLE` | 503 | Database unreachable. |
| `INTERNAL_ERROR` | 500 | Unhandled exception. |

---

#### `POST {API_PREFIX}/items/{item_id}/media`

**Auth:** bearer (registrant of the item, or `ADMIN`) · **Rate limit:** none · **Idempotency:** n/a

**Request path:** `item_id` — uuid. **Request body** — unknown fields rejected.

| Field | Type | Required | Constraints |
|---|---|---|---|
| `media_id` | uuid | yes | Must name an existing media row. |
| `kind` | string | yes | `LOOM_PHOTO` \| `WEAVE_MACRO` \| `CERTIFICATE` \| `VIDEO` |

**Response 201**

```json
{
  "media": {
    "id": "01a04842-7d47-7100-9d14-8b6ce1b7fd89",
    "sha256": "d5450104e18d2808f4b5a73c12627cb1124073406bae8adaa893d0a48ba6a418",
    "byte_size": 76,
    "content_type": "image/jpeg",
    "cid": "bafyd5450104e18d2808f4b5a73c12627cb1124073406bae8a",
    "pin_status": "PINNED",
    "created_at": "2026-08-28T12:05:12.133577Z"
  },
  "kind": "LOOM_PHOTO"
}
```

**Errors**

| Code | Status | Trigger |
|---|---|---|
| `VALIDATION_FAILED` | 422 | Body fails schema validation, or `kind` is not a known kind. |
| `ITEM_NOT_FOUND` | 404 | No such item. |
| `NOT_FOUND` | 404 | No such media. |
| `FORBIDDEN` | 403 | Caller is neither the item's registrant nor an `ADMIN`. |
| `CONFLICT` | 409 | This media is already linked to this item. |
| `UNAUTHENTICATED` / `TOKEN_EXPIRED` / `TOKEN_INVALID` / `ACCOUNT_SUSPENDED` | 401 | Credential problems. |
| `SERVICE_UNAVAILABLE` | 503 | Database unreachable. |
| `INTERNAL_ERROR` | 500 | Unhandled exception. |

---

#### `GET {API_PREFIX}/items/{item_id}/media`

**Auth:** bearer · **Rate limit:** none · **Idempotency:** n/a

**Request path:** `item_id` — uuid.

**Response 200** — a **bare JSON array**, not a collection envelope. Ordered by
media creation time.

```json
[
  {
    "media": {
      "id": "01a04842-7d47-7100-9d14-8b6ce1b7fd89",
      "sha256": "d5450104e18d2808f4b5a73c12627cb1124073406bae8adaa893d0a48ba6a418",
      "byte_size": 76,
      "content_type": "image/jpeg",
      "cid": "bafyd5450104e18d2808f4b5a73c12627cb1124073406bae8a",
      "pin_status": "PINNED",
      "created_at": "2026-08-28T12:05:12.133577Z"
    },
    "kind": "LOOM_PHOTO"
  }
]
```

**Errors:** `UNAUTHENTICATED` / `TOKEN_EXPIRED` / `TOKEN_INVALID` / `ACCOUNT_SUSPENDED` (401),
`SERVICE_UNAVAILABLE` (503), `INTERNAL_ERROR` (500). An item with no media
returns `[]`; a nonexistent item also returns `[]`.

---

#### `DELETE {API_PREFIX}/items/{item_id}/media/{media_id}`

**Auth:** bearer (registrant of the item, or `ADMIN`) · **Rate limit:** none · **Idempotency:** n/a

**Request path:** `item_id`, `media_id` — uuids.

**Response 204** — no body.

**Removes the link only.** The media row and its bytes are kept: the SHA-256 may
already be anchored on chain, and deleting bytes behind an anchored hash produces
exactly the dead reference the three-tier design exists to prevent. "No longer
depicts this item" is not "never existed".

**Errors**

| Code | Status | Trigger |
|---|---|---|
| `NOT_FOUND` | 404 | The media is not linked to that item — including a repeated delete. |
| `ITEM_NOT_FOUND` | 404 | No such item. |
| `FORBIDDEN` | 403 | Caller is neither the item's registrant nor an `ADMIN`. |
| `UNAUTHENTICATED` / `TOKEN_EXPIRED` / `TOKEN_INVALID` / `ACCOUNT_SUSPENDED` | 401 | Credential problems. |
| `SERVICE_UNAVAILABLE` | 503 | Database unreachable. |
| `INTERNAL_ERROR` | 500 | Unhandled exception. |

---

### 3.9 Tags and QR

---

#### `POST {API_PREFIX}/items/{item_id}/tag`

**Auth:** bearer + role(WEAVER, COOP_OFFICER) · **Rate limit:** none · **Idempotency:** required

Generates a code, binds it to the item under a conditional update, and records a
`TAG_ISSUED` event. A `WEAVER` may only tag items they registered; a
`COOP_OFFICER` or `ADMIN` may tag any.

**Request path:** `item_id` — uuid. **Request:** no body.

**Response 201**

| Field | Type | Notes |
|---|---|---|
| `item_id` | uuid | |
| `tag_code` | string | Canonical form: 12 uppercase characters, no separators. |
| `display_code` | string | Grouped in fours, for the label. |
| `payload_url` | string | The exact string encoded in the QR. Nothing else goes in the image. |
| `warnings` | string[] | **Non-blocking advisories.** Empty is the common case. |

```json
{
  "item_id": "01a04840-7562-7b01-b038-6a18f5f676a9",
  "tag_code": "86RG4H4BGJ55",
  "display_code": "86RG-4H4B-GJ55",
  "payload_url": "http://localhost:3000/v/86RG4H4BGJ55",
  "warnings": [
    "item 01a04840-7562-7b01-b038-6a18f5f676a9 has been split into child items: a tag belongs on the smallest sellable unit, and one tag covering several pieces is the substitution path this system exists to close. Tag the children instead unless this item is sold whole."
  ]
}
```

Show `warnings` to the operator. Tagging a split item is legal — a bolt can be
sold whole — and is also the laundering shape, so it is said out loud rather than
silently allowed.

**Errors**

| Code | Status | Trigger |
|---|---|---|
| `VALIDATION_FAILED` | 422 | `Idempotency-Key` header absent. |
| `TAG_NOT_ISSUABLE` | 422 | The item's `status` is `FAILED` — its hash never reached the chain, so it has no recorded provenance to tag. `details` carries `item_id` and `status`. |
| `ITEM_NOT_FOUND` | 404 | No such item. |
| `FORBIDDEN` | 403 | A `WEAVER` tagging an item they did not register. |
| `INSUFFICIENT_ROLE` | 403 | Caller is a `CONSUMER` or an `INSPECTOR`. |
| `TAG_ALREADY_ISSUED` | 409 | The item already carries a tag. **`details` carries the existing `tag_code`, `display_code` and `payload_url`** — the caller's next move is to print it, not to retry. |
| `IDEMPOTENCY_KEY_REUSED` | 409 | The key was used for a different body. |
| `TAG_GENERATION_EXHAUSTED` | 500 | Every generation attempt collided. At ~53 bits of entropy this cannot happen by chance, so it means the generator is broken. |
| `UNAUTHENTICATED` / `TOKEN_EXPIRED` / `TOKEN_INVALID` / `ACCOUNT_SUSPENDED` | 401 | Credential problems. |
| `SERVICE_UNAVAILABLE` | 503 | Database unreachable. |
| `INTERNAL_ERROR` | 500 | Unhandled exception. |

```json
{
  "error": {
    "code": "TAG_ALREADY_ISSUED",
    "message": "this item already carries a tag",
    "details": {
      "item_id": "01a04842-76c0-72c1-b417-0be953c395e1",
      "tag_code": "60KJM9HGBKWF",
      "display_code": "60KJ-M9HG-BKWF",
      "payload_url": "http://localhost:3000/v/60KJM9HGBKWF"
    },
    "request_id": "f4cdb18c31944a03b57ce2f7f692dc62"
  }
}
```

---

#### `GET {API_PREFIX}/items/{item_id}/tag/qr`

**Auth:** bearer + role(WEAVER, COOP_OFFICER, INSPECTOR) · **Rate limit:** none · **Idempotency:** n/a
**Response headers:** `Cache-Control: public, max-age=31536000, immutable`, `Content-Disposition: inline; filename="<TAG_CODE>.<ext>"`

Authenticated even though the payload it encodes is public: the QR *contents* are
a printed number, but the mapping from an internal item id to that number is not.

**Request path:** `item_id` — uuid. **Request query:**

| Parameter | Type | Required | Default | Constraints |
|---|---|---|---|---|
| `format` | string | no | `png` | `png` or `svg`. Anything else is `VALIDATION_FAILED`. |
| `size` | int | no | 512 | ≥ 1 in the schema; clamped server-side to 128–2048. Pixel edge for PNG, default `width`/`height` for SVG. |

**Response 200** — `image/png` or `image/svg+xml`. Error correction level **Q**,
four-module quiet zone. The SVG carries a `viewBox` in module units, so a print
shop can scale it to any physical size without resampling:

```
<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 41 41" shape-rendering="crispEdges" role="img" aria-label="QR code for tag 60KJ-M9HG-BKWF"><rect width="41" height="41" fill="#ffffff"/><path ...
```

The image contains the tag code and nothing else — no identifiers, no names, no
metadata. The file is destined for a printer that belongs to somebody else.

**Errors**

| Code | Status | Trigger |
|---|---|---|
| `VALIDATION_FAILED` | 422 | `format` is not `png` or `svg`, or `size` is below 1. |
| `NOT_FOUND` | 404 | The item exists but has no tag yet. `details.item_id` is present. |
| `ITEM_NOT_FOUND` | 404 | No such item. |
| `INSUFFICIENT_ROLE` | 403 | Caller is a `CONSUMER`. |
| `UNAUTHENTICATED` / `TOKEN_EXPIRED` / `TOKEN_INVALID` / `ACCOUNT_SUSPENDED` | 401 | Credential problems. |
| `SERVICE_UNAVAILABLE` | 503 | Database unreachable. |
| `INTERNAL_ERROR` | 500 | Unhandled exception. |

```json
{
  "error": {
    "code": "NOT_FOUND",
    "message": "this item has no tag yet; issue one first",
    "details": { "item_id": "01a04842-7f37-73d3-a348-bf373b3efb67" },
    "request_id": "6b85a04cbd3149c1ba49886ecd7081cb"
  }
}
```

---

#### `POST {API_PREFIX}/admin/tags/bulk`

**Auth:** bearer + role(COOP_OFFICER) · **Rate limit:** none · **Idempotency:** n/a

The path says admin; the permission says co-op officer. Batch tagging is the
co-op operator's job — it is the friction this endpoint exists to remove — and
routing it through an admin would put somebody who is not in the room between a
weaver's output and its labels. `ADMIN` also passes.

**Request body** — unknown fields rejected.

| Field | Type | Required | Constraints |
|---|---|---|---|
| `item_ids` | uuid[] | yes | At most **500** entries. |

**Response 200** — **partial success is success.** An already-tagged item, or one
whose anchoring failed, is reported and stepped over rather than failing the
whole batch.

| Field | Type | Notes |
|---|---|---|
| `requested` | int | |
| `issued` | int | |
| `already_tagged` | int | |
| `failed` | int | |
| `results` | object[] | One entry per requested id, in request order. |
| `results[].outcome` | string | `issued` \| `already_tagged` \| `failed` |
| `results[].tag_code` / `display_code` / `payload_url` | string \| null | Present for `issued` and `already_tagged`. |
| `results[].reason_code` | string \| null | The error code for anything other than `issued`. |
| `results[].reason` | string \| null | Its message. |
| `results[].warnings` | string[] | As for single issuance. |

```json
{
  "requested": 3,
  "issued": 1,
  "already_tagged": 1,
  "failed": 1,
  "results": [
    {
      "item_id": "01a04840-7d9e-7120-af42-da6686224d01",
      "outcome": "issued",
      "tag_code": "C7DHCWJM68C2",
      "display_code": "C7DH-CWJM-68C2",
      "payload_url": "http://localhost:3000/v/C7DHCWJM68C2",
      "reason_code": null,
      "reason": null,
      "warnings": []
    },
    {
      "item_id": "01a04840-7562-7b01-b038-6a18f5f676a9",
      "outcome": "already_tagged",
      "tag_code": "86RG4H4BGJ55",
      "display_code": "86RG-4H4B-GJ55",
      "payload_url": "http://localhost:3000/v/86RG4H4BGJ55",
      "reason_code": "TAG_ALREADY_ISSUED",
      "reason": "this item already carries a tag",
      "warnings": []
    },
    {
      "item_id": "f9c7a78f-e6d4-4a2e-a2dc-ac73db8c9670",
      "outcome": "failed",
      "tag_code": null,
      "display_code": null,
      "payload_url": null,
      "reason_code": "ITEM_NOT_FOUND",
      "reason": "no item with id f9c7a78f-e6d4-4a2e-a2dc-ac73db8c9670",
      "warnings": []
    }
  ]
}
```

**Errors**

| Code | Status | Trigger |
|---|---|---|
| `VALIDATION_FAILED` | 422 | Body fails schema validation. |
| `BULK_TOO_LARGE` | 422 | More than 500 ids. Checked **before anything is written**, so an oversized batch issues nothing at all. `details` carries `limit` and `received`. |
| `INSUFFICIENT_ROLE` | 403 | Caller is not `COOP_OFFICER` or `ADMIN`. |
| `UNAUTHENTICATED` / `TOKEN_EXPIRED` / `TOKEN_INVALID` / `ACCOUNT_SUSPENDED` | 401 | Credential problems. |
| `SERVICE_UNAVAILABLE` | 503 | Database unreachable. |
| `INTERNAL_ERROR` | 500 | Unhandled exception. |

```json
{
  "error": {
    "code": "BULK_TOO_LARGE",
    "message": "a batch may hold at most 500 items",
    "details": { "limit": 500, "received": 501 },
    "request_id": "980a20a555a4432fb215a7fbb5cc192b" }
}
```

---

### 3.10 Public verification

**Not under `{API_PREFIX}`.** These two routes are mounted at
`{PUBLIC_PREFIX}/v/...`, which is the empty prefix by default. They are the only
unauthenticated surface that touches item data, and they follow three rules
nothing else does: they never return `500`, they leak nothing (every field is a
hand-written projection, so no column added later becomes public by accident),
and they answer a malformed code without touching the database.

---

#### `GET /v/{tag_code}`

**Auth:** public · **Rate limit:** 60 / min / IP · **Idempotency:** n/a
**Response headers:** `Cache-Control: public, max-age=60`, `ETag`

**Reads only. No scan is recorded here** — a `GET` that wrote a scan row would
mean every link preview, crawler and browser prefetch counted as somebody holding
the object.

**Request path:** `tag_code` — any typed form; normalised per §1.5.
**Request headers (optional):** `If-None-Match` for a conditional read.

**Response 200**

| Field | Type | Notes |
|---|---|---|
| `tag_code` | string | Canonical form. |
| `display_code` | string | Grouped in fours. |
| `category` | object | `{slug, display_name, schema_version}` |
| `attributes` | object | **Filtered.** See the complete withholding rule below the example. |
| `quantity` | string | 4dp |
| `quantity_unit` | string | |
| `trust` | object | `{level, contributing_roles, attestation_count, disputed}`. See §5.2. |
| `chain` | object | See §5.3. |
| `provenance` | object | `{ancestry, events, child_count}` |
| `provenance.ancestry[]` | object | `{depth, quantity, quantity_unit, status}` — **no ids, no tag codes.** Those identify other objects. |
| `provenance.events[]` | object | `{type, at, tx_hash, block_number}` — **payloads are never published.** At most 50, oldest first. |
| `story` | object | `{weaver_display_name, region, maker_opted_out, media}` |
| `story.media[]` | object | `{kind, sha256, cid, gateway_url}`. `gateway_url` is `null` until pinned. |
| `scan` | object | `{count, suspicion_level, reason, signals}`. See §5.4. |
| `claim` | object | `{status, claimed, claimed_at, is_your_claim, claimed_region, message}`. See §5.5. |

**No user ids, no item ids, no email, no phone, no address, and no legal name
appear anywhere in this payload.** Internal ids are absent not because they are
secret but because publishing them turns one tag code into a way to walk the
item graph.

```json
{
  "tag_code": "B69C8K9853SS",
  "display_code": "B69C-8K98-53SS",
  "category": {
    "slug": "patola-silk",
    "display_name": "Patan Patola Silk",
    "schema_version": 1
  },
  "attributes": {
    "dye_type": "natural",
    "loom_type": "pit",
    "warp_count": 120,
    "weave_days": 210,
    "weft_count": 116,
    "double_ikat": true,
    "gi_registration_no": "GI-00232"
  },
  "quantity": "12.0000",
  "quantity_unit": "metre",
  "trust": {
    "level": "CO_OP_ATTESTED",
    "contributing_roles": ["COOP_OFFICER"],
    "attestation_count": 1,
    "disputed": false
  },
  "chain": {
    "status": "PENDING",
    "tx_hash": null,
    "block_number": null,
    "confirmations": 0,
    "anchored_at": null,
    "verification": "UNANCHORED",
    "stale": true,
    "chain_checked_at": "2026-08-28T12:39:18.696410Z",
    "inclusion_proof": null
  },
  "provenance": {
    "ancestry": [],
    "events": [
      { "type": "REGISTERED", "at": "2026-08-28T12:37:20.796586Z", "tx_hash": null, "block_number": null },
      { "type": "ATTESTED", "at": "2026-08-28T12:37:21.079512Z", "tx_hash": null, "block_number": null },
      { "type": "TAG_ISSUED", "at": "2026-08-28T12:37:21.096502Z", "tx_hash": null, "block_number": null }
    ],
    "child_count": 0
  },
  "story": {
    "weaver_display_name": "Kanubhai R. Patel",
    "region": "Patan, Gujarat",
    "maker_opted_out": false,
    "media": [
      {
        "kind": "LOOM_PHOTO",
        "sha256": "d5450104e18d2808f4b5a73c12627cb1124073406bae8adaa893d0a48ba6a418",
        "cid": "bafyd5450104e18d2808f4b5a73c12627cb1124073406bae8a",
        "gateway_url": "https://gateway.pinata.cloud/ipfs/bafyd5450104e18d2808f4b5a73c12627cb1124073406bae8a"
      }
    ]
  },
  "scan": {
    "count": 0,
    "suspicion_level": "NONE",
    "reason": null,
    "signals": []
  },
  "claim": {
    "status": "UNCLAIMED",
    "claimed": false,
    "claimed_at": null,
    "is_your_claim": false,
    "claimed_region": null,
    "message": null
  }
}
```

**Which attributes are withheld.** A category schema is operator-authored, so
the set of keys is not fixed at build time and an allowlist is impossible without
freezing the catalogue. This is the denylist under it, and it is the complete
list: a key is withheld when it starts with `_`, or when its lowercased form
**contains** any of

`aadhaar`, `aadhar`, `account`, `address`, `contact`, `dob`, `email`, `gst`,
`ifsc`, `mobile`, `name`, `pan_`, `passport`, `phone`, `upi`, `user`,
`weaver_id`

as a substring — so `contact_number` and `weaverName` are both caught. It is
deliberately blunt: withholding a legitimate field called `dyer_name` costs a
reader one line of context, and publishing it costs somebody their name. A
public page must therefore not assume that every key in the category's schema
will be present.

**Response 304** — when `If-None-Match` matches the current `ETag`. No body;
carries `Cache-Control` and `ETag`.

`inclusion_proof`, when present, is
`{root, leaf_index, leaf_count, proof: string[]}` and lets a reader verify the
Merkle inclusion offline, without trusting this service.

**Errors**

| Code | Status | Trigger |
|---|---|---|
| `INVALID_TAG_CODE` | 400 | Wrong length, out-of-alphabet character, or a failed check symbol. Answered **before any database query**. |
| `NOT_FOUND` | 404 | A well-formed code nobody holds. Says nothing about whether it was ever issued. |
| `RATE_LIMITED` | 429 | More than 60 reads from this IP in a minute. |
| `SERVICE_UNAVAILABLE` | 503 | Database unreachable. |

```json
{
  "error": {
    "code": "INVALID_TAG_CODE",
    "message": "that is not a readable tag code: it must be 12 characters and end in a check symbol",
    "details": null,
    "request_id": "1e5e68256f634ec6b830690f7cde76c5"
  }
}
```

```json
{
  "error": {
    "code": "NOT_FOUND",
    "message": "no record for this tag",
    "details": null,
    "request_id": "86b832197b3646639868cd8ddcbb602e"
  }
}
```

---

#### `POST /v/{tag_code}/scan`

**Auth:** public · **Rate limit:** 60 / min / IP · **Idempotency:** n/a
**Response headers:** `Cache-Control: no-store`, `X-Scan-Recorded: true|false`

Records the scan, scores the pattern **including the scan just made**, attempts
the claim, and returns the record. Order matters: scoring before writing would
leave the response one scan behind the thing it exists to notice.

**Request path:** `tag_code`. **Request body** — optional; `{}` and a wholly
absent body both work. Unknown fields are rejected.

| Field | Type | Required | Constraints |
|---|---|---|---|
| `device_fingerprint` | string \| null | no | ≤ 256 characters. An opaque client-chosen string; only its salted SHA-256 is ever stored. |
| `region_code` | string \| null | no | ≤ 8 characters. Consulted **only** when the edge that terminated the connection said nothing. |

**Geography comes from headers first.** The server reads
`X-Vercel-IP-Country` / `CF-IPCountry` / `X-Geo-Country` and
`X-Vercel-IP-Country-Region` / `CF-Region-Code` / `X-Geo-Region`, in that order,
because a header set by the infrastructure that saw the connection outranks a
value the caller typed. The finest granularity stored anywhere is a state; there
is no GPS, no city, and no raw address.

**Response 201** when a new scan row was written, **200** when it was
deduplicated (same object, device, place and network inside 60 seconds).
`X-Scan-Recorded` says which. **The body is the same `PublicItemView` as
`GET /v/{tag_code}`.**

```json
{
  "tag_code": "JYHPPTVXN89R",
  "display_code": "JYHP-PTVX-N89R",
  "scan": {
    "count": 2,
    "suspicion_level": "SUSPICIOUS",
    "reason": "Two scans 543 km apart (IN-GJ then IN-MH) happened less than a second apart, which implies travelling about 19,490,269 km/h -- faster than the 900 km/h this system treats as possible.",
    "signals": ["IMPOSSIBLE_VELOCITY"]
  },
  "claim": {
    "status": "ALREADY_CLAIMED",
    "claimed": true,
    "claimed_at": "2026-08-28T12:03:01.527834Z",
    "is_your_claim": false,
    "claimed_region": "IN-GJ",
    "message": "This tag was already claimed in IN-GJ on 28 August 2026 by the first device that scanned it. If you did not expect that, ask the seller you bought this from about it."
  }
}
```

(Trimmed to the two blocks that differ from the `GET`; every other field is
present and identical in shape.)

**Errors:** `VALIDATION_FAILED` (422) for an unknown body field or an
over-length value; `INVALID_TAG_CODE` (400); `NOT_FOUND` (404);
`RATE_LIMITED` (429); `SERVICE_UNAVAILABLE` (503).

---

### 3.11 Admin

---

#### `GET {API_PREFIX}/admin/system/status`

**Auth:** bearer + role(ADMIN) · **Rate limit:** none · **Idempotency:** n/a

One read of everything an operator needs. **It never returns `500`**, for the
same reason the public surface does not: an operator opens it precisely when
something is broken. An unreachable dependency is reported as a `null` with a
reason, never as an exception and never as a zero that would read as "fine".

**It reports; it does not decide.** No knobs, no retry buttons, no requeue.

**Request:** no parameters.

**Response 200**

| Field | Type | Notes |
|---|---|---|
| `observed_at` | datetime | |
| `app_env` | string | `local` \| `staging` \| `production` |
| `chain.mode` | string | **Read this first.** `live` \| `postgres_only`. See §5.1. |
| `chain.contract_address` | string | |
| `chain.chain_id` | int | |
| `chain.write_enabled` | bool | |
| `chain.signer_configured` | bool | |
| `chain.contract_deployed` | bool | False when `contract_address` is the zero address. |
| `chain.rpc_available` | bool | |
| `outbox` | object[] | `{job_type, status, count}` grouped by both. Job types: `ANCHOR_ITEM`, `ANCHOR_ATTESTATION`, `ANCHOR_BATCH`, `PIN_MEDIA`. Statuses: `QUEUED`, `IN_FLIGHT`, `DONE`, `DEAD`. |
| `outbox_total` | int | |
| `dead_letters` | int | Parked jobs, total. |
| `dead_letters_unresolved` | int | Of those, unresolved. |
| `indexer.checkpoint_block` | int | |
| `indexer.head_block` | int \| null | `null` when the node could not be read. |
| `indexer.lag_blocks` | int \| null | **`null`, not zero**, when the head is unknown. Zero means caught up. |
| `indexer.detail` | string \| null | Why it is unknown, when it is. |
| `quotas` | object[] | `{name, used, budget, used_percent, period_start}`. `used` and `budget` are 4dp decimal **strings**. |
| `scheduler_enabled` | bool | |
| `scheduler_running` | bool | |
| `jobs` | object[] | `{id, next_run_at, last_run_at}` |

```json
{
  "observed_at": "2026-08-28T12:03:01.857041Z",
  "app_env": "local",
  "chain": {
    "mode": "postgres_only",
    "contract_address": "0x0000000000000000000000000000000000000000",
    "chain_id": 80002,
    "write_enabled": false,
    "signer_configured": false,
    "contract_deployed": false,
    "rpc_available": false
  },
  "outbox": [
    { "job_type": "ANCHOR_ITEM", "status": "QUEUED", "count": 5 },
    { "job_type": "ANCHOR_ATTESTATION", "status": "QUEUED", "count": 1 }
  ],
  "outbox_total": 6,
  "dead_letters": 0,
  "dead_letters_unresolved": 0,
  "indexer": {
    "checkpoint_block": 0,
    "head_block": null,
    "lag_blocks": null,
    "detail": "no chain runtime in this process; the indexer is not running here"
  },
  "quotas": [
    { "name": "media_blob_bytes", "used": "76.0000", "budget": "268435456.0000", "used_percent": 0.0, "period_start": "1970-01-01T00:00:00.000000Z" },
    { "name": "pinata_storage_bytes", "used": "76.0000", "budget": "1073741824.0000", "used_percent": 0.0, "period_start": "1970-01-01T00:00:00.000000Z" }
  ],
  "scheduler_enabled": true,
  "scheduler_running": false,
  "jobs": []
}
```

**Errors**

| Code | Status | Trigger |
|---|---|---|
| `UNAUTHENTICATED` / `TOKEN_EXPIRED` / `TOKEN_INVALID` / `ACCOUNT_SUSPENDED` | 401 | Credential problems. |
| `INSUFFICIENT_ROLE` | 403 | Caller is not `ADMIN`. |
| `SERVICE_UNAVAILABLE` | 503 | Database unreachable. |

---

## 4. Error codes

Every code the system can emit, alphabetical. Sourced from `app/core/errors.py`.
The enum is append-only from this document forward: no code is renamed, and no
code is removed once it has been emitted.

| Code | HTTP | Meaning | Emitted by |
|---|---|---|---|
| `ACCOUNT_SUSPENDED` | 401 on a bearer route, 403 on login | The account is `SUSPENDED`. On any bearer route the token is refused before the body runs; on `/auth/login` it is refused after the password verifies. | every `bearer` route; `POST /auth/login` |
| `ACTOR_FRAUD_FLAGGED` | 403 | A fraud-flagged actor attempted to attest. Well-formed and colliding with nothing — simply not permitted. | `POST /items/{item_id}/attestations` |
| `ATTRIBUTE_VALIDATION_FAILED` | 422 | Item attributes do not satisfy the category's JSON Schema. `details.errors` is `[{path, message}]`. | `POST /categories/{slug}/validate`, `POST /items`, `POST /items/{item_id}/split` |
| `BULK_TOO_LARGE` | 422 | More than 500 ids in a bulk tag request. Nothing is written. | `POST /admin/tags/bulk` |
| `CATEGORY_NOT_FOUND` | 404 | No category with that slug, or the category is retired. | `GET /categories/{slug}`, `GET /categories/{slug}/versions`, `GET /categories/{slug}/v/{version}`, `POST /categories/{slug}/validate`, `POST /admin/categories/{slug}/versions`, `PATCH /admin/categories/{slug}`, `POST /items`, `POST /items/{item_id}/split` |
| `CATEGORY_RETIRED` | 422 | The category is inactive and no pinned version was requested. | `POST /categories/{slug}/validate`, `POST /items`, `POST /items/{item_id}/split` |
| `CATEGORY_SLUG_EXISTS` | 409 | A category already holds this slug. Publish a version instead. | `POST /admin/categories` |
| `CATEGORY_VERSION_NOT_FOUND` | 404 | The slug exists but not at that version. | `GET /categories/{slug}/v/{version}`, `POST /categories/{slug}/validate` |
| `CHAIN_UNAVAILABLE` | 503 | A chain operation could not be served — RPC unreachable, or the compute-unit budget is spent with no cached value to fall back on. Raised inside the chain client and the background workers; no HTTP route in this API propagates it to a caller today, because every route that touches the chain treats an unreachable node as data. | `app.chain.client` |
| `CONFLICT` | 409 | Generic collision. Used where the specific case needs no code of its own. | `POST /items/{item_id}/media` (already linked) |
| `DUPLICATE_ATTESTATION` | 409 | This actor has already attested to this item. Raised by a database constraint, not by a check-then-write. | `POST /items/{item_id}/attestations` |
| `EMAIL_ALREADY_REGISTERED` | 409 | An account already holds this email, case-insensitively. | `POST /auth/register`, `POST /auth/oauth/complete` |
| `FORBIDDEN` | 403 | Authenticated, but not permitted to act on this particular object. Ownership, not role. | `POST /items/{item_id}/tag`, `POST /items/{item_id}/media`, `DELETE /items/{item_id}/media/{media_id}` |
| `IDEMPOTENCY_KEY_REUSED` | 409 | The key was already used for a **different** request body. The new request is not performed. `details.key` is present. | `POST /items`, `POST /items/{item_id}/split`, `POST /items/{item_id}/tag`, `POST /admin/categories`, `POST /admin/categories/{slug}/versions` |
| `INSUFFICIENT_ROLE` | 403 | The caller's role does not permit this operation at all. `details.required` lists the roles that do. | every `bearer + role(X)` route |
| `INTERNAL_ERROR` | 500 | An unhandled exception. The body carries no detail — an unexpected exception is exactly where a message is most likely to contain something private. | any route |
| `INVALID_CATEGORY_SCHEMA` | 422 | `attribute_schema` is not a valid JSON Schema Draft 2020-12 document. An operator error at category creation, deliberately distinct from `ATTRIBUTE_VALIDATION_FAILED`, which is a weaver error at registration. | `POST /admin/categories`, `POST /admin/categories/{slug}/versions` |
| `INVALID_CREDENTIALS` | 401 | Unknown email, wrong password, or an OAuth-only account. Indistinguishable in wording and in timing. | `POST /auth/login` |
| `INVALID_CURSOR` | 422 | A pagination cursor is malformed, truncated or wrongly signed. All three are indistinguishable, so a forged cursor leaks nothing. | `GET /items`, `GET /items/{item_id}/attestations` |
| `INVALID_REFRESH_TOKEN` | 401 | No refresh token was presented, or it matches no record. | `POST /auth/refresh` |
| `INVALID_TAG_CODE` | 400 on the public route, 422 internally | A tag code fails length, alphabet or check-symbol validation. Answered before any database query. | `GET /v/{tag_code}`, `POST /v/{tag_code}/scan` |
| `ITEM_NOT_FOUND` | 404 | No item with that id. | `GET /items/{item_id}` and every route beneath it; `POST /admin/tags/bulk` per-result |
| `MASS_BALANCE_EXCEEDED` | 409 | A split or a child registration would allocate more than the parent holds. Well-formed, so 409 rather than 422. `details` carries the arithmetic. | `POST /items`, `POST /items/{item_id}/split` |
| `MAX_DEPTH_EXCEEDED` | 422 | The resulting item would sit deeper than 5 levels. | `POST /items`, `POST /items/{item_id}/split` |
| `MEDIA_TOO_LARGE` | 413 | The upload exceeds 5 MiB. Detected while streaming, so the whole file is never buffered. `details.limit_bytes` is present. | `POST /media` |
| `NOT_FOUND` | 404 | Generic absence, where the resource kind needs no code of its own. | `GET /v/{tag_code}`, `POST /v/{tag_code}/scan`, `GET /media/{media_id}`, `GET /media/{media_id}/raw`, `GET /items/{item_id}/tag/qr`, `DELETE /items/{item_id}/media/{media_id}` |
| `OAUTH_IDENTITY_LINKED` | 409 | A different Google subject presented the verified email of an already-linked account — the recycled-address case. | `GET /auth/oauth/google/callback`, `POST /auth/oauth/complete` |
| `OAUTH_PROVIDER_UNAVAILABLE` | 503 | Google is not configured, or its token or JWKS endpoint failed. Not an outage of this service. | `GET /auth/oauth/google/start`, `GET /auth/oauth/google/callback` |
| `OAUTH_STATE_INVALID` | 400 | The OAuth `state` is missing, forged, expired, or its nonce has already been spent. | `GET /auth/oauth/google/callback` |
| `PENDING_TOKEN_CONSUMED` | 401 | The pending sign-up token has already been spent. Exactly one of two concurrent completions wins. | `POST /auth/oauth/complete` |
| `PROVIDER_EMAIL_UNVERIFIED` | 400 | Google did not assert that the email is verified. 400 rather than 401 because nothing was wrong with the credential — the request simply can never succeed. | `GET /auth/oauth/google/callback` |
| `QUANTITY_UNIT_MISMATCH` | 422 | `quantity_unit` is not the unit the category is measured in. `details` carries `expected` and `received`. | `POST /items` |
| `QUOTA_EXCEEDED` | 429 | A metered external budget would be crossed by this operation, and the operation is refused rather than recorded. Distinct from `RATE_LIMITED`: a different ceiling, the same "come back later". Raised by `QuotaTracker.consume(strict=True)`; no HTTP route enables strict mode today. | `app.core.quota` |
| `RATE_LIMITED` | 429 | A rate limiter was exceeded. `Retry-After` header and `details.retry_after` carry whole seconds; `details` also has `scope`, `limit` and `window_seconds`. | every rate-limited endpoint in §1.9 |
| `REFRESH_TOKEN_EXPIRED` | 401 | The refresh token is past its 30-day life. | `POST /auth/refresh` |
| `REFRESH_TOKEN_REUSED` | 401 | An already-rotated refresh token was presented. The whole family is revoked, including the successor just issued. | `POST /auth/refresh` |
| `ROLE_NOT_SELF_ASSIGNABLE` | 403 | A role outside `{CONSUMER, WEAVER}` was requested at sign-up. `details.role` is present. | `POST /auth/register`, `POST /auth/oauth/complete` |
| `SERVICE_UNAVAILABLE` | 503 | A dependency this request needed is down. In practice: PostgreSQL is unreachable, and the request was **not** processed. The message is fixed and never quotes the exception, because a connection failure quotes the DSN and the DSN contains a password. | any route |
| `STORAGE_BUDGET_EXCEEDED` | 507 | A storage budget is exhausted. **Deliberately not 429**: a rate limit says "come back shortly", this says "the space is gone until somebody frees it", and a client that backs off and retries would hammer an endpoint that cannot succeed. | `POST /media` |
| `TAG_ALREADY_ISSUED` | 409 | The item already wears a tag. `details` carries `tag_code`, `display_code` and `payload_url` — there is nothing to retry, only something to read. | `POST /items/{item_id}/tag`, and as a per-result `reason_code` in `POST /admin/tags/bulk` |
| `TAG_GENERATION_EXHAUSTED` | 500 | Every generation attempt collided. At ~53 bits of entropy this cannot happen by chance, so it is reported as a system fault rather than as the caller's problem. | `POST /items/{item_id}/tag` |
| `TAG_NOT_ISSUABLE` | 422 | The item's anchoring failed, so it has no recorded provenance to tag. Distinct from a malformed request: an operator standing at a printer needs to know which it was. | `POST /items/{item_id}/tag` |
| `TOKEN_EXPIRED` | 401 | An access token or a pending token is past `exp`. | every `bearer` route; `POST /auth/oauth/complete` |
| `TOKEN_INVALID` | 401 | Bad signature, wrong algorithm, wrong audience, wrong issuer, malformed, or a subject that no longer exists. A pending token presented as a bearer token lands here. | every `bearer` route; `POST /auth/oauth/complete` |
| `UNAUTHENTICATED` | 401 | No `Authorization` header, or a non-bearer scheme. Never 403 — answering 403 would concede that the credential identified somebody. | every `bearer` route |
| `UNSUPPORTED_MEDIA_TYPE` | 415 | The uploaded bytes are not jpeg, png, webp or mp4. The client's declared content type is ignored entirely. `details.allowed` lists the four. | `POST /media` |
| `USER_NOT_FOUND` | 404 | No user with that id. | `POST /admin/actors/{user_id}/fraud-flag`, `POST /admin/actors/{user_id}/fraud-clear` |
| `VALIDATION_FAILED` | 422 | The request body or query fails schema validation, or a required header is absent. `details` is a list of `{loc, msg, type}` for body validation, and `null` for a missing `Idempotency-Key`. | any route with a body or typed query |

### 4.1 Reachability

Every code above is raised somewhere in `app/` and triggered by at least one
test. The mapping:

| Code | Test that triggers it |
|---|---|
| `ACCOUNT_SUSPENDED` | `tests/integration/test_auth_password.py` |
| `ACTOR_FRAUD_FLAGGED` | `tests/integration/test_attestation_api.py` |
| `ATTRIBUTE_VALIDATION_FAILED` | `tests/integration/test_catalog.py`, `test_provenance.py`, `test_live_category_add.py` |
| `BULK_TOO_LARGE` | `tests/integration/test_qr_issuance.py` |
| `CATEGORY_NOT_FOUND` | `tests/integration/test_catalog.py` |
| `CATEGORY_RETIRED` | `tests/integration/test_catalog.py` |
| `CATEGORY_SLUG_EXISTS` | `tests/integration/test_catalog.py` |
| `CATEGORY_VERSION_NOT_FOUND` | `tests/integration/test_catalog.py` |
| `CHAIN_UNAVAILABLE` | `tests/integration/test_chain_writer.py` (`TestQuotaCeiling`, asserting `status == 503`) |
| `CONFLICT` | `tests/integration/test_media_api.py`, `test_ratelimit.py` |
| `DUPLICATE_ATTESTATION` | `tests/integration/test_attestation_api.py`, `test_attestation_trust.py`, `test_concurrency.py` |
| `EMAIL_ALREADY_REGISTERED` | `tests/integration/test_auth_password.py` |
| `FORBIDDEN` | `tests/integration/test_qr_issuance.py` |
| `IDEMPOTENCY_KEY_REUSED` | `tests/integration/test_idempotency.py`, `test_catalog.py`, `test_concurrency.py` |
| `INSUFFICIENT_ROLE` | `tests/integration/test_admin_status.py`, `test_attestation_api.py`, `test_catalog.py` |
| `INTERNAL_ERROR` | `tests/integration/test_provenance.py` (`TestAtomicity`, an injected failure after the item insert) |
| `INVALID_CATEGORY_SCHEMA` | `tests/integration/test_catalog.py` |
| `INVALID_CREDENTIALS` | `tests/integration/test_auth_password.py` |
| `INVALID_CURSOR` | `tests/unit/test_pagination.py` |
| `INVALID_REFRESH_TOKEN` | `tests/integration/test_auth_password.py` |
| `INVALID_TAG_CODE` | `tests/integration/test_public_verification.py`, `test_qr_issuance.py`, `test_failure_matrix.py` |
| `ITEM_NOT_FOUND` | `tests/integration/test_provenance.py`, `test_qr_issuance.py` |
| `MASS_BALANCE_EXCEEDED` | `tests/integration/test_provenance.py`, `test_concurrency.py` |
| `MAX_DEPTH_EXCEEDED` | `tests/integration/test_provenance.py` |
| `MEDIA_TOO_LARGE` | `tests/integration/test_media_upload.py` |
| `NOT_FOUND` | `tests/integration/test_public_verification.py`, `test_failure_matrix.py` |
| `OAUTH_IDENTITY_LINKED` | `tests/integration/test_auth_oauth.py` |
| `OAUTH_PROVIDER_UNAVAILABLE` | `tests/integration/test_auth_oauth.py`, `test_failure_matrix.py` |
| `OAUTH_STATE_INVALID` | `tests/integration/test_auth_oauth.py` |
| `PENDING_TOKEN_CONSUMED` | `tests/integration/test_auth_oauth.py`, `test_concurrency.py` |
| `PROVIDER_EMAIL_UNVERIFIED` | `tests/integration/test_auth_oauth.py` |
| `QUANTITY_UNIT_MISMATCH` | `tests/integration/test_provenance.py` |
| `QUOTA_EXCEEDED` | `tests/integration/test_quota.py` (`TestStrictConsumption`) |
| `RATE_LIMITED` | `tests/integration/test_ratelimit.py`, `test_auth_password.py`, `test_public_verification.py` |
| `REFRESH_TOKEN_EXPIRED` | `tests/integration/test_auth_password.py` |
| `REFRESH_TOKEN_REUSED` | `tests/integration/test_auth_password.py`, `test_concurrency.py` |
| `ROLE_NOT_SELF_ASSIGNABLE` | `tests/integration/test_auth_password.py`, `test_auth_oauth.py`, `test_no_admin_escalation.py` |
| `SERVICE_UNAVAILABLE` | `tests/integration/test_failure_matrix.py` (`TestPostgresDown`) |
| `STORAGE_BUDGET_EXCEEDED` | `tests/integration/test_media_upload.py`, `test_failure_matrix.py` |
| `TAG_ALREADY_ISSUED` | `tests/integration/test_qr_issuance.py`, `test_concurrency.py` |
| `TAG_GENERATION_EXHAUSTED` | `tests/integration/test_qr_issuance.py` |
| `TAG_NOT_ISSUABLE` | `tests/integration/test_qr_issuance.py` |
| `TOKEN_EXPIRED` | `tests/integration/test_auth_password.py`, `test_auth_oauth.py` |
| `TOKEN_INVALID` | `tests/integration/test_auth_password.py` |
| `UNAUTHENTICATED` | `tests/integration/test_security_sweep.py`, `test_admin_status.py`, `test_failure_matrix.py` |
| `UNSUPPORTED_MEDIA_TYPE` | `tests/integration/test_media_api.py`, `test_media_upload.py` |
| `USER_NOT_FOUND` | `tests/integration/test_attestation_reputation.py` |
| `VALIDATION_FAILED` | `tests/integration/test_qr_issuance.py` and every schema-validation case |

**Two codes are raised only below the HTTP layer**, and both are documented above
with that noted in place. `CHAIN_UNAVAILABLE` is raised inside the chain client
and caught by the workers — every HTTP route that touches the chain treats an
unreachable node as data rather than as an error, so a client will not see it
today. `QUOTA_EXCEEDED` is raised by `QuotaTracker.consume(strict=True)`, and no
route enables strict mode; the media path records consumption and lets
`STORAGE_BUDGET_EXCEEDED` do the refusing. Both are tested at the layer that
raises them, and both stay in the enum because the code paths are live and a
future route can reach them.

**Six codes were deleted in this phase** rather than documented, because nothing
in `app/` ever raised them and no client could have branched on them:
`SCHEMA_VIOLATION`, `TOKEN_REUSED`, `ACCOUNT_NOT_VERIFIED`, `ITEM_ALREADY_CLAIMED`,
`TAG_CODE_TAKEN` and `PINNING_UNAVAILABLE`. `ITEM_ALREADY_CLAIMED` was superseded
by the non-error claim design in §5.5; `TAG_CODE_TAKEN` by the SAVEPOINT retry
that never lets a generator collision reach a client. A documented code that
nothing produces is worse than an absent one — a frontend writes a handler for it
and that handler is dead the day it is written.

---

## 5. Domain semantics

The part a frontend developer cannot infer from the schemas.

### 5.1 Item status, and why everything is `PENDING`

`status` on an item is one of three values:

| Value | Meaning |
|---|---|
| `PENDING` | The hash is recorded in PostgreSQL and queued for anchoring. It is not on chain, or it is on chain without enough confirmations yet. |
| `CONFIRMED` | The anchoring transaction has been mined **and has `CHAIN_CONFIRMATIONS` blocks on top of it** (3 by default). Only then. |
| `FAILED` | The anchoring transaction reverted, or the job exhausted its retries. Tags cannot be issued for a `FAILED` item. |

> **`PENDING` is the current, correct, expected state of every item in this
> deployment, and it is not a bug.** `CONTRACT_ADDRESS` is the zero address,
> `CHAIN_WRITE_ENABLED` is false, and no relayer key is configured. Records are
> real; anchors are not yet. `GET {API_PREFIX}/admin/system/status` reports
> `chain.mode: "postgres_only"` and says so plainly.
>
> **Do not render `PENDING` as an error, a warning, or a spinner that never
> resolves.** Render it as what it is: "recorded, not yet anchored". A frontend
> that shows a red state for the ordinary case will show a red state at every
> demo.

An item can move `PENDING → CONFIRMED` and back to `PENDING`: a chain reorg that
drops the block carrying an anchor emits a `REORGED` event and returns the item
to `PENDING`. Treat status as live, not as a one-way ratchet.

### 5.2 Trust levels

Four values, derived at read time from the attestation set and the dispute set.
**No table stores a trust column, no endpoint assigns one, and no admin can grant
one.** Consequences, all intended: nobody can grant a level; fraud-flagging an
attestor takes effect on the very next read everywhere, with no cache to
invalidate; and the stored data and the displayed level cannot disagree, because
there is only one of them.

| Level | Derived from |
|---|---|
| `SELF_DECLARED` | Only the registrant has vouched — or nobody independent and unflagged has. The floor, and the honest default. |
| `CO_OP_ATTESTED` | At least one independent, `ACTIVE`, unflagged `COOP_OFFICER` has attested. |
| `INSPECTED` | At least one independent, `ACTIVE`, unflagged `INSPECTOR` has attested. Outranks `CO_OP_ATTESTED`. |
| `DISPUTED` | An **override**, not a rung: the item has an open dispute, or any attestor on it is fraud-flagged. It is applied regardless of how many other people vouched. |

What "independent" excludes: the item's own registrant (self-endorsement is
already the claim being made), a second attestation from someone who has already
attested (repetition is not corroboration), a fraud-flagged actor, and an account
still in `PENDING_VERIFICATION` (otherwise the ladder would be self-service —
register, claim `COOP_OFFICER`, attest, look corroborated). A `WEAVER` attesting
to somebody else's item is recorded and shown but **does not raise the level**:
peer endorsement between weavers is not the independent check a co-op or an
inspector represents.

> **The API never returns a binary genuine/fake verdict, and no screen may
> synthesise one.** A chain stores whatever a human typed into it. A weaver, or a
> co-op officer taking a bribe, can register a powerloom piece as handloom and
> the ledger will hold that claim, unaltered, forever. Immutability is a property
> of the record, not of the truth of the record. This system answers **who
> vouched, in what capacity, and how independent they were** — a smaller promise
> than the one weak pitches make, and one it can actually keep.

**Approved display vocabulary:** *verified provenance*, *self-declared*,
*co-op attested*, *inspected*, *disputed*, *recorded*, *anchored*, *unanchored*,
*claimed*, *already claimed*.

**Never use, anywhere in any surface:** *genuine*, *authentic*, *real*, *fake*,
*counterfeit*, *counterfeit-proof*, *verified authentic*, *guaranteed*, or any
phrasing that asserts the object is what it claims to be. A CI grep enforces this
against the backend; the frontend is expected to hold the same line.

### 5.3 Verification result

`chain.verification` on the public payload is one of three values. It is the
outcome of recomputing the item's hash from the database and comparing it to what
was anchored.

| Value | What happened | What to show |
|---|---|---|
| `MATCH` | The hash recomputed from the record right now equals the anchored value (or, for a batched item, is provably a leaf under the anchored Merkle root). | The record has not changed since it was anchored. Show the anchor, the block, the confirmations. |
| `MISMATCH` | Something was anchored for this item, and the database no longer produces that hash. The record changed after it was written. | Say so plainly and prominently. This is the one signal in the whole system that means somebody with database access altered a record. It is not an accusation about the object. |
| `UNANCHORED` | Nothing has been anchored for this item, so there is nothing to compare against. | **Not a failure.** The ordinary state of a new record and, today, of every record. Show "recorded, not yet anchored". |

`stale` is a separate boolean and it means: *this answer did not come from a live
chain call this request.* It is `true` when there is no chain runtime, no
contract binding, no working connection, or the live read threw — in which case
the last indexed state is served instead. Cached data presented as live would be
the one dishonest field in the payload, so it is labelled.

**When `stale` is `true`, label the answer as "last known" and show
`chain_checked_at`**, which then carries the observation time rather than the
request time. Today, with nothing deployed, every response has
`"verification": "UNANCHORED", "stale": true`.

### 5.4 Suspicion levels

`scan.suspicion_level` is one of `NONE`, `WATCH`, `SUSPICIOUS`, computed from
four rules over the tag's whole scan history.

| Signal | Fires when |
|---|---|
| `GEOGRAPHIC_SPREAD` | Scans in more than 3 distinct regions. |
| `IMPOSSIBLE_VELOCITY` | Two consecutive located scans imply travel faster than 900 km/h. |
| `VOLUME` | More than 50 scans in total. |
| `DEVICE_DIVERSITY` | More than 5 distinct devices in the last 60 minutes. |

`NONE` when no rule fires; `WATCH` for one rule; `SUSPICIOUS` for two or more, or
for `IMPOSSIBLE_VELOCITY` alone. Velocity is promoted on its own because it is
the one signal with no innocent reading: the other three describe a tag being
looked at a lot, which a shop window produces honestly. Two places at once
describes two objects.

`scan.reason` is a sentence written for a person holding the object, and
`scan.signals` is the machine-readable list of codes that fired.

> **These are advisory and never accusatory. Nothing here blocks anything.** A
> saree bought in Gujarat and carried to Assam is an ordinary gift. A retail
> display gets scanned by dozens of people who are doing nothing wrong. Show the
> level and the reason as information the reader can weigh; do not phrase them as
> a finding against the object or against whoever is holding it.

The inputs are a coarse region code, a hashed device fingerprint and a hashed
network address. There is no GPS, no city, no coordinates finer than a state
centroid, and no raw address in the table these read from.

### 5.5 Claim semantics

**First scan wins, and the first claim is never overwritten** — not by a later
scan, not by an admin, not by a retry. `claims.item_id` is a primary key, so the
rule is enforced by PostgreSQL rather than by an application check that could
lose a race. Two shoppers scanning the same tag at the same instant is a shelf in
a shop, not a hypothetical.

`claim.status` is one of:

| Value | Meaning |
|---|---|
| `UNCLAIMED` | Nobody has claimed this object. Only ever returned by a read (`GET`), or by a scan that supplied no device fingerprint. |
| `CLAIMED` | **This device** holds the claim — it either just made it or made it earlier. `is_your_claim` is `true`. |
| `ALREADY_CLAIMED` | A different device claimed it first. `is_your_claim` is `false` and `message` is populated. |

`is_your_claim` compares the request's salted fingerprint hash against the stored
one. **Two absent fingerprints are not the same device**; they are two absences,
and a claim is never awarded on that basis. A scan carrying no fingerprint at all
does not claim: binding an object to "whoever this was" is worse than leaving it
unclaimed, because the next person to scan would be told somebody else owns it
with no way to tell whether that was true.

**What the second scanner sees** is exactly this, and the wording is a product
decision rather than politeness:

```
This tag was already claimed in IN-GJ on 28 August 2026 by the first device that scanned it. If you did not expect that, ask the seller you bought this from about it.
```

One sentence of fact, one of advice, and **nothing about the object.** Render
`claim.message` as it is returned. Do not add "possible counterfeit", "duplicate
tag", or any framing that tells the second scanner what it means — a retail
display gets scanned by dozens of people who are not doing anything wrong, a
person who scans their own object twice has done nothing at all, and a system
that tells the second scanner they are holding something illegitimate will be
wrong far more often than it is right, in public, to the one customer who cared
enough to check. Say what happened; let the person and their seller work out what
it means.

This is also the direct answer to the peeled-QR scenario: the tag is unaltered
and the code is correct, so cryptography cannot see the substitution. What is
visible is that one tag was claimed once, in one place, at one time — and the
scan pattern beside it.

### 5.6 Media resolution tiers

`tiers` on a media payload is ordered **best first**, and a client should walk it
in order:

1. **`IPFS`** — the Pinata gateway URL. Fastest and CDN-backed, and the most
   likely to be dead: a pin is only as durable as whoever is paying for it.
   `durable: false`.
2. **`MIRROR`** — `{API_PREFIX}/media/{id}/raw?tier=MIRROR` on this service, served
   from the local filesystem. **Wiped by every redeploy** on an ephemeral
   filesystem. `durable: false`.
3. **`BLOB`** — `{API_PREFIX}/media/{id}/raw?tier=BLOB`, served from a PostgreSQL
   blob. `durable: true`.

> **Only the database blob tier survives a Render redeploy.** The IPFS pin
> depends on a third party and the local mirror is wiped. Only files at or below
> 2 MiB get a blob copy, so a larger file has `durable: false` on every tier.

Implement the fallback client-side: an `<img>` that fails on the gateway URL
should retry the mirror URL and then the blob URL. The whole chain is handed over
up front precisely so a failed image load costs no round trip.

`tier` on `/raw` is advisory. Requesting `MIRROR` after a redeploy still falls
back to the blob rather than 404ing on a tier that used to exist.

`sha256` is the integrity claim and is true whether or not anybody is currently
hosting the bytes. `cid` and `gateway_url` are `null` until a pin succeeds, and
that is a normal steady state, not an error.

### 5.7 Tag and QR payload

**The QR encodes exactly one string and nothing else:**

```
{PUBLIC_BASE_URL}/v/{TAG_CODE}
```

For example: `http://localhost:3000/v/86RG4H4BGJ55`.

* No authentication. No query parameters. No tracking identifiers. No item id.
* `PUBLIC_BASE_URL` is the **frontend** origin, so a scan opens the frontend's page. A printed URL outlives every deployment decision, and pointing it at the backend would put a free-tier cold start between a shopper and their answer.
* The path is `/v/` rather than `/verify/` because every character costs modules, and fewer modules means a coarser grid that survives a crease.
* The code in the payload is the **canonical** form: uppercase, no separators.

Rendering: error correction level **Q** (~25% recoverable), a four-module quiet
zone, black on white. PNG modules are scaled by a whole number of pixels with the
remainder added to the margin, never resampled — a half-pixel module edge is how
a code that decodes on a screen fails on paper. SVG carries a `viewBox` in module
units and an `aria-label` naming the display code.

Images are `Cache-Control: public, max-age=31536000, immutable`, because a tag
code is immutable once bound and the payload is a pure function of it. Cache them
as aggressively as you like.

---

## 6. Versioning

**This contract is frozen as of this document.** The rules below apply from here
forward and are themselves part of the contract.

**Additive changes do not bump the version.** A new optional request field, a new
response field, a new endpoint, a new enum member on a field already documented as
an open set, a new error code — none of these require a new `{API_PREFIX}`.
Clients must therefore be written to tolerate them: **ignore unknown response
fields rather than failing on them**, and treat an unrecognised error `code` as a
generic failure of its HTTP status class rather than crashing.

**Breaking changes require a new `{API_PREFIX}` version** — `/api/v2` alongside
`/api/v1`, not in place of it. Breaking means any of:

* removing a response field, or making a non-nullable one nullable;
* changing a field's type, including string-to-number on a decimal;
* changing the meaning of an existing value or enum member;
* adding a required request field, or tightening an existing constraint;
* changing an endpoint's path, method, or success status code;
* changing which role or credential an endpoint requires.

**The public path is exempt from versioning and can never be versioned.**
`/v/{tag_code}` is printed on cloth. It has no version segment, it will not
acquire one, and a change to the payload it returns must be additive forever. If
the public payload ever needs a breaking change, the answer is a new field beside
the old one, not a new path.

**Error codes are append-only from this document.** No code documented in
section 4 will be renamed or removed once it has been emitted. New failure modes
get new codes.

The frozen `item_hash` preimage is not part of this HTTP contract and is stricter
still: it cannot change at any version, because a chain cannot be rewritten.

---

*Generated from the implementation at application version `0.1.0`. Enforced by
`tests/contract/test_contract_matches_implementation.py`.*
