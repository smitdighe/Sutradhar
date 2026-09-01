# Phase 12 — every bug found and fixed

Phases 1–11 each tested one seam. This pass tested the joins between them, killed
each dependency on its own, and drove two requests at one row at the same
instant. Fourteen defects came out of it. Nine were found by reading the code
before a line of test was written; five were found by the tests themselves, and
those five are the more interesting half.

Every entry names the file, what was wrong, why it mattered, and the test that
now fails without the fix. Where a fix was *proved* necessary by reverting it and
watching a test go red, that is recorded — a race test that passes both with and
without the fix is not evidence of anything.

---

## Found by reading

### B1 — a retried request could return 500

**`app/core/idempotency.py`** · `begin()`

The key claim was `SELECT`, then `INSERT` if nothing came back. Two simultaneous
retries of one request — which is exactly the traffic this table exists to
absorb — both saw no row, both inserted, and the loser took a unique violation
on `uq_idempotency_keys_user_key` into the generic handler. The caller, who had
done nothing wrong beyond retrying a timeout, got `INTERNAL_ERROR`.

Now a single `INSERT … ON CONFLICT DO NOTHING RETURNING id`, with the winner's
row read back on a miss and treated like any other replay. The same shape
`enqueue_job` and `attempt_claim` already used.

*Proved:* reverting it raises
`UniqueViolationError: duplicate key value violates unique constraint
"uq_idempotency_keys_user_key"` in
`test_concurrency.py::TestSameIdempotencyKey::test_claiming_one_key_from_two_transactions_does_not_raise`.

Note that this race is **not** reachable through the in-process ASGI transport —
two HTTP requests there tend to serialise, and five parallel POSTs still passed
with the bug in place. The test drives `begin` from two open, uncommitted
transactions instead, which is the state two real clients are in. A test written
only at the HTTP layer would have shipped this.

### B2 — two tags on one item, and a printed label pointing at nothing

**`app/qr/service.py`** · `assign_tag_code()`

`issue_tag` read the item, checked `tag_code is None`, and then assigned. Under
contention both requests read `None`, both updated, and the second **overwrote**
the first's code — two `TAG_ISSUED` events, no 409, and whichever label had
already gone to the printer now resolved to nothing.

The binding is now a conditional update: `WHERE items.id = :id AND tag_code IS
NULL`, with zero rows updated raising `TAG_ALREADY_ISSUED` (409) carrying the
winner's code. The database decides, the way `claims.item_id` and
`uq_attestations_item_attestor` already do. The SAVEPOINT retry stays — it is
for *generator* collisions, which is a different failure.

*Proved:* reverting it gives `[201, 201, 409, 409, 409]` from five parallel
issuances in
`test_concurrency.py::TestTagIssuance::test_five_at_once_still_leaves_one_code`.
Two parallel requests were not enough to reproduce it reliably; five were.

### B3 — `/readyz` never failed

**`app/api/health.py`**

It returned 200 whatever it found, so an orchestrator had nothing to route on.
It now answers 503 when a dependency in `REQUIRED_CHECKS` is down, and that set
contains exactly one name: `postgres`.

Deliberately not "any check that is down". The chain RPC is unreachable in the
normal configuration; making that a 503 would take a working instance out of
rotation for a dependency it does not need, and would have broken two existing
tests that correctly assert 200 in that state. The body is unchanged apart from
a new `unready` list naming what failed.

### B4 — no way to trace a request to an account

**`app/main.py`**, **`app/auth/guards.py`**

The access log carried `request_id`, method, path, status and duration, and no
user. A report of "something went wrong for me" could not be tied to an account.

`guards._resolve_user` now publishes the id on `request.state` after resolving
the token, and the access-log middleware reads it. The id and nothing else — an
email in a log line is the leak this system takes the most trouble elsewhere to
avoid.

A contextvar would have been the obvious mechanism and does not work here:
Starlette's `BaseHTTPMiddleware` runs the downstream app in its own task, so a
value set in a dependency is not visible to the middleware afterwards. The ASGI
scope is shared, so `request.state` is.

### B5 — the scan history was scored twice on every scan

**`app/verification/router.py`**

`record_scan` computed an `AnomalyVerdict` over the tag's whole history and
returned it. The router discarded it and called `build_view` without
`verdict=` — so `_scan_block` loaded every scan row a second time to reach the
same answer. The parameter already existed; nothing passed it.

### B6 — the one real N+1 on the public page

**`app/verification/service.py`** · `_ancestry()`

The lineage was walked with one `session.get(Item, …)` per level. A four-deep
provenance chain was four round trips on the one page a shopper waits for, and
the depth is whatever somebody registered — not a quantity this module controls.

Replaced with `app.provenance.tree.get_ancestry`, the recursive CTE the
authenticated tree endpoint already uses. Sharing it rather than writing a second
one is deliberate: two copies would drift, and the first symptom of drift would
be the public and private views disagreeing about an object's parentage.

*Guarded by:* `test_query_counts.py::test_the_count_does_not_move_with_lineage_depth`,
which asserts a five-deep item costs the same as a flat one — a stronger property
than any ceiling, and one that fails at depth 1.

### B7 — counting rows that had just been loaded

**`app/verification/service.py`** · `_scan_block()`

`SELECT count(*)` over `scans`, followed by `assess_scans` loading every row of
the same set. The count now travels on the verdict, which every anomaly rule was
already deriving from the full history.

### B8 — a malformed mount became a different, working one

**`app/config.py`** · `_normalise_mount()`

Anything was accepted. `PUBLIC_PREFIX=http://example.com` normalised to
`/http:`, and the service booted and served a path no printed label could ever
reach. Since this value is the leading segment of a URL that goes on cloth, a
silent rewrite is the one misconfiguration nobody catches before the labels come
off the printer.

Whitespace and a trailing slash are still normalised — those are typing. A
scheme, a host, internal whitespace, `?` or `#` now fail at boot with a message
naming the value.

### B9 — no operator surface

**`app/admin/`** was an empty package. Added `GET {API}/admin/system/status`
(`require_role(ADMIN)`): outbox depth by job type and state, dead-letter counts,
indexer lag in blocks, quota usage with percentages, scheduler jobs with last and
next run, and a derived `chain_mode` of `live` or `postgres_only`.

It never 500s, for the same reason the public surface does not: an operator opens
it precisely when something is broken. An unreachable node gives
`lag_blocks: null` with the reason in `detail`, never a zero that would read as
"caught up".

---

## Found by the tests

These five are the reason the phase was worth running.

### B10 — tests could write to the development database

**`tests/integration/conftest.py`**

The shared fixture redirected `SessionLocal` in three modules but not in
`app.db.session` itself. The `rate_limit` dependency imports it *inside the
function body*, so it reads the attribute off `app.db.session` at call time and
never sees a patch applied anywhere else. Any test exercising a rate-limited
route — every public `/v/` route among them — wrote its buckets to whichever
database that name pointed at.

Three test files carried private workarounds for this. The failure is silent: the
test passes, the rows land in the development database, and the counts survive to
rate-limit the next run. Hoisted into the shared fixture so it cannot be
forgotten.

### B11 — the registrant row was fetched twice per public page view

**`app/verification/service.py`**

`recompute_item_hash` and `_story` each did `session.get(User, item.registered_by)`
and each emitted a query for the same row.

The cause is worth recording because the obvious fix does not work. SQLAlchemy's
identity map holds **weak** references, so a row loaded inside a helper and
dropped on return is garbage-collected before the next helper asks for it. The
first attempt at this was a three-way join in `load_item_by_tag` — it changed
nothing, because the joined rows had no name holding them alive, and it added a
join to pay for. That attempt was reverted and the reason documented in the
function that would otherwise attract it again.

The working fix is to load the category and registrant once into locals in
`build_view` that live for the whole call, and pass them down. 16 → 15
statements.

### B12 — the live chain read had never been executed

**`app/verification/router.py`** · `_chain_reader()`

It passed `data` to `eth_call` as raw `bytes`. The JSON-RPC parameter is defined
as a `0x`-prefixed hex string; web3.py hexlifies on the way out, so this worked
against a real provider and would be rejected by any other client.

What matters more than the fix is how it surfaced: **every existing test stubbed
`_chain_reader` out entirely**, so the production reader had zero coverage. It
was found the first time a test wired a real chain client to the public route —
the `chain_on` variant of the end-to-end walk — and the offline node refused the
calldata. The symptom was subtle: verification still said `MATCH` from the
offline comparison, with `stale: true`, because `chain_state` catches everything
a reader throws. A public page that quietly stopped doing live reads and kept
answering correctly is exactly the failure nobody notices.

### B13 — an unreachable database returned 500

**`app/db/session.py`**, **`app/core/ratelimit.py`**, **`app/core/error_handlers.py`**

Every request during a database outage answered `INTERNAL_ERROR`, with a stack in
the log and nothing the caller could act on.

The first fix was an exception handler on `sqlalchemy.exc.OperationalError` and
`InterfaceError`, and **it never fired**. A refused connection surfaces from
asyncpg as a bare `ConnectionRefusedError` — SQLAlchemy does not wrap it, because
there is no connection yet for a dialect to attach the error to. The commonest
outage there is was the one case the handler could not catch.

Translation now happens where connections are actually made:

* `get_session`, for failures raised from a route body;
* `ratelimit.consume`, which opens its own session and can be resolved *before*
  `get_session` — without it, rate-limited routes answered 503 during an outage
  while every other route answered 500;
* the exception handler, kept as a backstop for a connection that breaks
  mid-statement.

The message is fixed and says nothing about the exception: `str(exc)` on a
connection failure quotes the DSN, and the DSN contains a password.

*Proved twice:* removing the `get_session` half turns
`test_failure_matrix.py::TestPostgresDown::test_an_unlimited_route_is_also_503`
red; the rate-limiter half is what makes the limited-route row pass. Each covers
a path the other cannot.

### B14 — an unauthenticated endpoint doing unmetered work

**`app/catalog/router.py`** · `POST /categories/{slug}/validate`

The route audit enumerated every operation and found five unauthenticated
catalog routes. Four are public by design and documented as such — a Geographical
Indication is public information and the frontend renders a category page before
anybody signs in — so they are on the allowlist with their reasons.

The fifth is different. It loads a schema and runs a validator over a
caller-supplied body: the only unauthenticated endpoint in the system that does
real work, and it had no ceiling. On a single free-tier process that is a way to
spend the CPU from outside with no account and no cost. It now carries the
public-surface limiter, reusing `RATE_LIMIT_SCAN_PER_MINUTE` rather than adding a
knob that would need tuning consistently and would not be.

---

## Two findings about the tests themselves

Neither is a bug in `app/`, and both would have made a guard worthless.

**The route audit initially found zero routes.** FastAPI 0.141 wraps each
`include_router` call in an `_IncludedRouter` that keeps the original router and
its prefix and exposes no `routes` attribute, so a walk over `app.routes` returns
nothing — and "no unguarded route was found" passed for the worst possible
reason. Caught by a companion test asserting the walk matches the OpenAPI
document path for path. Any inventory used as a security control needs one.

**The brief's verdict-language grep was too broad to enforce.** `authentic`
without a word boundary matches `authentication`, which this codebase says
constantly and must. Run as specified it produced 31 hits across the auth
package, and a check that fires on every one of those is a check somebody
switches off. The pattern now carries `\b` around the two words with innocent
longer forms; the surviving allowlist is a single file, `mass_balance.py`, whose
docstring names the attack in order to explain the defence — and a companion test
asserts those occurrences are prose and not string literals.

---

## What was searched and found clean

An empty finding is only meaningful alongside what was looked at. These were
tested and were already correct:

* **Refresh rotation under contention.** `SELECT … FOR UPDATE` serialises;
  exactly one 200, and reuse detection kills the family including the successor
  just issued. Two parties on one token chain is theft until proven otherwise.
* **Simultaneous first scans.** `claims.item_id` is a primary key with
  `ON CONFLICT DO NOTHING`; five at once produce one claim row, one device told
  it is theirs, and four told a dated fact with no accusation in it.
* **Parallel OAuth completions.** The conditional `UPDATE … WHERE consumed_at IS
  NULL` admits one; the other gets `PENDING_TOKEN_CONSUMED`. One account.
* **Duplicate attestations.** `uq_attestations_item_attestor` gives one row and
  one 409.
* **Parallel outbox drains.** `FOR UPDATE SKIP LOCKED` splits the queue with no
  row leased twice; nonces are contiguous with no duplicates across 800 jobs.
* **Mass balance under contention.** Two 8-metre splits of a 12-metre parent
  cannot both land.
* **Job-type isolation.** A chain drain will not lease a `PIN_MEDIA` job.
* **Secrets in error bodies.** None of six configured secrets, nor the signing
  key, appears in any error this suite can provoke.
* **Identities in logs.** A full end-to-end flow with the log captured contains
  no email, display name, access token, raw refresh token, raw device
  fingerprint, or password.
* **CORS.** A non-allowlisted origin gets no grant; `ETag`, `X-Scan-Recorded` and
  `X-Request-ID` are exposed, and the first two appear only on the public routes.
* **Every configured rate limit** fires at its threshold, read from settings
  rather than written into the test.
* **Zero PII on the public surface**, re-asserted against payloads produced by the
  real flow rather than by a fixture.
