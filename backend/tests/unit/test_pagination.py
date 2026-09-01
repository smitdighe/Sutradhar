"""Signed keyset cursors.

Two properties matter: a cursor the server issued must round-trip exactly, and
a cursor the server did not issue must be refused. The second is the security
one -- an unsigned cursor is a client-controlled value spliced into a WHERE
clause.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

import pytest

from app.core.errors import ErrorCode, ValidationError
from app.core.ids import new_uuid
from app.core.pagination import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    clamp_limit,
    decode_cursor,
    encode_cursor,
)

pytestmark = pytest.mark.unit


class TestRoundTrip:
    def test_datetime_key_round_trips_exactly(self) -> None:
        # Microsecond precision must survive: created_at is the sort key, and a
        # truncated cursor would re-show or skip rows at the page boundary.
        key = datetime(2026, 8, 26, 11, 4, 22, 481920, tzinfo=UTC)
        row_id = new_uuid()
        decoded = decode_cursor(encode_cursor(key, row_id))
        assert decoded.key == key
        assert decoded.id == row_id

    def test_current_time_round_trips(self) -> None:
        key = datetime.now(UTC)
        decoded = decode_cursor(encode_cursor(key, new_uuid()))
        assert decoded.key == key

    def test_string_key_round_trips(self) -> None:
        decoded = decode_cursor(encode_cursor("banarasi", new_uuid()), key_is_datetime=False)
        assert decoded.key == "banarasi"

    def test_integer_key_round_trips(self) -> None:
        decoded = decode_cursor(encode_cursor(42, new_uuid()), key_is_datetime=False)
        assert decoded.key == 42

    def test_cursor_is_url_safe(self) -> None:
        cursor = encode_cursor(datetime.now(UTC), new_uuid())
        assert "+" not in cursor
        assert "/" not in cursor
        assert "=" not in cursor

    def test_cursor_is_opaque(self) -> None:
        # The id must not be readable straight out of the cursor string.
        row_id = new_uuid()
        assert str(row_id) not in encode_cursor(datetime.now(UTC), row_id)

    def test_same_input_produces_the_same_cursor(self) -> None:
        key, row_id = datetime.now(UTC), new_uuid()
        assert encode_cursor(key, row_id) == encode_cursor(key, row_id)


class TestForgeryRejection:
    def _valid(self) -> str:
        return encode_cursor(datetime.now(UTC), new_uuid())

    @pytest.mark.parametrize(
        "cursor",
        ["", "x", "not-a-cursor", "!!!!", "a" * 200],
        ids=["empty", "single-char", "words", "punctuation", "long"],
    )
    def test_garbage_is_rejected(self, cursor: str) -> None:
        with pytest.raises(ValidationError) as caught:
            decode_cursor(cursor)
        assert caught.value.code == ErrorCode.INVALID_CURSOR

    def test_truncated_cursor_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            decode_cursor(self._valid()[:-4])

    def test_flipped_character_is_rejected(self) -> None:
        cursor = self._valid()
        flipped = cursor[:-1] + ("A" if cursor[-1] != "A" else "B")
        with pytest.raises(ValidationError):
            decode_cursor(flipped)

    def test_signature_only_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            decode_cursor(base64.urlsafe_b64encode(b"0" * 16).decode().rstrip("="))

    def test_unsigned_payload_is_rejected(self) -> None:
        # The exact structure the encoder produces, minus the HMAC. A client
        # that reverse-engineers the format still cannot mint a cursor.
        payload = json.dumps({"k": "2026-01-01T00:00:00.000000Z", "id": str(new_uuid())})
        forged = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
        with pytest.raises(ValidationError):
            decode_cursor(forged)

    def test_tampered_payload_under_a_valid_signature_is_rejected(self) -> None:
        # Take a real cursor, keep its signature, swap the body underneath.
        cursor = self._valid()
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        signature = raw[:16]
        swapped = json.dumps({"k": "1970-01-01T00:00:00.000000Z", "id": str(new_uuid())}).encode()
        forged = base64.urlsafe_b64encode(signature + swapped).decode().rstrip("=")
        with pytest.raises(ValidationError):
            decode_cursor(forged)

    def test_rejection_message_leaks_nothing(self) -> None:
        # Every failure mode reports identically, so a probing client learns
        # nothing about which part it got wrong.
        messages = set()
        for cursor in ["", "garbage", self._valid()[:-3]]:
            with pytest.raises(ValidationError) as caught:
                decode_cursor(cursor)
            messages.add(caught.value.message)
        assert len(messages) == 1


class TestLimitClamping:
    @pytest.mark.parametrize(
        ("requested", "expected"),
        [
            (None, DEFAULT_LIMIT),
            (1, 1),
            (20, 20),
            (100, MAX_LIMIT),
            (101, MAX_LIMIT),
            (10_000, MAX_LIMIT),
            (0, 1),
            (-5, 1),
        ],
    )
    def test_clamping(self, requested: int | None, expected: int) -> None:
        assert clamp_limit(requested) == expected

    def test_clamping_is_silent(self) -> None:
        # Deliberately not a 422: a client asking for 1000 wants as many as it
        # can have, and failing the request teaches it nothing useful.
        assert clamp_limit(10_000) == MAX_LIMIT
