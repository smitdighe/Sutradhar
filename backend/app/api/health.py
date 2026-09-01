"""Liveness and readiness endpoints.

``/healthz`` is a pure liveness signal and touches nothing. ``/readyz`` probes
each dependency independently, reports ``ok`` / ``degraded`` / ``down`` per item,
and never raises — an unreachable dependency is data, not an error.

**Only one dependency can make this service unready.** ``/readyz`` answers 503
when PostgreSQL is unreachable and 200 in every other case, including a chain
RPC that is down. That asymmetry is the whole design: without Postgres there is
no request this API can serve, so an orchestrator should stop sending traffic;
without a chain node every route still works and the public page honestly reports
``UNANCHORED``. A readiness probe that failed because a testnet had a bad
afternoon would take a working service out of rotation for a dependency it does
not need. The per-item statuses still carry the detail either way — the status
code answers "should traffic come here", not "is everything perfect".
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app.chain.contract import load_contract
from app.config import get_settings
from app.db.session import SessionLocal
from app.workers.scheduler import get_scheduler

router = APIRouter(tags=["health"])

# "unconfigured" is not a failure: an optional feature nobody set up has
# nothing wrong with it. Kept distinct from "degraded", which means
# configured-but-impaired.
Status = Literal["ok", "unconfigured", "degraded", "down"]

PROBE_TIMEOUT_SECONDS = 3.0

# The checks whose failure means "send no traffic here". Exactly one, and adding
# a second is a decision about what this service cannot serve without, not a
# tidying-up: every name in here can take the instance out of rotation.
REQUIRED_CHECKS = frozenset({"postgres"})


def _item(status: Status, detail: str) -> dict[str, str]:
    return {"status": status, "detail": detail}


async def _check_postgres() -> dict[str, str]:
    try:
        async with SessionLocal() as session:
            await asyncio.wait_for(session.execute(text("select 1")), PROBE_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 - readiness must never propagate
        return _item("down", f"{type(exc).__name__}: {exc}"[:200])
    return _item("ok", "select 1 succeeded")


async def _check_chain_rpc() -> dict[str, str]:
    settings = get_settings()
    payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS) as client:
            response = await client.post(settings.chain_rpc_url, json=payload)
            response.raise_for_status()
            reported = int(response.json()["result"], 16)
    except Exception as exc:  # noqa: BLE001 - readiness must never propagate
        return _item("down", f"{type(exc).__name__}: {exc}"[:200])
    if reported != settings.chain_id:
        return _item(
            "degraded",
            f"chain id mismatch: rpc={reported} configured={settings.chain_id}",
        )
    if not settings.chain_signer_configured:
        return _item("degraded", "reachable but CHAIN_SIGNER_PRIVATE_KEY is empty; writes disabled")
    if not settings.chain_write_enabled:
        return _item("degraded", "reachable but CHAIN_WRITE_ENABLED is false; outbox will not send")
    return _item("ok", f"chain id {reported}")


async def _check_pinata() -> dict[str, str]:
    settings = get_settings()
    if not settings.pinata_enabled:
        return _item("unconfigured", "PINATA_JWT absent; local mirror only")
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS) as client:
            response = await client.get(
                "https://api.pinata.cloud/data/testAuthentication",
                headers={"Authorization": f"Bearer {settings.pinata_jwt}"},
            )
            response.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - readiness must never propagate
        return _item("down", f"{type(exc).__name__}: {exc}"[:200])
    return _item("ok", "authenticated")


def _check_anchoring() -> dict[str, str]:
    """Can an anchor actually be written right now, and if not, why not.

    Separate from ``chain_rpc``: a reachable node with no contract artifact and
    no relayer key is a perfectly healthy node that cannot anchor anything, and
    collapsing the two would send whoever is debugging to the wrong place.
    """
    settings = get_settings()
    try:
        binding = load_contract()
    except Exception as exc:  # noqa: BLE001 - readiness must never propagate
        return _item("down", f"contract artifact unusable: {type(exc).__name__}: {exc}"[:200])

    if not settings.chain_signer_configured:
        return _item("degraded", "no relayer key; the outbox queues but never sends")
    if not settings.chain_write_enabled:
        return _item("degraded", "CHAIN_WRITE_ENABLED=false; items stay PENDING by design")
    if int(settings.contract_address, 16) == 0:
        return _item("degraded", "CONTRACT_ADDRESS is the zero address; nothing is deployed")
    return _item("ok", f"writer bound to {binding.address}")


def _check_google_oauth() -> dict[str, str]:
    if not get_settings().google_oauth_enabled:
        return _item("unconfigured", "GOOGLE_CLIENT_ID/SECRET absent; Google sign-in disabled")
    return _item("ok", "client credentials present")


def _check_scheduler() -> dict[str, str]:
    settings = get_settings()
    scheduler = get_scheduler()
    if not settings.scheduler_enabled:
        return _item("degraded", "SCHEDULER_ENABLED is false")
    if scheduler is None or not scheduler.running:
        return _item("down", "scheduler enabled but not running")
    return _item("ok", f"{len(scheduler.get_jobs())} job(s) registered")


@router.get("/healthz", summary="Liveness probe")
async def healthz() -> dict[str, str]:
    """Always ``ok``. Touches no dependency."""
    return {"status": "ok"}


@router.get("/readyz", summary="Readiness probe")
async def readyz(response: Response) -> dict[str, Any]:
    """Per-dependency readiness.

    503 only when a dependency in :data:`REQUIRED_CHECKS` is down; 200 otherwise,
    however degraded the optional ones are. Read the per-item statuses for the
    detail — the status code is a routing decision, not a summary.
    """
    postgres, chain_rpc, pinata = await asyncio.gather(
        _check_postgres(),
        _check_chain_rpc(),
        _check_pinata(),
        return_exceptions=False,
    )
    checks: dict[str, dict[str, str]] = {
        "postgres": postgres,
        "chain_rpc": chain_rpc,
        "anchoring": _check_anchoring(),
        "pinata": pinata,
        "google_oauth": _check_google_oauth(),
        "scheduler": _check_scheduler(),
    }
    statuses = {item["status"] for item in checks.values()}
    # An unconfigured optional feature never degrades the overall verdict.
    statuses.discard("unconfigured")
    if "down" in statuses:
        overall: Status = "down"
    elif "degraded" in statuses:
        overall = "degraded"
    else:
        overall = "ok"

    unready = sorted(
        name for name in REQUIRED_CHECKS if checks.get(name, {}).get("status") == "down"
    )
    if unready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": overall, "checks": checks, "unready": unready}


__all__ = ["REQUIRED_CHECKS", "router"]
