# Sutradhar backend — what exists and why

A map of the whole service, written for somebody who has to work on it and has
not read the thirteen phase briefs that produced it.

Three documents, three jobs. This one is the architecture. [`README.md`](../README.md)
is how to run it. [`API_CONTRACT.md`](API_CONTRACT.md) is the frozen wire
contract a frontend consumes. [`phase-12-findings.md`](phase-12-findings.md) is
the hardening pass and its fourteen bugs.

---

## 1. What the system claims

**It proves who claimed what, and when. It does not prove that a physical object
is what somebody says it is.**

That sentence is the whole design constraint, and it is unusual enough to state
before anything else. A blockchain stores whatever a human typed into it. A
weaver, or a co-op officer taking a bribe, can register a powerloom saree as
handloom, and the ledger will hold that claim, unaltered, forever. Immutability
is a property of the record, not of the truth of the record. Every provenance
system that presents an anchored hash as evidence of authenticity has quietly
substituted one for the other.

So this service answers a smaller question it can actually answer: **who vouched
for this record, in what capacity, how independent were they, and has anyone
contested it.** No endpoint returns a genuine/fake verdict, no column stores one,
and `tests/unit/test_no_verdict_language.py` greps the source tree to keep it
that way.

The approved vocabulary is *verified provenance*, *self-declared*, *co-op
attested*, *inspected*, *disputed*. The banned vocabulary is *genuine*,
*authentic*, *counterfeit-proof*, and anything else that asserts a fact about
the object rather than about the record.

---

## 2. Shape and size

| | |
|---|---|
| Application | 108 Python files, ~20,000 lines under `app/` |
| Tests | 62 files, ~20,400 lines. **1,106 exist; 1,097 run by default** — the 9 load tests are deselected by `pytest.ini` |
| HTTP surface | 44 operations |
| Error codes | 48, all reachable and all covered by a test |
| Database | 26 tables, 14 native PostgreSQL enum types, 10 Alembic migrations |
| Background jobs | 5, on APScheduler inside the FastAPI lifespan |
| Solidity | one contract, `contracts/src/Sutradhar.sol` |

Test code and application code are roughly the same size. That ratio is
deliberate for a system whose whole proposition is that its records can be
trusted.

### Stack

FastAPI · PostgreSQL 16+ (asyncpg, SQLAlchemy 2 async) · Alembic · Pydantic v2 ·
argon2-cffi · PyJWT (Ed25519) · Authlib · web3.py / eth-account · Hardhat (compile
and local node only) · Pinata / IPFS · APScheduler · structlog · pytest.

### Constraints that shaped it

* **No Docker, anywhere.** Native PostgreSQL, native Node for the local EVM node.
* **Free tier only.** One Render instance, one Neon database, a Pinata free plan, an Alchemy compute-unit budget. Several designs below exist because there is no Redis and no second process.
* **Google is the only OAuth provider.** No second provider is configured or planned.
* **Backend-only repository.** A sibling `frontend/` is a separate concern and consumes `API_CONTRACT.md`.

---

## 3. Layout

```
backend/
├── app/
│   ├── main.py            application factory, middleware, lifespan
│   ├── config.py          every environment knob, validated at import
│   ├── core/              cross-cutting primitives (15 files, ~1,700 lines)
│   ├── db/                engine, session, ORM models (15 files, ~1,600 lines)
│   ├── api/               health probes and the aggregate router
│   ├── auth/              password auth, sessions, roles (18 files, ~2,400 lines)
│   │   └── oauth/         Google authorization-code flow
│   ├── catalog/           GI categories and their versioned JSON Schemas
│   ├── provenance/        items, splits, the frozen item hash
│   ├── attestation/       attestations, derived trust, fraud propagation
│   ├── chain/             EVM client, outbox, writer, indexer (12 files, ~4,500 lines)
│   ├── media/             upload pipeline, three-tier storage, IPFS
│   ├── qr/                tag issuance and QR rendering
│   ├── verification/      the public surface (7 files, ~1,900 lines)
│   ├── admin/             the operator status endpoint
│   └── workers/           APScheduler lifecycle and the five jobs
├── alembic/versions/      10 migrations
├── contracts/             Solidity + Hardhat
├── scripts/               bootstrap, admin creation, key generation, deploy, replay
├── seeds/                 3 GI categories, seed users, a seed item tree
├── docs/                  this file, the API contract, the Phase 12 findings
└── tests/                 unit · integration · load · contract · fakes
```

The `chain/` package is the largest single thing here, and that is proportionate:
it is the only part that talks to something it does not control.

---

## 4. Request lifecycle

```
request
  └─ CORSMiddleware                 allowlisted origins; exposes X-Request-ID, ETag, X-Scan-Recorded
     └─ RequestContextMiddleware    assigns a request id, binds it to structlog
        └─ access_log middleware    one structured line per request, with user_id when authenticated
           └─ route dependencies
              ├─ rate_limit(...)    own session, own transaction — see §7
              ├─ get_current_user   bearer token → User, or 401
              └─ get_session        one AsyncSession per request
                 └─ route body
```

**Errors** funnel through five handlers registered in `app/core/error_handlers.py`,
matched along the exception MRO: `AppError` (the taxonomy), `RequestValidationError`
(Pydantic), `StarletteHTTPException`, connection-level database errors, and a
catch-all. Every one produces the same envelope:

```json
{"error": {"code": "...", "message": "...", "details": null, "request_id": "..."}}
```

An unhandled exception logs a full traceback server-side and returns an opaque
`INTERNAL_ERROR` — an unexpected exception is exactly the case where the message
is most likely to contain something private.

**Three routers are mounted, at three different places**, and the difference
matters:

| Mount | Why |
|---|---|
| `/healthz`, `/readyz` | unprefixed, so an orchestrator never tracks an API version |
| `/v/{tag_code}` | unprefixed, because **this path is printed on cloth** and cannot ever be migrated |
| `/api/v1/...` | everything else |

---

## 5. The core primitives

`app/core/` is what everything else is built on. Each module has exactly one job.

| Module | Job | The decision worth knowing |
|---|---|---|
| `clock.py` | the only source of "now" | Nothing else may call `datetime.now`; a CI grep enforces it, and tests monkeypatch one function. `UtcDatetime` makes every response timestamp RFC 3339 with exactly six fractional digits. |
| `ids.py` | UUIDv7 ids, 12-char tag codes | Tag alphabet drops A, E, I, L, O, U and Z — 29 symbols, no letter that can be misread as another and none that can spell a word on a label. The 12th character is a mod-29 check symbol, and 29 being both prime and the alphabet size is what makes every adjacent transposition detectable. |
| `canonical.py` | RFC 8785 JCS | Sorts object keys, so a Solidity verifier, a Python reader and a JS client hash identically without agreeing on an ordering first. |
| `hashing.py` | keccak256 and SHA-256 | One place, so nothing invents a second digest convention. |
| `merkle.py` | sorted-pair keccak256 tree | Sorted pairs mean a proof carries no left/right bits. |
| `crypto_shred.py` | per-subject salted identity hashes | **The DPDP Act 2023 erasure mechanism.** The chain cannot forget; deleting a subject's `identity_salt` makes every anchored hash permanently unlinkable to them. |
| `errors.py` | the 48-code taxonomy | Codes are the public contract. Clients branch on them; messages are not stable. |
| `pagination.py` | signed keyset cursors | HMAC-signed so a client cannot hand-craft one to probe rows. No `OFFSET`, and deliberately **no total count** — `COUNT(*)` on PostgreSQL is a full scan, and the fastest-growing table is the one that would most want one. |
| `idempotency.py` | replay on `(user_id, key)` | A single `INSERT … ON CONFLICT DO NOTHING RETURNING`, not read-then-insert. Uses the *caller's* session, so the stored response commits atomically with the write it describes. |
| `ratelimit.py` | fixed windows in Postgres | No Redis: one free instance, and the database is already a hard dependency. Counted in its **own** transaction, so a request that rolls back still spent its allowance. |
| `quota.py` | budgets for metered services | Periodic (Alchemy compute units, monthly) and cumulative (Pinata bytes, pinned to the epoch). |
| `logging.py` | structlog, request correlation, redaction | An outbound redactor, because the cheapest way to leak a token is to log it. |

---

## 6. Data model

26 tables. The ones that carry the design:

**Identity.** `users`, `oauth_identities`, `refresh_tokens`, `pending_tokens`,
`auth_events`, `oauth_states`. Passwords are argon2id. Refresh tokens are stored
only as SHA-256. `auth_events` is append-only.

**Catalogue.** `gi_categories` holds one row per `(slug, schema_version)` — a
category is versioned, not mutated, and every item pins the version it was
registered under so a later schema change cannot invalidate an existing record.

**Provenance.** `items` is a tree via `parent_id`, capped at depth 5, with
`numeric(18,4)` quantities. `item_events` is the append-only log; each row
carries a `payload_hash`. `item_disputes` records *why* something is contested,
by source, which is what makes a dispute reversible without collateral damage —
clearing a fraud flag lifts the disputes that flag caused and leaves an
inspector's independent finding standing.

**Attestation.** `attestations`, unique on `(item_id, attestor_id)`. The role is
snapshotted at attestation time, because an inspector who later became a consumer
still made that attestation as an inspector.

**Chain.** `outbox`, `chain_txs`, `chain_nonce`, `chain_events`,
`indexer_checkpoints`, `merkle_batches`, `merkle_leaves`.

**Media.** `media` (with an optional `bytea` blob), `item_media`.

**Public.** `scans` and `claims`. `claims.item_id` is a **primary key**, which is
the entire first-scan-wins rule — enforced by PostgreSQL, not by an
`if already_claimed` that could lose a race on a shop shelf.

**Operations.** `rate_limit_buckets`, `idempotency_keys`, `quota_usage`,
`dead_letters`.

Enums are **native PostgreSQL types**, not check constraints or text: the
database refuses an unknown value at write time, and the type is visible in the
schema. They are append-only.

---

## 7. The load-bearing decisions

These are the ones that are non-obvious, and the ones a change is most likely to
break.

### The item hash preimage is frozen

`app/provenance/item_hash.py` defines a v1 preimage — ten fields, canonicalised
per RFC 8785, keccak256'd. **It is a wire format, not an implementation.** Every
hash ever anchored is a digest of that structure, so changing the field set or
any value's encoding makes every prior record permanently unverifiable. There is
no migration, because a chain cannot be rewritten. Hardcoded-digest tests exist
specifically to fail if somebody edits it.

Two encodings inside it are load-bearing: quantity is a **string at exactly 4dp**
(as a JSON number it would pass through a float and `12.0` would eventually hash
as `12.000000000000002`), and timestamps carry **exactly six fractional digits**
(a renderer that trimmed trailing zeros would hash the same instant two ways).

No personally identifying data enters the preimage, ever. The registrant appears
only as a salted digest — see `crypto_shred` above.

### Trust is derived, never stored

No table has a trust column, nothing has a setter, and no endpoint assigns a
level. It is a pure function of the attestation set and the dispute set, computed
on every read. Three consequences, all of them the point: nobody can grant a
level; fraud-flagging an attestor takes effect on the very next read everywhere,
with no cache to invalidate and no backfill job that might not have run; and the
stored data and the displayed level cannot disagree, because there is only one
of them.

What "independent" excludes is the substance: the registrant themselves, a repeat
attestation from the same actor, a fraud-flagged actor, and an account still in
`PENDING_VERIFICATION` — otherwise the ladder would be self-service.

### One job queue, four job types

`outbox` began as a chain-anchoring queue and is now the single mechanism for any
work that must survive a crash: claimed under `FOR UPDATE SKIP LOCKED`, retried
with backoff, dead-lettered with its full error history. `ANCHOR_ITEM`,
`ANCHOR_ATTESTATION`, `ANCHOR_BATCH` and `PIN_MEDIA` all use it. A second retry
loop for pinning would have drifted from the first.

Attestations anchor through **the same** chain function as items, distinguished
by a `kind` field in the preimage. There is no second chain path.

### The database decides races, not the application

A recurring pattern, and each instance is a real bug that was found or prevented:

| Race | Decided by |
|---|---|
| Two tags on one item | `UPDATE … WHERE tag_code IS NULL`; zero rows updated raises 409 |
| Two claims on one object | `claims.item_id` primary key + `ON CONFLICT DO NOTHING` |
| Two attestations by one actor | `uq_attestations_item_attestor` |
| Two retries of one idempotent request | `INSERT … ON CONFLICT DO NOTHING RETURNING` |
| Two OAuth completions of one pending token | `UPDATE … WHERE consumed_at IS NULL` |
| Two workers draining the outbox | `FOR UPDATE SKIP LOCKED` |
| Two splits of one parent | row lock, mass balance checked under it |

### Single instance is asserted, not assumed

The outbox is concurrency-safe, but **nonce allocation and replace-by-fee are
only correct with one scheduler.** Two schedulers would send competing
transactions at the same nonce and each anchor would silently replace the other.
So the scheduler takes a PostgreSQL advisory lock at startup; a second process
registers no jobs and logs why. Render's free tier never scales past one
instance, and this checks that rather than trusting it.

### The public surface is isolated by test, not by habit

`app/verification/` imports nothing from `app.auth`, `app.admin`, or any
authenticated router, and shares no serialiser with them.
`tests/unit/test_verification_isolation.py` asserts it by reading the imports.
The reason is a plausible next move: lifting that package to an edge runtime so a
shopper scanning a tag is not waiting on a free-tier cold start. That move is
cheap only while the boundary holds.

What *is* shared is the frozen hasher, the trust ladder and the Merkle code —
pure derivations where a second copy would drift, and the first symptom of drift
would be the public and private views disagreeing about one object.

---

## 8. The chain layer

`app/chain/` is the largest package and the one with the most failure modes,
because it is the only part talking to something it does not control.

| Module | Job |
|---|---|
| `client.py` | the only door to an EVM JSON-RPC endpoint; retries, caching, compute-unit metering |
| `contract.py` | binding to the registry; **asserts at import** that the compiled ABI really exposes `anchorItem(bytes32,bytes32)`, `anchorBatch(bytes32,uint32)` and both events |
| `nonce.py` | nonce allocation serialised through Postgres rather than read from the node |
| `writer.py` | EIP-1559 fee building, signing, broadcast, replace-by-fee |
| `outbox.py` | claiming, retrying and parking jobs |
| `confirmations.py` | receipt polling, confirmation depth, reorg demotion |
| `indexer.py` | tails contract events into `chain_events` so browsing never reads the chain |
| `batching.py` | many item hashes, one Merkle root, one transaction (built, tested, off by default) |
| `reconcile.py` | diffs the chain against Postgres. **Reports, never heals.** |

The contract itself is small on purpose: `anchorItem`, `anchorBatch`,
`isItemAnchored`, `isBatchAnchored`, `setWriter`, `transferOwnership`. It stores
hashes and emits events. It knows nothing about textiles.

**Nonces come from Postgres, not from the node.** `eth_getTransactionCount`
answers about the node's view of the mempool, which lags and disagrees between
providers; two sends racing that read produce two transactions at one nonce and
one of them silently replaces the other.

**Reconciliation reports and does not correct.** An automatic healer that is
wrong writes bad data confidently. A report that is wrong wastes somebody's
morning. `scripts/replay_chain.py --into-empty` proves the index is a cache and
the chain is the record: it clears `chain_events`, rewinds the indexer to
genesis, re-reads every anchor, and reconciles. Exit `0` means zero drift.

### Background jobs

Five, all inside the FastAPI lifespan, all off under `SCHEDULER_ENABLED=false`:

| Job | Cadence | Does |
|---|---|---|
| outbox drain | `OUTBOX_POLL_SECONDS` (5s) | claims queued anchors and sends them |
| confirmation sweep | `CONFIRMATION_POLL_SECONDS` (15s) | receipts, reorg checks, promotions, fee bumps, nonce gap fills |
| indexer | `INDEXER_POLL_SECONDS` (20s) | tails events into `chain_events` |
| pin retry | `PIN_RETRY_POLL_SECONDS` (120s) | retries `PIN_PENDING` media through the same outbox |
| reconcile | `RECONCILE_CRON` (`*/30 * * * *`) | diffs chain against Postgres |

An unreachable RPC endpoint is a degraded dependency, not a failed boot: the API
serves normally, the outbox fills, and items stay honestly `PENDING`.

---

## 9. Media

IPFS stores nothing by itself. A CID is an address; a pinning service is what
keeps bytes at it, and a lapsed free tier takes them away while the chain still
points at the hash. So the SHA-256 is the integrity proof and the bytes live in
three places:

| Tier | Durable | Notes |
|---|---|---|
| Pinata gateway | no | only while somebody keeps paying |
| local mirror (`media_mirror/`) | **no** | a Render redeploy empties the disk |
| PostgreSQL `bytea` | **yes** | the one that is still there afterwards |

The API hands a client **every** tier at once, best first, so a failed image load
retries client-side with no round trip. Only files at or below 2 MiB get a blob
copy; the blob tier has its own smaller budget, because a full PostgreSQL refuses
every write, not just uploads.

The upload pipeline's step order is the design: stream and bound, sniff by magic
bytes (the client's declared content type is ignored entirely), hash, check
budgets **before** anything is spent, mirror, store, then try to pin. Pinning
failure still returns `201` — the SHA-256 is already committed, and a pinning
service having a bad day is not a reason to reject a weaver's photograph.

Unlinking media never deletes the bytes. The digest may already be anchored, and
deleting bytes behind an anchored hash produces exactly the dead reference the
three tiers exist to prevent.

---

## 10. Tags, QR, and the public page

A tag code is 12 characters of reduced Crockford base32 with a check symbol —
about 53 bits of entropy, so unguessable as well as collision-free at any
realistic scale. The QR encodes exactly one string:

```
{PUBLIC_BASE_URL}/v/{TAG_CODE}
```

No auth, no query parameters, no tracking identifiers, no item id.
`PUBLIC_BASE_URL` is the **frontend** origin, so a scan opens the frontend's page
rather than putting a free-tier cold start between a shopper and their answer.
The path is `/v/` rather than `/verify/` because every character costs modules,
and fewer modules means a coarser grid that survives a crease.

The public page recomputes the item hash from the PostgreSQL row and compares it
to what was anchored. **That comparison is the reason the chain is in this system
at all**: an operator with database write access cannot quietly change a record,
because changing any hashed column changes what the recomputation produces, the
anchored value does not move, and the page flips to `MISMATCH` in public.

The surface follows three rules nothing else does. *It never returns 500* — an
unreachable chain, an unindexed anchor, a missing category row are each reported
inside a 200 payload. *It leaks nothing* — every field is a hand-written
projection, so no column added later becomes public by accident, and there is no
user id, item id, email, phone, address or legal name anywhere in it. *It answers
a malformed code without touching the database* — the check symbol is verified
first, which refuses a smudged label instantly and stops a stranger probing the
items table with codes that cannot exist.

### The attack it actually addresses

Cryptography cannot see a peeled label. Anyone holding one real object can
photograph its tag and reprint it, and every reprint scans identically because it
*is* the same number. What remains visible is the **pattern of scans**, so four
rules run over the scan history — geographic spread, impossible velocity, volume,
device diversity — producing `NONE`, `WATCH` or `SUSPICIOUS` with a sentence a
shopper can read. Rules rather than a model, because in a room with a regulator
the only thing that matters is being able to say why a tag was flagged.

Nothing blocks anything. A saree bought in Gujarat and carried to Assam is an
ordinary gift, and a system that refused to show provenance because of that would
be wrong far more often than it was right.

**Claiming is first-scan-wins and the wording is a product decision.** When a
second device scans a claimed tag it is told *what happened* — claimed on a date,
in a region — and to ask the seller. It is not told what that means. A retail
display gets scanned by dozens of people doing nothing wrong; a person who scans
their own object twice has done nothing at all; and a system that tells the
second scanner they are holding something illegitimate will be wrong far more
often than it is right, in public, to the one customer who cared enough to check.

---

## 11. Security posture

* **Passwords** argon2id, peppered. Never returned by any endpoint.
* **Access tokens** Ed25519 JWTs, 15 minutes, algorithm/issuer/audience all pinned at verification — which is what stops `alg: none` and an HS256 token signed with the public key.
* **Refresh tokens** opaque 32 random bytes in an `HttpOnly` cookie, stored only as SHA-256, rotated on every use. Presenting a rotated token revokes the **entire family including the successor just issued**: two parties on one token chain is theft until proven otherwise.
* **Pending tokens** a different key (HS256), a different audience (`sutradhar/pending`), no role, no user id, single use, ten minutes. Presenting one as a bearer token fails on both signature and audience.
* **Privilege escalation** is structurally impossible rather than defended against: `role`, `status` and `email` are **absent from** `UpdateProfileRequest`, so no request shape can reach them. Only `CONSUMER` and `WEAVER` are self-assignable, and a self-declared weaver lands in `PENDING_VERIFICATION`, where their attestations raise nobody's trust level.
* **OAuth** resolves by provider subject, never by email — Google recycles addresses for deleted Workspace accounts, and email resolution would hand the next holder somebody else's account. An unverified provider email is refused outright.
* **Every route is guarded or explicitly allowlisted**, and `test_security_sweep.py` proves the inventory is really an inventory before trusting it.
* **PII** never enters a hashed preimage, a log line, or the public payload. Scan telemetry keeps a coarse region code, a hashed device fingerprint and a hashed address — no GPS, no city, no raw address.
* **Error bodies** never quote an exception. A connection failure's message contains the DSN, and the DSN contains a password.

---

## 12. Testing

| Suite | Files | Lines | What it is for |
|---|---|---|---|
| `unit/` | 14 | 2,121 | pure logic — hashing, canonicalisation, Merkle, ids, pagination, anomaly rules, and the two grep-based invariant tests |
| `integration/` | 35 | 15,663 | real PostgreSQL and the full ASGI app |
| `contract/` | 2 | 419 | `API_CONTRACT.md` against the running application |
| `load/` | 5 | 1,009 | throughput and cold start; deselected by default |
| `fakes/` | 5 | 1,187 | offline chain, Google (real RSA, real signatures), Pinata |

**SQLite is not a substitute.** The things worth testing at this layer are
`ON CONFLICT DO UPDATE`, native enum types, `citext` and `JSONB`, none of which
SQLite has. The schema is built from `Base.metadata` rather than by running
migrations, so a schema bug and a migration bug stay distinguishable; the
migration is checked separately by its own upgrade/downgrade round trip.

The fakes are worth a note: `fake_google.py` uses real RSA keys, real signatures
and a real JWKS document, so the tests drive the actual verification code. Only
the network is faked. A test that mocked verification away would prove nothing
about the module whose entire job is to verify.

Several tests assert properties rather than thresholds — `test_query_counts.py`
asserts a five-deep item costs the *same* number of statements as a flat one,
which is a stronger claim than any ceiling and fails at depth 1.

### One trap, twice

**Never write `from app.db.session import SessionLocal` at module level.** The
rate limiter and the media quota trackers deliberately open their own sessions,
so they need the sessionmaker by name — and a module-level import binds whatever
that name pointed at *the moment the module was first imported*. That moment is
not a fixed point: it is inside whichever test builds an application first. A run
whose first `create_app()` sits outside the integration fixtures leaves the
binding on the **production** sessionmaker, and the affected code then meters
against the development database while every assertion about the test database
quietly stops meaning anything.

The failure shape is the worst available: green in isolation, red in a full run,
and red on a test that has nothing to do with whatever caused it. Phase 12 hit
this as B10 and fixed four modules; `app/media/router.py` was missed and hit it
again. It now resolves the name inside the function body, as `app.core.ratelimit`
already did, and the shared fixture names every module that reaches for the
factory.

Three modules still capture it at import: `app/auth/router.py` and
`app/auth/oauth/router.py`, both of which the shared fixture redirects by name,
and `app/api/health.py`, which the dead-database fixture redirects for the one
test that depends on it. They are correct today because a fixture covers each of
them. Anything new that needs the sessionmaker should resolve it lazily instead
of relying on that.

---

## 13. Operations

Configuration is one typed `Settings` object, and `get_settings()` is called at
import so a misconfigured deployment fails at startup rather than on the first
request that happens to touch a missing variable.

**Hard requirements** (absent → import fails): `DATABASE_URL`,
`DATABASE_URL_SYNC`, `JWT_PRIVATE_KEY_PATH`, `JWT_PUBLIC_KEY_PATH`,
`PENDING_TOKEN_SECRET`, `CURSOR_SECRET`.

**Soft requirements** (absent → that feature reports itself unavailable):
`GOOGLE_CLIENT_ID`/`SECRET`, `PINATA_JWT`, `CHAIN_SIGNER_PRIVATE_KEY`.

`PUBLIC_PREFIX` is validated rather than normalised, because it is the leading
segment of a URL that goes on cloth: `PUBLIC_PREFIX=http://example.com` used to
become `/http:` and boot happily, serving a path no printed label could ever
reach.

### Probes

`/healthz` is liveness and touches nothing. `/readyz` probes six dependencies
independently and **answers 503 only when PostgreSQL is down**. That asymmetry is
the design: without PostgreSQL there is no request this API can serve; without a
chain node every route still works and the public page honestly reports
`UNANCHORED`. A readiness probe that failed because a testnet had a bad afternoon
would pull a working instance out of rotation for a dependency it does not need.

### The operator screen

`GET /api/v1/admin/system/status` answers the questions somebody actually asks
five minutes before a demo: queue depth by job type and state, dead letters,
indexer lag, quota percentages, scheduler jobs with last and next run, and a
derived `chain_mode`. It never returns 500 — an operator opens it precisely when
something is broken — and an unreachable node gives `lag_blocks: null` with the
reason, never a zero that would read as "caught up".

### Scripts

| Script | Does |
|---|---|
| `gen_keys.py` | generates the Ed25519 signing keypair; idempotent |
| `bootstrap_db.sql` / `bootstrap_db.py` | database, extensions, schema |
| `create_admin.py` | the first admin account |
| `deploy_contract.py` | deploys the registry — Python, because the relayer key is already loaded there |
| `replay_chain.py` | rebuilds the event index from the chain and reconciles |
| `demo_oauth_flow.py` | drives the Google flow end to end |

---

## 14. Current state

**`chain_mode` is `postgres_only`.** `CONTRACT_ADDRESS` is the zero address,
`CHAIN_WRITE_ENABLED` is false, and `PINATA_JWT` and `CHAIN_SIGNER_PRIVATE_KEY`
are unset.

This is the real default, and **every phase is tested against it as the primary
path rather than as an edge case.** Records are real; anchors are not yet. Items
sit at `PENDING`, the public page reports `UNANCHORED` with `stale: true`, and
the admin screen says `postgres_only` in as many words. None of that is a fault
state, and a frontend that renders `PENDING` as an error will render an error at
every demo.

Built, tested, and deliberately off: Merkle batching (`BATCHING_ENABLED=false`,
so each registration gets its own visible transaction during a demo).

Not built: a frontend. `API_CONTRACT.md` is frozen and is what it consumes.

### If you change one thing, know this first

1. **`item_hash` preimage** — frozen. Changing it invalidates every anchored record, with no migration.
2. **Trust levels** — derived at read time. Do not add a column, a cache, or a setter.
3. **`outbox`** — one table, four job types, one retry mechanism. Do not add a second queue.
4. **`app/verification/`** — imports nothing from `app.auth` or `app.admin`. A test enforces it.
5. **Media** — unlinked is never hard-deleted, and the PostgreSQL blob is the only tier that survives a redeploy.
6. **`/v/{tag_code}`** — printed on cloth. It cannot be renamed or versioned, ever.
7. **Verdict vocabulary** — a grep test fails the build on *genuine*, *authentic*, *counterfeit-proof* and friends.
8. **`app/chain/`** — no swallowed exceptions.

---

## 15. Where to read next

| Question | File |
|---|---|
| How do I run it? | [`README.md`](../README.md) |
| What does the API return? | [`API_CONTRACT.md`](API_CONTRACT.md) |
| What broke under load and concurrency? | [`phase-12-findings.md`](phase-12-findings.md) |
| What exactly is hashed? | `app/provenance/item_hash.py` — the docstring is the specification |
| Why is trust shaped like this? | `app/attestation/trust.py` — the docstring is the argument |
| Why does the public page look so small? | `app/verification/schemas.py` |
| What can go wrong with the chain? | `app/chain/client.py` and `app/chain/confirmations.py` |

Most of this codebase's reasoning lives in module docstrings rather than in
documents, deliberately: a docstring is read by whoever is about to change the
code, and a document is read by whoever is about to write a different one.
