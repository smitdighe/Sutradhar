"""The framing guard: this system reports evidence and never delivers a verdict.

A provenance system that says "authentic" has claimed something it cannot know.
The chain records that a person typed a claim; it cannot tell handloom from
powerloom, and no amount of cryptography changes that. The moment a payload,
an enum value or a log line says "genuine", the product is making a promise it
will eventually break in public, in front of the one consumer who checks.

So the vocabulary is enforced mechanically rather than left to reviewer
attention. Two tiers:

**Banned outright.** Terms that could only ever be a verdict about a physical
object. No allowlist, no exceptions, anywhere under ``app/``.

**Banned with a narrow allowlist.** Ordinary English and protocol vocabulary
that collides with the ban list -- "authentication" is the HTTP and OAuth term of
art and has nothing to do with whether a saree is what it claims to be; "genuine
failure" is plain English about an error. Each allowed line is listed explicitly
with the reason it is allowed.

The allowlist cannot rot: every entry must still match something, so a stale
exemption fails the suite instead of quietly widening the hole.

Approved vocabulary, for reference: "verified provenance", "chain of custody
recorded", "self-declared", "co-op attested", "inspected", "disputed".
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

APP_DIR = Path(__file__).resolve().parents[2] / "app"

# Tier 1. A verdict about a physical object, in any casing, in any file. There is
# no context in this codebase where these are defensible.
FORBIDDEN_ABSOLUTE: dict[str, str] = {
    r"counterfeit[\s_.-]?proof": "claims an object cannot be faked; nothing here can know that",
    r"\bis_fake\b": "a boolean verdict about a physical object",
    r"\bis_real\b": "a boolean verdict about a physical object",
    r"\bVERIFIED_AUTHENTIC\b": "an enum value asserting a verdict",
    r'"verified"\s*:\s*true': "a payload field asserting a verdict",
    r"\bguaranteed[\s_-]?(?:genuine|authentic|real)\b": "a guarantee about an object",
    r"\bcertified[\s_-]?(?:genuine|authentic|real)\b": "a guarantee about an object",
    r"\bproof[\s_-]?of[\s_-]?authenticity\b": "asserts authenticity as a provable property",
}

# Tier 2. Words that are usually a framing violation but have legitimate
# non-product uses. Every occurrence must appear in ALLOWED below.
FORBIDDEN_UNLESS_ALLOWED: dict[str, str] = {
    r"\bgenuine\b": "reads as a verdict about the object unless it is plain English about a bug",
    r"\bgenuinely\b": "same as 'genuine'",
    r"\bauthentic\b": "reads as a verdict about the object",
    r"\bauthenticity\b": "reads as a verdict about the object",
    r"\bcounterfeit\b": "the vocabulary this product deliberately avoids",
}

# (path suffix, substring that must appear on the line, why it is allowed).
# Matched on the *line*, so moving code does not silently re-open the hole --
# the substring has to still be there.
ALLOWED: tuple[tuple[str, str, str], ...] = (
    # --- HTTP and OAuth vocabulary. "Authentication" is the protocol term for
    # establishing who is calling, and has nothing to do with product framing.
    ("app/api/health.py", "testAuthentication", "Pinata's API endpoint name"),
    ("app/api/health.py", '_item("ok", "authenticated")', "reports a successful API login"),
    ("app/auth/guards.py", "return 403", "docstring about HTTP authentication"),
    ("app/auth/guards.py", 'message="authentication required"', "401 message"),
    ("app/auth/guards.py", "Require a valid bearer token", "docstring"),
    ("app/auth/oauth/google.py", "bearer-authenticated JSON endpoint", "describes OAuth userinfo"),
    ("app/auth/oauth/router.py", "this browser is not authenticated", "OAuth callback comment"),
    ("app/auth/pending.py", "It cannot authenticate anywhere", "pending-token docstring"),
    ("app/auth/router.py", "authenticate,", "import of the login function"),
    ("app/auth/router.py", "authenticate(session", "call to the login function"),
    ("app/auth/router.py", "the authenticated user", "docstring about the caller"),
    ("app/auth/service.py", '"authenticate"', "__all__ entry for the login function"),
    ("app/auth/service.py", "async def authenticate", "the login function"),
    ("app/auth/__init__.py", "Authentication and session-token issuance", "package docstring"),
    # --- The one place the banned vocabulary is the subject matter. This
    # docstring explains the laundering attack the mass-balance rule defends
    # against, and naming it is the point: "one certificate attached to more
    # objects than it covers" is the failure other provenance systems miss
    # precisely because they never say it out loud. Rewording it would cost the
    # reader the explanation and gain nothing.
    (
        "app/provenance/mass_balance.py",
        "that both scan as genuine",
        "names the attack the module exists to prevent",
    ),
    (
        "app/provenance/mass_balance.py",
        "how counterfeit goods get",
        "names the attack the module exists to prevent",
    ),
    (
        "app/provenance/mass_balance.py",
        "attaching one genuine certificate to more objects",
        "names the attack the module exists to prevent",
    ),
    # --- The trust ladder's own docstring, which has to name the words it
    # refuses to emit in order to explain why it refuses.
    (
        "app/attestation/trust.py",
        "does not answer",
        "the docstring stating what the module will not claim",
    ),
)


def python_sources() -> list[Path]:
    return sorted(
        path
        for path in APP_DIR.rglob("*.py")
        if "__pycache__" not in path.parts
    )


def _relative(path: Path) -> str:
    return path.relative_to(APP_DIR.parent).as_posix()


def _is_allowed(rel_path: str, line: str) -> bool:
    return any(
        rel_path.endswith(suffix) and needle in line
        for suffix, needle, _reason in ALLOWED
    )


class TestNoVerdictLanguage:
    def test_there_are_sources_to_scan(self) -> None:
        # Guards against the whole suite passing on an empty glob.
        assert len(python_sources()) >= 40

    @pytest.mark.parametrize("pattern,why", sorted(FORBIDDEN_ABSOLUTE.items()))
    def test_absolutely_forbidden_terms_appear_nowhere(self, pattern: str, why: str) -> None:
        compiled = re.compile(pattern, re.IGNORECASE)
        hits: list[str] = []

        for path in python_sources():
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if compiled.search(line):
                    hits.append(f"{_relative(path)}:{number}: {line.strip()}")

        assert not hits, f"{pattern} ({why}) found at:\n" + "\n".join(hits)

    @pytest.mark.parametrize("pattern,why", sorted(FORBIDDEN_UNLESS_ALLOWED.items()))
    def test_conditional_terms_appear_only_where_allowlisted(
        self, pattern: str, why: str
    ) -> None:
        compiled = re.compile(pattern, re.IGNORECASE)
        hits: list[str] = []

        for path in python_sources():
            rel = _relative(path)
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if compiled.search(line) and not _is_allowed(rel, line):
                    hits.append(f"{rel}:{number}: {line.strip()}")

        assert not hits, (
            f"{pattern} ({why}) found outside the allowlist at:\n"
            + "\n".join(hits)
            + "\n\nUse the approved vocabulary, or add an entry to ALLOWED with a reason."
        )

    def test_the_allowlist_does_not_rot(self) -> None:
        """Every exemption must still match a real line.

        An allowlist entry that no longer matches anything is a hole left open
        for code that has since moved or changed. Failing here forces the entry
        to be removed rather than quietly widening what is permitted.
        """
        stale: list[str] = []

        for suffix, needle, reason in ALLOWED:
            if suffix == "tests-are-not-scanned":
                continue
            matched = any(
                _relative(path).endswith(suffix)
                and needle in path.read_text(encoding="utf-8")
                for path in python_sources()
            )
            if not matched:
                stale.append(f"{suffix} :: {needle!r} ({reason})")

        assert not stale, "stale allowlist entries, remove them:\n" + "\n".join(stale)


class TestApprovedVocabularyIsPresent:
    """The positive half: the honest words are actually the ones being used."""

    def test_the_trust_ladder_uses_the_approved_terms(self) -> None:
        from app.attestation.trust import TrustLevel

        values = {level.value for level in TrustLevel}
        assert values == {"SELF_DECLARED", "CO_OP_ATTESTED", "INSPECTED", "DISPUTED"}

    def test_no_trust_level_is_a_claim_about_the_object(self) -> None:
        from app.attestation.trust import TrustLevel

        for level in TrustLevel:
            lowered = level.value.lower()
            assert "real" not in lowered
            assert "fake" not in lowered
            assert "valid" not in lowered


class TestTrustLevelIsNeverStored:
    """DoD: no model may carry a settable trust level."""

    def test_no_model_declares_a_trust_level_column(self) -> None:
        from app.db.base import Base

        offenders = [
            f"{table.name}.{column.name}"
            for table in Base.metadata.tables.values()
            for column in table.columns
            if "trust" in column.name.lower()
        ]
        # A stored level is a level somebody can set, and a level that can go
        # stale against the evidence it claims to summarise.
        assert not offenders, f"trust must be derived, not stored: {offenders}"

    def test_no_source_file_declares_a_trust_level_field(self) -> None:
        pattern = re.compile(r"trust_level\s*[:=]")
        hits = [
            f"{_relative(path)}:{number}"
            for path in python_sources()
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if pattern.search(line)
        ]
        assert not hits, f"trust_level must not be a stored field: {hits}"
