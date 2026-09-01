"""Local mirror storage and the resolution order across all three tiers.

**Why three copies of every file.** The SHA-256 goes on chain, which proves the
bytes were never altered. It does not keep the bytes. IPFS does not keep them
either -- a CID is an address, and a pinning service on a free tier is one
lapsed invoice away from that address resolving to nothing. So the bytes live in
three places, and the chain guarantee survives even if every host disappears.

**The local mirror is ephemeral and that is the point of the third tier.** On
Render's free tier the filesystem is not persistent: it is rebuilt on every
deploy and the mirror directory comes back empty. So the mirror is a fast local
cache, never a durable copy, and the database blob is what actually survives a
redeploy.

Anyone tempted to drop the blob tier as redundant should read that paragraph
again. Two of the three tiers can vanish without anyone doing anything wrong --
a free pinning tier lapsing, a routine redeploy -- and they can vanish on the
same afternoon. The blob is the one that is still there afterwards.

**Resolution order is cheapest-and-most-public first.** Pinata's gateway is a
CDN somebody else pays for and the CID is verifiable by anyone; the mirror is a
local disk read; the blob is a database round trip that competes with real
queries. The response carries *every* available tier, so a browser that finds
the gateway down falls back on its own without asking the API again.
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from app.config import Settings, get_settings
from app.core.logging import get_logger
from app.db.models.enums import PinStatus
from app.db.models.media import Media

__all__ = [
    "MirrorStore",
    "ResolvedMedia",
    "Tier",
    "TierOption",
    "resolve",
]

logger = get_logger(__name__)


class Tier(StrEnum):
    """Where a copy of the bytes can be read from."""

    IPFS = "IPFS"
    MIRROR = "MIRROR"
    BLOB = "BLOB"


@dataclass(frozen=True, slots=True)
class TierOption:
    """One place the bytes are available, and how to fetch them."""

    tier: Tier
    url: str
    # False for the mirror, which a redeploy wipes. Surfaced so a caller can
    # tell a cache from a copy rather than assuming three tiers means three
    # durable copies.
    durable: bool


@dataclass(frozen=True, slots=True)
class ResolvedMedia:
    """Every readable copy of one file, best first."""

    media_id: str
    sha256: str
    tiers: tuple[TierOption, ...]

    @property
    def primary(self) -> TierOption | None:
        return self.tiers[0] if self.tiers else None

    @property
    def urls(self) -> list[str]:
        """Candidate URLs in order. The list the spec asks ``resolve`` for."""
        return [option.url for option in self.tiers]


class MirrorStore:
    """Reads and writes the local ``media_mirror/`` copy.

    Files are named by their own SHA-256, sharded two levels deep. Content
    addressing means writing the same bytes twice is idempotent, and the
    sharding keeps any one directory from collecting tens of thousands of
    entries -- which is where ``ls`` stops working and some filesystems slow
    down badly.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    @property
    def root(self) -> Path:
        return Path(self._settings.ipfs_mirror_dir)

    def path_for(self, sha256: str) -> Path:
        """``<root>/ab/cd/abcdef...`` for a digest. Deterministic, no lookup."""
        digest = sha256.lower()
        return self.root / digest[:2] / digest[2:4] / digest

    def write(self, sha256: str, data: bytes) -> str:
        """Write the mirror copy and return its path, relative to the root.

        Written to a temporary name and moved into place, so a crash mid-write
        cannot leave a truncated file sitting at the path that a digest says
        holds those exact bytes.
        """
        target = self.path_for(sha256)
        target.parent.mkdir(parents=True, exist_ok=True)

        temporary = target.with_suffix(".partial")
        temporary.write_bytes(data)
        shutil.move(str(temporary), str(target))

        relative = str(target.relative_to(self.root))
        logger.debug("media.mirror.written", sha256=sha256, path=relative, bytes=len(data))
        return relative

    def read(self, mirror_path: str | None) -> bytes | None:
        """Read a mirrored file, or ``None`` if a redeploy has taken it."""
        if not mirror_path:
            return None
        candidate = self.root / mirror_path
        if not candidate.is_file():
            return None
        return candidate.read_bytes()

    def exists(self, mirror_path: str | None) -> bool:
        return bool(mirror_path) and (self.root / str(mirror_path)).is_file()

    def verify(self, mirror_path: str, expected_sha256: str) -> bool:
        """Re-hash a mirrored file and compare.

        Cheap paranoia on a tier that lives on a disk nobody promises anything
        about. A silently corrupted mirror serving wrong bytes under a correct
        digest is worse than a missing one.
        """
        data = self.read(mirror_path)
        if data is None:
            return False
        return hashlib.sha256(data).hexdigest() == expected_sha256.lower()


def resolve(
    media: Media,
    settings: Settings | None = None,
    store: MirrorStore | None = None,
) -> ResolvedMedia:
    """Every tier this file can currently be served from, best first.

    A tier is only listed when it is actually readable *now*: a CID is skipped
    unless the row says ``PINNED``, and the mirror is skipped when the file is
    not on disk. Listing a tier that would 404 hands the frontend a fallback
    chain with a hole in it.
    """
    resolved = settings or get_settings()
    mirror = store or MirrorStore(resolved)
    base = resolved.app_base_url.rstrip("/")
    prefix = resolved.api_prefix

    options: list[TierOption] = []

    if media.cid and media.pin_status is PinStatus.PINNED:
        options.append(
            TierOption(
                tier=Tier.IPFS,
                url=f"{resolved.pinata_gateway_url.rstrip('/')}/{media.cid}",
                # Durable only while somebody keeps paying to pin it, which is
                # precisely the assumption this design refuses to make.
                durable=False,
            )
        )

    if mirror.exists(media.mirror_path):
        options.append(
            TierOption(
                tier=Tier.MIRROR,
                url=f"{base}{prefix}/media/{media.id}/raw?tier=MIRROR",
                # Ephemeral on Render's free tier: gone on the next deploy.
                durable=False,
            )
        )

    if media.blob is not None:
        options.append(
            TierOption(
                tier=Tier.BLOB,
                url=f"{base}{prefix}/media/{media.id}/raw?tier=BLOB",
                durable=True,
            )
        )

    return ResolvedMedia(
        media_id=str(media.id), sha256=media.sha256, tiers=tuple(options)
    )
