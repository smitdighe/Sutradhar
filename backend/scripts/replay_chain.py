"""Rebuild the off-chain index from chain events alone, then prove it agrees.

Replayability is the property that makes an off-chain index trustworthy: if the
index can be thrown away and reconstructed from the chain, then the index is a
cache and the chain is the record. If it cannot, the index *is* the record, the
chain is decoration, and the whole provenance claim is weaker than it sounds.

This script demonstrates the property instead of asserting it. It empties the
event mirror, rewinds the indexer to genesis, re-reads every anchoring event
from the chain, and then runs reconciliation. A clean report means the rebuilt
index and the business tables agree on every hash, every status and every block.

**What "empty" means here.** ``--into-empty`` clears ``chain_events`` and resets
the indexer checkpoint. It does *not* delete items, users or categories, and it
must not: an item's attributes, its weaver and its quantity were never on chain
and cannot be recovered from it -- only the hash was anchored. Emptying those
would not be a replay, it would be data loss, and reconciliation afterwards
would report every anchor as unaccounted for. The index is what the chain can
rebuild; the business data is what the chain commits to.

Usage::

    uv run python scripts/replay_chain.py --into-empty
    uv run python scripts/replay_chain.py --into-empty --dsn postgresql+asyncpg://.../scratch
    uv run python scripts/replay_chain.py --from-block 8000000 --json

Exit codes: ``0`` clean, ``1`` drift found, ``2`` the replay could not run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import delete  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.chain.client import ChainClient, build_client  # noqa: E402
from app.chain.contract import ContractSurfaceError, load_contract  # noqa: E402
from app.chain.indexer import INDEXER_NAME, EventIndexer  # noqa: E402
from app.chain.reconcile import reconcile  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.models.chain import ChainEvent  # noqa: E402

EXIT_CLEAN = 0
EXIT_DRIFT = 1
EXIT_UNAVAILABLE = 2


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rebuild the chain event index and reconcile it against Postgres.",
    )
    parser.add_argument(
        "--into-empty",
        action="store_true",
        help=(
            "clear chain_events and rewind the indexer to genesis before replaying. "
            "Business tables are never touched -- see the module docstring."
        ),
    )
    parser.add_argument(
        "--from-block",
        type=int,
        default=None,
        help="replay from this height instead of genesis (implies a partial rebuild)",
    )
    parser.add_argument(
        "--dsn",
        default=None,
        help="async DSN to replay into; defaults to DATABASE_URL",
    )
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    dsn = args.dsn or settings.database_url
    engine = create_async_engine(dsn, pool_size=5, max_overflow=2)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    try:
        binding = load_contract()
    except (ContractSurfaceError, OSError, ValueError) as exc:
        _fail(f"contract artifact unusable: {exc}", args.json)
        await engine.dispose()
        return EXIT_UNAVAILABLE

    client: ChainClient = build_client(session_factory, settings)
    if not await client.connect():
        _fail(
            f"chain unreachable at {settings.chain_rpc_url}: {client.last_error}",
            args.json,
        )
        await engine.dispose()
        return EXIT_UNAVAILABLE

    indexer = EventIndexer(client, binding, session_factory, settings)

    removed = 0
    if args.into_empty:
        async with session_factory() as session:
            result = await session.execute(delete(ChainEvent))
            removed = int(getattr(result, "rowcount", 0) or 0)
            await session.commit()
        await indexer.reset_checkpoint(0)
    elif args.from_block is not None:
        await indexer.reset_checkpoint(args.from_block)

    index_report = await indexer.run()
    drift_report = await reconcile(session_factory, client, settings)
    await client.flush_quota()
    await engine.dispose()

    payload = {
        "indexer": INDEXER_NAME,
        "cleared_events": removed,
        "replayed": {
            "from_block": index_report.from_block,
            "to_block": index_report.to_block,
            "head_block": index_report.head_block,
            "events_written": index_report.events_written,
            "windows": index_report.windows,
            "errors": index_report.errors,
        },
        "reconcile": {
            **drift_report.as_log_fields(),
            "drifts": [drift.as_dict() for drift in drift_report.drifts[:100]],
        },
        "verdict": "clean" if drift_report.clean and not index_report.errors else "drift",
    }

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        _render(payload, drift_report.clean, index_report.errors)

    if index_report.errors:
        # A window that failed means the replay is incomplete, so a clean
        # reconciliation would be a statement about a partial index.
        return EXIT_UNAVAILABLE
    return EXIT_CLEAN if drift_report.clean else EXIT_DRIFT


def _render(payload: dict[str, object], clean: bool, errors: list[str]) -> None:
    replayed = payload["replayed"]
    assert isinstance(replayed, dict)
    reconciled = payload["reconcile"]
    assert isinstance(reconciled, dict)

    print("replay")
    print(f"  cleared events   : {payload['cleared_events']}")
    print(f"  blocks           : {replayed['from_block']} -> {replayed['to_block']}")
    print(f"  head             : {replayed['head_block']}")
    print(f"  events written   : {replayed['events_written']} over {replayed['windows']} window(s)")
    print()
    print("reconcile")
    print(f"  items checked    : {reconciled['items_checked']}")
    print(f"  events checked   : {reconciled['events_checked']}")
    print(f"  on-chain-not-db  : {reconciled['on_chain_not_in_db']}")
    print(f"  in-db-not-chain  : {reconciled['in_db_not_on_chain']}")
    print(f"  hash mismatch    : {reconciled['hash_mismatch']}")
    print(f"  status disagree  : {reconciled['status_disagreement']}")
    print()

    drifts = reconciled.get("drifts") or []
    assert isinstance(drifts, list)
    for drift in drifts:
        print(f"  ! {drift['kind']}: {drift['subject']} -- {drift['detail']}")

    if errors:
        for error in errors:
            print(f"  ! indexer: {error}")
        print("\nverdict: INCOMPLETE -- the replay did not cover every block")
        return

    print("verdict: " + ("zero drift" if clean else "DRIFT -- reported, not corrected"))


def _fail(message: str, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"verdict": "unavailable", "error": message}, indent=2))
    else:
        print(f"cannot replay: {message}", file=sys.stderr)


def main() -> int:
    return asyncio.run(run(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
