"""Identifier generation: UUIDv7 primary keys and human-typeable tag codes.

Two distinct namespaces that are never interchangeable:

``new_id``
    UUIDv7, canonical hyphenated string. The timestamp prefix makes these
    sort in generation order, which keeps keyset pagination on ``(created_at,
    id)`` stable and B-tree inserts local.

``new_tag_code``
    The 12-character code physically printed on a textile's tag. Optimised for
    a human reading it off a label and typing it into a phone.
"""

from __future__ import annotations

import secrets
import uuid

import uuid_utils

__all__ = [
    "TAG_ALPHABET",
    "TAG_CHECKSUM_MODULUS",
    "TAG_CODE_LENGTH",
    "new_id",
    "new_tag_code",
    "new_uuid",
    "normalize_tag_code",
    "validate_tag_code",
]

# Crockford base32 already omits I, L, O and U. Dropping the remaining vowels
# A and E leaves 30 symbols, which removes every letter that can be misread as
# another letter and every letter that can spell a word on a printed tag. Z is
# then dropped as well, for the reason below -- and losing it is no hardship,
# since Z and 2 are a classic handwriting confusion anyway.
TAG_ALPHABET = "0123456789BCDFGHJKMNPQRSTVWXY"
TAG_CODE_LENGTH = 12
_DATA_LENGTH = TAG_CODE_LENGTH - 1

# Crockford's own check symbol is mod 37, which is unrepresentable here: the
# alphabet cannot encode residues beyond its own size without the extra
# check-only symbols (*, ~, $, =, U), and those reintroduce ambiguity and
# punctuation.
#
# 29 is prime AND equal to the alphabet size, and both halves matter. With a
# positionally weighted sum, transposing adjacent symbols shifts the checksum by
# exactly (v[i] - v[i+1]), so a transposition goes undetected precisely when the
# two symbol indices are congruent mod 29. Symbol count == modulus makes the
# index-to-residue map a bijection, so that happens only when the symbols are
# equal -- and swapping two identical symbols is not an error.
#
# A 30-symbol alphabet would leave exactly one blind spot, '0' against 'Z',
# because 0 and 29 are congruent. That is a small hole, but it is a hole in the
# one guarantee a check character exists to provide.
TAG_CHECKSUM_MODULUS = 29

_INDEX = {char: position for position, char in enumerate(TAG_ALPHABET)}
# Characters the alphabet excludes, mapped to what the reader almost certainly
# saw on the label. Folding is safe because none of these can legitimately
# appear in a code, so the only way to type one is to have misread something.
# Crockford's own fold rules, plus Z, which this alphabet drops. A and E are
# deliberately absent: they were removed for being vowels, not for looking like
# anything, so there is no obvious character they should fold to.
_AMBIGUOUS = {"I": "1", "L": "1", "O": "0", "U": "V", "Z": "2"}


def new_uuid() -> uuid.UUID:
    """Return a fresh UUIDv7 as a :class:`uuid.UUID`."""
    return uuid.UUID(str(uuid_utils.uuid7()))


def new_id() -> str:
    """Return a fresh UUIDv7 in canonical hyphenated string form."""
    return str(new_uuid())


def _checksum(data: str) -> str:
    """Positionally weighted mod-29 check symbol over *data*."""
    total = sum((position + 1) * _INDEX[char] for position, char in enumerate(data))
    return TAG_ALPHABET[total % TAG_CHECKSUM_MODULUS]


def new_tag_code() -> str:
    """Return a fresh 12-character tag code: 11 random symbols plus a check symbol.

    11 symbols over a 29-symbol alphabet is about 53 bits of entropy, so tag
    codes are unguessable as well as collision-free at any realistic scale.
    """
    data = "".join(secrets.choice(TAG_ALPHABET) for _ in range(_DATA_LENGTH))
    return data + _checksum(data)


def normalize_tag_code(code: str) -> str:
    """Fold user input into canonical form.

    Uppercases, strips hyphens, spaces and underscores, and maps the characters
    the alphabet deliberately excludes onto what the reader almost certainly
    meant: I and L to 1, O to 0, U to V, Z to 2.
    """
    stripped = code.upper().replace("-", "").replace(" ", "").replace("_", "")
    return "".join(_AMBIGUOUS.get(char, char) for char in stripped)


def validate_tag_code(code: str) -> bool:
    """True when *code* is the right length, in-alphabet, and checksum-valid."""
    candidate = normalize_tag_code(code)
    if len(candidate) != TAG_CODE_LENGTH:
        return False
    if any(char not in _INDEX for char in candidate):
        return False
    return _checksum(candidate[:_DATA_LENGTH]) == candidate[-1]
