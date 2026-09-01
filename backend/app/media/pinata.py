"""The Pinata pinning client. One network call, treated as untrusted.

**IPFS stores nothing by itself.** A CID is an address, not storage; the bytes
live wherever somebody chose to keep them. A pinning service is that somebody,
and a free tier that lapses takes the bytes with it. The CID then resolves to
nothing while the chain still points at it -- the "immutable" record breaking in
the most embarrassing way available, in front of whoever is checking.

So this module is deliberately the *least* trusted tier. It can fail, it can be
unconfigured, it can hang, and none of those may fail a weaver's upload. The
integrity proof is the SHA-256, which is computed and persisted before this
module is ever called; pinning only decides whether the bytes are also on IPFS.

Three rules:

**Unconfigured is a state, not an error.** With ``PINATA_JWT`` unset the client
reports itself disabled, uploads still return 201, everything lands
``PIN_PENDING``, and ``/readyz`` says ``unconfigured``. Nothing raises.

**Every call has a timeout.** A hung upload to a third party must not hold a
request open, and "no timeout" is how one slow dependency becomes an exhausted
connection pool.

**The JWT never leaves this module.** Not in a log line, not in an exception
message, not in an error body returned to a caller. httpx puts request headers
in some exception reprs, so failures are re-raised as this module's own error
type carrying only a status code and a truncated body.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.core.logging import get_logger

__all__ = [
    "PIN_ENDPOINT",
    "PinResult",
    "PinataClient",
    "PinataError",
    "PinataUnconfigured",
]

logger = get_logger(__name__)

PIN_ENDPOINT = "https://api.pinata.cloud/pinning/pinFileToIPFS"

# Enough of a failure body to debug with, short enough that a provider echoing
# the request back cannot smuggle a credential into a log line.
MAX_ERROR_BODY = 300


class PinataError(RuntimeError):
    """A pin attempt failed. Carries no credential, by construction."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class PinataUnconfigured(PinataError):
    """No ``PINATA_JWT``. Not a failure -- a feature nobody switched on."""


@dataclass(frozen=True, slots=True)
class PinResult:
    """What a successful pin returned."""

    cid: str
    pin_size: int | None = None


class PinataClient:
    """Pins bytes to IPFS through Pinata, or reports honestly that it cannot."""

    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._client = client

    @property
    def enabled(self) -> bool:
        """Whether a pin could be attempted at all."""
        return self._settings.pinata_enabled

    def gateway_url(self, cid: str) -> str:
        """Public URL for a pinned CID."""
        return f"{self._settings.pinata_gateway_url.rstrip('/')}/{cid}"

    def _headers(self) -> dict[str, str]:
        # Built per call and never stored on the instance, so a repr of this
        # object cannot contain the credential.
        return {"Authorization": f"Bearer {self._settings.pinata_jwt.strip()}"}

    async def pin(self, data: bytes, filename: str, content_type: str) -> PinResult:
        """Pin *data* and return its CID.

        Raises :class:`PinataUnconfigured` when there is no JWT, and
        :class:`PinataError` for anything else. Both are expected outcomes the
        caller handles by leaving the row ``PIN_PENDING``; neither is a reason
        to fail the upload that produced the bytes.
        """
        if not self.enabled:
            raise PinataUnconfigured("PINATA_JWT is not set; pinning is disabled")

        timeout = httpx.Timeout(self._settings.pinata_timeout_seconds)
        files = {"file": (filename, data, content_type)}

        try:
            if self._client is not None:
                response = await self._client.post(
                    PIN_ENDPOINT, headers=self._headers(), files=files, timeout=timeout
                )
            else:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.post(
                        PIN_ENDPOINT, headers=self._headers(), files=files
                    )
        except httpx.HTTPError as exc:
            # Re-raised as this module's own type on purpose: an httpx exception
            # repr can include the request, and the request carries the bearer
            # token. Only the class name and message survive.
            raise PinataError(f"pin request failed: {type(exc).__name__}") from None

        if response.status_code >= 400:
            body = response.text[:MAX_ERROR_BODY]
            logger.warning(
                "media.pin.rejected",
                status_code=response.status_code,
                # The body, never the headers.
                detail=body,
            )
            raise PinataError(
                f"pinata returned {response.status_code}", status_code=response.status_code
            )

        payload: dict[str, Any] = response.json()
        cid = payload.get("IpfsHash") or payload.get("cid")
        if not cid:
            raise PinataError("pinata response carried no CID")

        size = payload.get("PinSize")
        logger.info("media.pin.ok", cid=str(cid), pin_size=size)
        return PinResult(cid=str(cid), pin_size=int(size) if size is not None else None)
