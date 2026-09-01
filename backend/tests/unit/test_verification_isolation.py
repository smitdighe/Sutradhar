"""The public module must stay liftable to an edge runtime.

The verification package is the only unauthenticated surface in the system, and
the plausible next move for it is to run somewhere else entirely -- a worker at
the edge, in front of a cold backend, so a shopper scanning a tag is not waiting
on a free-tier instance to wake up. That move is only cheap while the package
does not reach into the authenticated half of the application.

So the boundary is asserted mechanically rather than remembered. Models are
shared -- they are the schema, and two copies of a schema is a worse problem
than a shared import. Pure derivations are shared for the same reason a second
copy of the trust ladder would be worse than one: the first symptom of drift
would be the public view and the private view disagreeing about one object.

What is not shared is anything that knows about a caller: sessions, tokens,
roles, guards, and the routers built on them.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

VERIFICATION_DIR = Path(__file__).resolve().parents[2] / "app" / "verification"

# Prefixes the public package may never import, and why.
FORBIDDEN_PREFIXES: dict[str, str] = {
    "app.auth": "authentication has no meaning on a surface with no callers to identify",
    "app.admin": "moderation tools are not part of a public read",
    "app.provenance.router": "the authenticated item router serialises registrant ids",
    "app.qr.router": "tag issuance is an authenticated write",
    "app.media.router": "the media router is behind a bearer token",
    "app.catalog.router": "the catalog router is behind a bearer token",
    "app.attestation.router": "the attestation router is behind a bearer token",
}


def sources() -> list[Path]:
    return sorted(
        path for path in VERIFICATION_DIR.rglob("*.py") if "__pycache__" not in path.parts
    )


def imported_modules(path: Path) -> set[str]:
    """Every module named by an import in *path*, including inside functions."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


class TestIsolation:
    def test_there_are_sources_to_scan(self) -> None:
        # Guards against the whole class passing on an empty glob.
        assert len(sources()) >= 5

    @pytest.mark.parametrize("prefix,why", sorted(FORBIDDEN_PREFIXES.items()))
    def test_the_public_package_does_not_import(self, prefix: str, why: str) -> None:
        offenders: list[str] = []
        for path in sources():
            for module in imported_modules(path):
                if module == prefix or module.startswith(f"{prefix}."):
                    offenders.append(f"{path.name}: {module}")
        assert not offenders, f"{prefix} ({why}) imported at:\n" + "\n".join(offenders)

    def test_no_authenticated_serialiser_is_reused(self) -> None:
        """Response models are the public package's own, always.

        A schema shared with an authenticated endpoint is one field addition
        away from publishing something that was only ever meant for a caller
        holding a token.
        """
        offenders: list[str] = []
        for path in sources():
            for module in imported_modules(path):
                if module.endswith(".schemas") and not module.startswith("app.verification"):
                    offenders.append(f"{path.name}: {module}")
        assert offenders == []

    def test_the_shared_imports_are_the_ones_intended(self) -> None:
        """A ratchet on what the package is allowed to depend on.

        Not a ban -- these are deliberate, and each is either the schema, the
        frozen hasher, or a pure derivation over them. The test exists so that
        adding a *new* application dependency is a decision somebody makes on
        purpose rather than something that happens in a hurry.
        """
        permitted_roots = {
            "app.config",
            "app.core",
            "app.db",
            "app.verification",
            # The frozen preimage. Verification recomputes exactly what was
            # anchored, so a second implementation is not an option.
            "app.provenance.item_hash",
            # The lineage CTE. Raw SQL over `items`, no caller anywhere in it,
            # and the alternative is a second recursive query that would drift
            # from the authenticated one -- the public and private views
            # disagreeing about an object's parentage is the exact failure this
            # list exists to prevent. Note the entry is the *module*, not the
            # package: `app.provenance.router` must stay forbidden.
            "app.provenance.tree",
            # The trust ladder and the Merkle proof: pure functions over models.
            "app.attestation.trust",
            "app.chain.batching",
        }
        unexpected: set[str] = set()
        for path in sources():
            for module in imported_modules(path):
                if not module.startswith("app."):
                    continue
                if any(
                    module == root or module.startswith(f"{root}.")
                    for root in permitted_roots
                ):
                    continue
                unexpected.add(module)
        assert unexpected == set(), (
            "new application dependencies in the public package: "
            f"{sorted(unexpected)}. Add them above only if they are schema or a "
            "pure derivation, never anything that knows about a caller."
        )
