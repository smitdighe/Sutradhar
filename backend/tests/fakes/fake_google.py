"""A stand-in for Google's OAuth endpoints.

Real RSA keys, real signatures, real JWKS -- the tests exercise the actual
verification path in :mod:`app.auth.oauth.google` rather than stubbing it out.
That matters: the whole point of that module is that it verifies, so a test that
mocks verification away would prove nothing. Only the network is faked.

The keypair is generated once per process and served as a JWKS document, so
:class:`jwt.PyJWKClient` fetches and validates against it exactly as it would
against Google.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.auth.oauth.google import JWKS_URI, TOKEN_ENDPOINT

__all__ = [
    "DEFAULT_EMAIL",
    "DEFAULT_SUBJECT",
    "FakeGoogle",
    "fake_google",
]

DEFAULT_SUBJECT = "108451234567890123456"
DEFAULT_EMAIL = "weaver@gmail.example.com"
KEY_ID = "fake-google-key-1"


def _b64uint(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


@dataclass
class FakeGoogle:
    """Issues ID tokens and serves the JWKS they verify against."""

    client_id: str
    private_key: rsa.RSAPrivateKey = field(
        default_factory=lambda: rsa.generate_private_key(public_exponent=65537, key_size=2048)
    )
    # Set by a test to control what the next exchange returns.
    subject: str = DEFAULT_SUBJECT
    email: str | None = DEFAULT_EMAIL
    email_verified: bool = True
    display_name: str = "Test Weaver"
    issuer: str = "https://accounts.google.com"
    token_status: int = 200

    def jwks(self) -> dict[str, Any]:
        numbers = self.private_key.public_key().public_numbers()
        return {
            "keys": [
                {
                    "kty": "RSA",
                    "use": "sig",
                    "alg": "RS256",
                    "kid": KEY_ID,
                    "n": _b64uint(numbers.n),
                    "e": _b64uint(numbers.e),
                }
            ]
        }

    def id_token(self, **overrides: Any) -> str:
        """Mint a signed ID token, with any claim overridable for negative tests."""
        issued_at = int(time.time())
        claims: dict[str, Any] = {
            "iss": self.issuer,
            "aud": self.client_id,
            "sub": self.subject,
            "email": self.email,
            "email_verified": self.email_verified,
            "name": self.display_name,
            "iat": issued_at,
            "exp": issued_at + 3600,
        }
        claims.update(overrides)
        private_pem = self.private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        return jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": KEY_ID})

    def install(self, respx_mock: Any, **token_overrides: Any) -> None:
        """Route the JWKS and token endpoints at this fake."""
        respx_mock.get(JWKS_URI).mock(
            return_value=httpx.Response(200, json=self.jwks())
        )
        if self.token_status != 200:
            respx_mock.post(TOKEN_ENDPOINT).mock(
                return_value=httpx.Response(self.token_status, json={"error": "invalid_grant"})
            )
            return
        respx_mock.post(TOKEN_ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "fake-access-token",
                    "expires_in": 3599,
                    "token_type": "Bearer",
                    "id_token": self.id_token(**token_overrides),
                },
            )
        )


def fake_google(client_id: str) -> FakeGoogle:
    """Build a fake bound to *client_id*, which must match GOOGLE_CLIENT_ID."""
    return FakeGoogle(client_id=client_id)


def decode_unverified(token: str) -> dict[str, Any]:
    """Read a token's claims without verification. Tests only."""
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))
