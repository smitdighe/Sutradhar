"""The public surface. Two routes, no credentials, and a very short path.

**Mounted bare.** ``/v/{tag_code}`` sits outside ``API_PREFIX``, next to the
health probes. The printed payload is ``{PUBLIC_BASE_URL}/v/{TAG_CODE}`` and
``PUBLIC_BASE_URL`` is the frontend origin, so what a phone opens is the
frontend's page at that path; this service answers the same shape on its own
origin, and the frontend page calls it across origins with CORS. Two things
follow from that and both are deliberate: the path a printer commits to cloth
is one segment and carries no API version, and a tag URL pasted at this
service's own origin still resolves instead of 404ing.

**This is the only unauthenticated surface in the system**, which sets three
rules it does not share with anything else:

*It never 500s.* An unreachable chain, an unindexed anchor, a missing category
row -- each is reported inside a 200 payload. A shopper standing in a shop gets
the state of the record, honestly labelled, or a 404. They never get a stack
trace's HTTP equivalent.

*It leaks nothing.* Responses are hand-built projections from
:mod:`app.verification.schemas`. No ORM row is serialised, so no column added
later becomes public by accident.

*It answers a malformed code without touching the database.* The check symbol
is verified first. That refuses a smudged label instantly and keeps a stranger
from using this endpoint to probe the items table with codes that cannot exist.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.hashing import sha256_hex
from app.core.ratelimit import rate_limit
from app.db.session import get_session
from app.verification import claiming, scan as scan_module, service
from app.verification.schemas import PublicItemView, ScanRequest

__all__ = ["router"]

_settings = get_settings()

router = APIRouter(prefix=f"{_settings.public_prefix}/v", tags=["public"])

# The read is limited too, not just the write. A public GET that recomputes a
# keccak256 and walks a lineage is not free, and the one endpoint with no
# credential in front of it is the one most worth putting a ceiling on.
_read_limit = rate_limit(
    "public_verify", _settings.rate_limit_scan_per_minute, 60
)
_scan_limit = rate_limit(
    "public_scan", _settings.rate_limit_scan_per_minute, 60
)


def _chain_reader(request: Request) -> service.ChainReader | None:
    """Adapt the running chain client to the two questions this module asks.

    Returns ``None`` -- meaning "do not attempt a live read" -- whenever there
    is no runtime, no contract binding, or no working connection. That is the
    normal state locally, where nothing is deployed and writes are off, so the
    absence of a reader is an ordinary path and not a failure branch.
    """
    runtime = getattr(request.app.state, "chain_runtime", None)
    if runtime is None or runtime.binding is None or not runtime.client.available:
        return None

    binding = runtime.binding
    client = runtime.client

    class _Reader:
        async def is_item_anchored(self, item_hash: str) -> bool:
            data = binding.encode_is_item_anchored(item_hash)
            returned = await client.call({"to": binding.address, "data": _calldata(data)})
            return bool(binding.decode_bool(returned))

        async def is_batch_anchored(self, root: str) -> bool:
            data = binding.encode_is_batch_anchored(root)
            returned = await client.call({"to": binding.address, "data": _calldata(data)})
            return bool(binding.decode_bool(returned))

    return _Reader()


def _calldata(encoded: bytes) -> str:
    """The ``data`` field of an ``eth_call``, in the form the wire defines.

    ``eth_call``'s ``data`` parameter is a ``0x``-prefixed hex string.
    web3.py hexlifies raw bytes on the way out, so passing them worked against a
    real provider and hid the fact that this function was sending something the
    JSON-RPC spec does not describe -- which any other client, including the
    offline node these tests run against, is entitled to reject.
    """
    return f"0x{encoded.hex()}"


def _etag(payload: PublicItemView) -> str:
    """Weak-ish validator over the payload's stable parts.

    ``chain_checked_at`` is excluded on purpose: it moves on every request by
    construction, and including it would produce a new ETag every time and make
    the header decorative.
    """
    body: dict[str, Any] = payload.model_dump(mode="json")
    chain = body.get("chain")
    if isinstance(chain, dict):
        chain.pop("chain_checked_at", None)
    serialised = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return f'"{sha256_hex(serialised.encode("utf-8"))[:32]}"'


def _cached(response: Response, payload: PublicItemView) -> None:
    response.headers["Cache-Control"] = f"public, max-age={_settings.public_cache_seconds}"
    response.headers["ETag"] = _etag(payload)


@router.get(
    "/{tag_code}",
    response_model=PublicItemView,
    dependencies=[Depends(_read_limit)],
    summary="The public record behind one tag",
)
async def verify(
    tag_code: str,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> PublicItemView | Response:
    """Recompute, compare, and report. Reads only -- no scan is recorded here.

    A GET that wrote a scan row would mean every link preview, every crawler
    and every browser prefetch counted as somebody holding the object.
    """
    item = await service.load_item_by_tag(session, tag_code)
    context = scan_module.build_context(request)
    claim = await claiming.read_claim(session, item.id, context.fingerprint_hash)

    payload = await service.build_view(
        session, item, claim, reader=_chain_reader(request), settings=_settings
    )

    tag = _etag(payload)
    if request.headers.get("if-none-match") == tag:
        # 304 carries no body, so this returns a bare response rather than the
        # payload -- a "not modified" with a document in it is neither.
        return Response(
            status_code=status.HTTP_304_NOT_MODIFIED,
            headers={
                "Cache-Control": f"public, max-age={_settings.public_cache_seconds}",
                "ETag": tag,
            },
        )

    _cached(response, payload)
    return payload


@router.post(
    "/{tag_code}/scan",
    response_model=PublicItemView,
    dependencies=[Depends(_scan_limit)],
    summary="Record a scan of one tag",
)
async def record(
    tag_code: str,
    request: Request,
    response: Response,
    payload: ScanRequest | None = None,
    session: AsyncSession = Depends(get_session),
) -> PublicItemView:
    """Record the scan, score the pattern, attempt the claim, return the record.

    Order matters. The scan is written first so the pattern being scored
    includes the scan being made -- otherwise the response is always one scan
    behind the thing it exists to notice. The claim is attempted after, because
    a claim is a consequence of a scan having happened.
    """
    item = await service.load_item_by_tag(session, tag_code)
    body = payload or ScanRequest()

    context = scan_module.build_context(
        request,
        device_fingerprint=body.device_fingerprint,
        region_code=body.region_code,
        settings=_settings,
    )
    _scan_row, _recorded, verdict = await scan_module.record_scan(
        session, item.id, item.tag_code or tag_code, context, _settings
    )
    claim = await claiming.attempt_claim(session, item.id, context)

    view = await service.build_view(
        session,
        item,
        claim,
        reader=_chain_reader(request),
        # The verdict `record_scan` already computed over this tag's whole
        # history, including the scan just written. Recomputing it inside
        # `build_view` would reload every scan row a second time to reach the
        # same answer.
        verdict=verdict,
        settings=_settings,
    )
    await session.commit()

    # A scan is a write. It is never cached, and the response is specific to the
    # device that made it -- the claim block differs per caller.
    response.headers["Cache-Control"] = "no-store"
    response.status_code = status.HTTP_201_CREATED if _recorded else status.HTTP_200_OK
    # Reported so a client can tell a deduplicated retry from a fresh scan
    # without diffing the payload.
    response.headers["X-Scan-Recorded"] = "true" if _recorded else "false"
    return view
