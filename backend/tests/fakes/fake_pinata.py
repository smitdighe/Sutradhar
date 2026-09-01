"""A Pinata stand-in, and the byte fixtures the sniffer has to recognise.

Built on respx so the real HTTP client is exercised: timeouts, headers, status
handling and JSON parsing are the production code path, and only the socket is
replaced. A hand-rolled stub of :class:`~app.media.pinata.PinataClient` would
prove the stub works.

Nothing here ever reaches ``api.pinata.cloud``. A test that quietly hit the real
service would be slow, flaky, dependent on somebody's free-tier quota, and would
pin junk to IPFS permanently.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
import respx

from app.media.pinata import PIN_ENDPOINT

__all__ = [
    "EXE_BYTES",
    "JPEG_BYTES",
    "MP4_BYTES",
    "PNG_BYTES",
    "WEBP_BYTES",
    "FakePinata",
    "fake_cid",
    "pinata_down",
    "pinata_ok",
    "pinata_rejects",
]


# --------------------------------------------------------------- byte fixtures
#
# Real magic numbers with filler behind them. The sniffer only reads the first
# 32 bytes, so these are honest inputs to it while staying small enough to keep
# in source.

JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"\x00" * 64
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 64
WEBP_BYTES = b"RIFF" + (100).to_bytes(4, "little") + b"WEBPVP8 " + b"\x00" * 64
MP4_BYTES = (32).to_bytes(4, "big") + b"ftypisom" + b"\x00" * 64

# A Windows executable, which a client may cheerfully label image/jpeg. The
# sniffer has to refuse it on the bytes alone.
EXE_BYTES = b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 64


def fake_cid(data: bytes) -> str:
    """A deterministic stand-in CID. Shaped like one, derived from the content."""
    return "bafy" + hashlib.sha256(data).hexdigest()[:46]


@dataclass
class FakePinata:
    """Records what the client sent, so a test can assert it was never called."""

    calls: list[dict[str, Any]] = field(default_factory=list)

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def never_called(self) -> bool:
        return not self.calls

    @property
    def last_payload_size(self) -> int:
        """Size of the file in the most recent call, envelope excluded."""
        return int(self.calls[-1]["size"]) if self.calls else 0

    def saw_authorization(self) -> bool:
        """Whether a bearer token actually reached the wire."""
        return any("authorization" in call["headers"] for call in self.calls)


def _file_part(body: bytes) -> bytes:
    """Pull the uploaded file out of a multipart body.

    A real CID is the hash of the *file*, not of the multipart envelope around
    it. Deriving the fake CID from the envelope would make it depend on httpx's
    randomly generated boundary, so a test could never predict it -- and the
    property worth testing is that the CID the client stores corresponds to the
    bytes that were sent.
    """
    marker = b"\r\n\r\n"
    start = body.find(marker)
    if start == -1:
        return body
    start += len(marker)
    end = body.find(b"\r\n--", start)
    return body[start:end] if end != -1 else body[start:]


def _record(recorder: FakePinata, request: httpx.Request) -> bytes:
    body = request.content
    payload = _file_part(body)
    recorder.calls.append(
        {
            "headers": {k.lower(): v for k, v in request.headers.items()},
            "size": len(payload),
            "envelope_size": len(body),
        }
    )
    return payload


@contextmanager
def pinata_ok(recorder: FakePinata | None = None) -> Iterator[FakePinata]:
    """Pinata accepts everything and returns a CID derived from the bytes."""
    tracker = recorder or FakePinata()

    def responder(request: httpx.Request) -> httpx.Response:
        payload = _record(tracker, request)
        return httpx.Response(
            200,
            json={
                "IpfsHash": fake_cid(payload),
                "PinSize": len(payload),
                "Timestamp": "2026-08-28T00:00:00.000Z",
            },
        )

    with respx.mock:
        respx.post(PIN_ENDPOINT).mock(side_effect=responder)
        yield tracker


@contextmanager
def pinata_down(recorder: FakePinata | None = None) -> Iterator[FakePinata]:
    """The connection never completes -- the most common real failure."""
    tracker = recorder or FakePinata()

    def responder(request: httpx.Request) -> httpx.Response:
        _record(tracker, request)
        raise httpx.ConnectError("connection refused", request=request)

    with respx.mock:
        respx.post(PIN_ENDPOINT).mock(side_effect=responder)
        yield tracker


@contextmanager
def pinata_rejects(
    status_code: int = 429, recorder: FakePinata | None = None
) -> Iterator[FakePinata]:
    """Pinata answers, and the answer is no. Quota exhaustion looks like this."""
    tracker = recorder or FakePinata()

    def responder(request: httpx.Request) -> httpx.Response:
        _record(tracker, request)
        return httpx.Response(status_code, json={"error": "over the free tier limit"})

    with respx.mock:
        respx.post(PIN_ENDPOINT).mock(side_effect=responder)
        yield tracker
