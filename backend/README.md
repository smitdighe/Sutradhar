# Sutradhar — backend

FastAPI service. Everything lives under `backend/`; a sibling `frontend/` is a
separate concern.

## Requirements

- Python 3.12 (managed by `uv`)
- PostgreSQL 16+ running natively on `localhost:5432`
- Node.js 20+ (only to run a local EVM JSON-RPC node for the offline demo)

No containers are used anywhere in this project.

## Setup

```bash
cd backend
uv sync --all-groups
uv run python scripts/gen_keys.py     # idempotent; writes keys/
cp .env.example .env                  # then fill in the secrets
```

Database bootstrap, as a PostgreSQL superuser:

```bash
psql -U postgres -h localhost -f scripts/bootstrap_db.sql
```

## Run

```bash
uv run alembic upgrade head
uv run uvicorn app.main:app --reload --port 8000
```

- `GET /healthz` — liveness, touches nothing
- `GET /readyz`  — per-dependency readiness (postgres, chain_rpc, anchoring,
  pinata, google_oauth, scheduler); always 200, read the per-item status
- `GET /docs`    — OpenAPI UI

An unreachable RPC endpoint is a degraded dependency, not a failed boot: the API
serves normally, the outbox keeps filling, and items stay honestly `PENDING`.

## Contract

Solidity lives in `contracts/`. Hardhat's only jobs are compiling and running a
local node; deployment is Python, because the relayer key is already loaded there.

```bash
cd contracts && npm install
npm run compile     # compiles and exports app/chain/abi/Sutradhar.json
npm run node        # optional: a local EVM node on 127.0.0.1:8545, no containers
```

```bash
# deploy, then copy CONTRACT_ADDRESS into .env
uv run python scripts/deploy_contract.py --rpc http://127.0.0.1:8545 --chain-id 31337
uv run python scripts/deploy_contract.py            # Polygon Amoy, from .env
```

`app/chain/contract.py` asserts at load time that the compiled ABI really exposes
`anchorItem(bytes32,bytes32)`, `anchorBatch(bytes32,uint32)` and the two events.
A drifted ABI fails at import rather than encoding calldata nothing answers to.

## Chain workers

Five APScheduler jobs run inside the FastAPI lifespan, all disabled by
`SCHEDULER_ENABLED=false`:

| Job | Cadence | Does |
|---|---|---|
| outbox drain | `OUTBOX_POLL_SECONDS` | claims queued anchors and sends them |
| pin retry | `PIN_RETRY_POLL_SECONDS` | retries `PIN_PENDING` media through the same outbox |
| confirmation sweep | `CONFIRMATION_POLL_SECONDS` | receipts, reorg checks, promotions, fee bumps, nonce gap fills |
| indexer | `INDEXER_POLL_SECONDS` | tails contract events into `chain_events` |
| reconcile | `RECONCILE_CRON` | diffs chain against Postgres; reports, never corrects |

Single-instance is asserted with a Postgres advisory lock, not assumed. A second
process registers no jobs and logs why.

Prove the index is a cache and the chain is the record:

```bash
uv run python scripts/replay_chain.py --into-empty
```

It clears `chain_events`, rewinds the indexer to genesis, re-reads every anchor
from the chain, and then reconciles. Exit `0` means zero drift.

## Trust and attestation

The chain stores whatever a human typed. A weaver or a corrupt co-op officer can
register a powerloom piece as handloom and the ledger holds that claim, unaltered,
forever. Immutability is a property of the record, not of its truth.

So the system never reports whether an object is what it claims to be. It reports
**who vouched for it and how independent they were**:

| Level | Means |
|---|---|
| `SELF_DECLARED` | only the registrant has attested |
| `CO_OP_ATTESTED` | plus at least one independent co-op officer |
| `INSPECTED` | plus at least one independent inspector |
| `DISPUTED` | a participant is fraud-flagged, or the item is contested — overrides everything above |

The level is **derived on every read** from the attestation and dispute sets.
There is no stored column, no setter, and no endpoint that assigns one — so
nobody can grant a level, nothing goes stale, and a fraud flag takes effect on
the very next read with no cache to invalidate.

```
POST {prefix}/items/{id}/attestations    WEAVER | COOP_OFFICER | INSPECTOR
GET  {prefix}/items/{id}/attestations    paginated; roles and pseudonymous refs only
GET  {prefix}/items/{id}/trust           level plus the evidence behind it
POST {prefix}/admin/actors/{id}/fraud-flag    ADMIN
POST {prefix}/admin/actors/{id}/fraud-clear   ADMIN
```

Attestations anchor through the Phase 7 outbox — same claim, nonce, writer and
confirmation depth an item anchor uses. There is no second chain path.

**Vocabulary is enforced, not just intended.** `tests/unit/test_no_verdict_language.py`
greps the whole `app/` tree for "genuine", "authentic", "counterfeit-proof",
`is_fake`, `is_real` and friends, and fails on anything outside a short
allowlist that must itself still match real lines. Approved vocabulary:
*verified provenance*, *chain of custody recorded*, *self-declared*,
*co-op attested*, *inspected*, *disputed*.

## Media and IPFS

IPFS stores nothing by itself. A CID is an address; a pinning service is what
keeps the bytes at it, and a free tier that lapses takes them away while the
chain still points at the hash. So the SHA-256 goes on chain -- proving the bytes
were never altered -- and the bytes themselves live in **three** places:

| Tier | Durable | Notes |
|---|---|---|
| Pinata gateway | no | only while somebody keeps paying to pin |
| local mirror (`media_mirror/`) | **no** | Render's free-tier disk is ephemeral; a redeploy empties it |
| Postgres `bytea` | yes | the one that is still there afterwards |

Two of those can vanish without anyone doing anything wrong, on the same
afternoon. That is why the database tier is mandatory and must not be
"optimised" away. It is also the tier whose limit takes the whole API down, so
it has its own inline ceiling (`MEDIA_BLOB_MAX_BYTES`) and its own budget
(`MEDIA_BLOB_BUDGET_BYTES`), both well under the Pinata ones. Crossing them
costs the durable copy, never the upload.

The upload pipeline refuses in order of increasing cost: stream under a hard
ceiling, sniff the type **from the bytes** (a client's `Content-Type` is not
evidence), hash, dedupe on the hash, check both budgets, mirror, store, and only
then attempt the pin. The digest is committed before pinning is attempted, so a
row with no CID is still a complete integrity record.

```
POST   {prefix}/media                      multipart; WEAVER | COOP_OFFICER | INSPECTOR
GET    {prefix}/media/{id}                 metadata plus every readable tier
GET    {prefix}/media/{id}/raw             bytes, falling through tiers
POST   {prefix}/items/{id}/media           link  (registrant or admin)
DELETE {prefix}/items/{id}/media/{mid}     unlink; the media row is never deleted
```

Pinning failures never fail an upload. The row lands `PIN_PENDING` and a
`PIN_MEDIA` job goes into the same outbox that anchors hashes, so retries get the
same backoff and the same dead-lettering. After the last attempt the row is
`PIN_FAILED` -- the file still resolves from the mirror and the blob, it is just
not on IPFS.

With `PINATA_JWT` unset, pinning is off: uploads still return 201, everything
stays `PIN_PENDING`, and `/readyz` reports `pinata: unconfigured`.

Production path for real durability is Filecoin or Arweave, which pay for
persistence up front instead of renting it monthly. Not built here.

## Tags and QR codes

Tag codes are generated by the backend at issuance, never pre-printed. A
pre-printed range is a list of valid codes sitting in a print shop: whoever
holds it can put a plausible tag on anything. A code here exists only once a row
in `items` claims it, so a label with no matching record resolves to nothing.

The code itself is 12 characters over a 29-symbol alphabet with a mod-29 check
symbol (`app/core/ids.py`) -- roughly 53 bits, so it is unguessable as well as
collision-free. `tag_code` is `UNIQUE`; a violation is retried up to five times
and then reported as `TAG_GENERATION_EXHAUSTED` (500), because at that entropy a
real collision means the generator is broken, not that the caller did anything
wrong. Codes are stored and compared bare, displayed grouped in fours
(`X7K2-9M4P-3RQ8`), and normalised on every lookup: uppercased, separators
stripped, and the excluded characters folded onto what the reader meant.

The QR encodes exactly `{PUBLIC_BASE_URL}/v/{TAG_CODE}` and nothing else -- no
token, no signature, no query string. A QR is a printed number; anything secret
inside one is public the moment it goes on fabric. **`PUBLIC_BASE_URL` is the
frontend origin, not this service**, so a scan resolves without waiting on a
backend cold start. Set it to the real deployment origin before printing
anything: changing it later does not fix labels already on cloth.

Images are error-correction level Q (25% recovery, for creased and rubbed
fabric) with a four-module quiet zone, PNG at 512px by default and SVG carrying
a `viewBox` so a print shop can scale it freely. Both are served
`Cache-Control: public, immutable` -- the payload for a tag never changes.

```
POST {prefix}/items/{id}/tag          Idempotency-Key required; WEAVER (own items) | COOP_OFFICER | ADMIN
GET  {prefix}/items/{id}/tag/qr       ?format=png|svg&size=<px>
POST {prefix}/admin/tags/bulk         {item_ids: [...]}, max 500; COOP_OFFICER | ADMIN
```

Already tagged is `409 TAG_ALREADY_ISSUED` carrying the existing code -- a fact
to read, not a failure to retry. A `FAILED` item is `422 TAG_NOT_ISSUABLE`:
nothing was anchored, so there is no recorded provenance to put a label on.
Tagging an item that has children succeeds but returns a `warnings` entry --
one tag over several objects is the substitution path, and a bolt sold whole is
still legitimate. Bulk issuance reports per item and commits the rows that
worked; one bad item does not sink the batch.

Issuance is a Postgres write and nothing else. It is not anchored and does not
touch the outbox: the item hash does not commit to the tag code, so a reprinted
label never invalidates an anchor. Every issuance writes a `TAG_ISSUED` event,
including the seeded ones -- `seeds/loader.py` binds tags through the same
service the API uses.

## Public verification, scan anomalies, and claiming

`/v/{tag_code}` is the only unauthenticated surface in the system. It is mounted
bare, outside `API_PREFIX`, because its path is printed on cloth: the QR payload
is `{PUBLIC_BASE_URL}/v/{TAG_CODE}` on the frontend origin, the frontend's page
at that path calls this service across origins, and a URL committed to fabric
cannot carry a version segment that might later change.

```
GET  /v/{tag_code}         public record; ETag + Cache-Control: public, max-age=60
POST /v/{tag_code}/scan    {device_fingerprint?, region_code?}; per-IP limited
```

### Verification is a recomputation, not a lookup

1. Load the item row.
2. Recompute its hash from that row with the frozen Phase 6 preimage.
3. Read what was anchored **for this item** -- live from the chain when it can be
   reached, from the indexed event mirror when it cannot.
4. Compare: `MATCH`, `MISMATCH`, or `UNANCHORED`.

Editing any hashed column in PostgreSQL flips the public answer to `MISMATCH`.
That is the whole reason the chain is here: it means an operator with write
access cannot quietly rewrite a record. `test_public_verification.py` asserts it
by changing a quantity behind the API's back. Batched items verify by inclusion
proof against the anchored root, and the proof is published so a reader can
check it offline instead of taking the answer on trust.

The anchor is located by **item identity** -- the Merkle leaf or the append-only
`ANCHORED` event -- never by the stored digest column, which an editor could also
change to cover their tracks.

Nothing is deployed and `CHAIN_WRITE_ENABLED=false`, so `UNANCHORED` with
`stale: true` is the ordinary answer today, served as a 200. An unreachable RPC
endpoint gets the last indexed state, labelled `stale` with the timestamp it was
observed. This surface never returns 500.

### The attack this phase answers

**A QR is a printed number.** Whoever owns one real object can photograph its
label and reprint it, and every reprint carries a correct code with a correct
check symbol. Peeling a label off one object and sticking it on another is the
same problem with no printer at all. No signature scheme sees either case,
because in both the number is right. What is left is the *pattern of scans*.

| Signal | Fires when |
|---|---|
| `GEOGRAPHIC_SPREAD` | distinct regions > `SCAN_ANOMALY_MAX_REGIONS` |
| `IMPOSSIBLE_VELOCITY` | implied travel between consecutive scans > `SCAN_ANOMALY_VELOCITY_KM_PER_H` |
| `VOLUME` | total scans > `SCAN_ANOMALY_MAX_SCANS` |
| `DEVICE_DIVERSITY` | distinct devices in `SCAN_ANOMALY_DEVICE_WINDOW_MINUTES` > `SCAN_ANOMALY_MAX_DEVICES` |

`NONE` -> `WATCH` (one signal) -> `SUSPICIOUS` (two, or impossible velocity
alone -- it is the only one with no innocent reading; a shop window explains a
lot of scans and explains nothing about one object in two places).

Rules, not a model: every flag carries a sentence, and every threshold is an
environment variable so it can be tuned on stage. Nothing is ever blocked. A
saree bought in Gujarat and gifted in Assam is an ordinary gift.

Location is coarse and second-hand: country and subdivision come from the edge
that terminated the connection (`X-Vercel-IP-Country-Region`, `CF-IPCountry`,
`X-Geo-Region`) or from the body, never from a geo-IP call at request time.
Distances use a bundled table of Indian state centroids. No GPS, no city, no
coordinates. The address is peppered and hashed before it reaches a column, and
the device fingerprint is stored only as a SHA-256.

### First scan wins

`claims.item_id` is the primary key, so a second claim is refused by PostgreSQL
rather than by an `if` this code could lose a race on. Two simultaneous first
scans produce exactly one claim; the loser is told `ALREADY_CLAIMED`. A rescan
from the same device is `is_your_claim: true` with no warning. The first claim is
never overwritten.

The message to a second device states **what happened and nothing else**: this
tag was already claimed on a date, ask the seller if that is unexpected. It never
says the object is illegitimate. A retail display gets scanned by dozens of
people who have done nothing wrong, and accusing the customer who bothered to
check is worse than the problem. The wording is asserted against a forbidden-word
list, the same way Phase 8 enforces the trust vocabulary.

### Zero PII, asserted by grep

`test_public_payload_pii.py` seeds a maker carrying an email, a legal name, a
phone number, an address and government identifiers, then greps the raw
serialised response for every one of them. Internal ids are checked the same way:
publishing an item id turns one tag code into a handle on the whole item graph.
The maker appears as a display handle they chose, and
`users.public_display_opt_out` withdraws it -- a display choice, not erasure, so
the record still verifies and the choice is reversible.

### Isolation

The package shares models and pure derivations (the frozen hasher, the trust
ladder, the Merkle proof) and imports **no** authentication, moderation, or
authenticated router, and no serialiser from them.
`tests/unit/test_verification_isolation.py` asserts it by reading the imports,
so the package stays liftable to an edge runtime later.

## Checks

```bash
uv run ruff check . && uv run mypy app
uv run pytest tests/ -q                              # the normal run
uv run pytest tests/ -m "not chain and not load" -q  # without the chain suite
uv run pytest tests/ -m "chain and not load" -q      # against tests/fakes/fake_chain.py
uv run pytest tests/load -m load -q -s               # slow, opt-in; prints its numbers
```

The `not load` is not decoration. A `-m` on the command line *replaces* the one
in `pytest.ini` instead of combining with it, so a bare `-m "not chain"` selects
the load tests and spawns a `uvicorn` nobody asked for.

The chain suite runs entirely against an in-memory EVM that reorganises blocks,
drops RPC calls and reverts transactions on command. No testnet is involved and
none is needed.

Load tests are excluded from the default run by a marker. They spawn a real
`uvicorn`, seed ten thousand items, and take minutes — the wrong price for the
run somebody does before every commit. `-s` matters: they exist to print
numbers, and a number nobody sees is a number nobody checks.

## Before a demo

Open `GET {API}/admin/system/status` as an admin. One screen, one read, and it
answers everything worth knowing five minutes beforehand:

| Field | Read it for |
|---|---|
| `chain.mode` | `live` or `postgres_only`. Today it is `postgres_only`, and that is correct — records are real, anchors are not yet |
| `outbox` | depth by job type and state. A growing `QUEUED` with the scheduler running means the chain is unreachable |
| `dead_letters_unresolved` | jobs that exhausted their retries. Should be zero |
| `indexer.lag_blocks` | how far behind the event mirror is. `null` means the RPC could not be reached, which is not the same as caught up |
| `quotas` | Alchemy compute units and Pinata bytes, with percent used |
| `jobs` | each worker's last and next run. Empty means the scheduler is off |

### Measured numbers

From `tests/load`, on a local machine with PostgreSQL on the same host. Rerun
them rather than trusting these; they are here so there is something to compare
against.

| Measurement | Value |
|---|---|
| `GET /v/{tag_code}` service time, p50 / p95 | 43 ms / 48 ms |
| Malformed tag code refused (checksum, no query) | 22 ms p95 |
| Unknown but well-formed tag (404) | 26 ms p95 |
| Simultaneous scanners served inside a 400 ms p95 | 8 |
| 200-concurrent burst, p50 / p95 / p99 | 7.9 s / 35 s / 53 s, no 5xx |
| **Cold start — process spawn to first `/v/{code}`** | **≈ 4.4 s** |
| Outbox drain, 800 chain jobs against the fake node | 16.6 jobs/s, no nonce gaps |

The cold-start figure is the Render free-tier answer: how long a shopper waits
if the instance has been spun down. It is measured, not asserted against a
threshold — no number would be right on both a laptop and a free instance.

Response time under a 200-deep queue is not service time. `GET /v/{tag_code}`
issues 15 statements, each a round trip worth roughly 2 ms locally, which is
where the 43 ms goes; beyond about 8 simultaneous requests a single worker is
queueing, and latency grows linearly with offered load. Adding uvicorn workers
helps sub-linearly (4 workers gave 1.3×, not 4×) because the shared PostgreSQL
becomes the constraint. The route to more headroom is fewer round trips per
request — the same quantity `tests/integration/test_query_counts.py` pins.

## Environments

`DATABASE_URL` is the only thing that differs between local and production.
Production runs on Neon (managed Postgres); local dev runs the native instance
configured by `scripts/bootstrap_db.sql`.

Absent credentials are an expected state, never a crash:

| Variable | Absent means |
|---|---|
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Google sign-in reports unavailable |
| `PINATA_JWT` | pinning unavailable; uploads still succeed and resolve from mirror + blob |
| `CHAIN_SIGNER_PRIVATE_KEY` | outbox fills but never sends |
| `CHAIN_WRITE_ENABLED=false` | outbox fills, nothing is sent, items stay `PENDING`, API unaffected |
| unreachable `CHAIN_RPC_URL` | app still boots; `/readyz` reports the chain down |
