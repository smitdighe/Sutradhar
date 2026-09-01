"""Generate the Ed25519 session-token signing keypair.

Idempotent: an existing private key is never overwritten. Emits
``keys/jwt_access_ed25519`` (PKCS#8 PEM) and ``keys/jwt_access_ed25519.pub``
(SubjectPublicKeyInfo PEM), restricted to the current user.

Run from the backend directory::

    uv run python scripts/gen_keys.py
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

BACKEND_DIR = Path(__file__).resolve().parent.parent
KEYS_DIR = BACKEND_DIR / "keys"
PRIVATE_KEY_PATH = KEYS_DIR / "jwt_access_ed25519"
PUBLIC_KEY_PATH = KEYS_DIR / "jwt_access_ed25519.pub"


def restrict_permissions(path: Path) -> None:
    """Reduce *path* to owner-only read/write on both POSIX and Windows."""
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    if sys.platform != "win32":
        return
    # NTFS ignores the POSIX mode bits, so reset the ACL to the current user only.
    user = os.environ.get("USERNAME")
    if not user:
        print(f"  ! USERNAME unset; ACL not tightened on {path.name}")
        return
    result = subprocess.run(
        ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(R,W)"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(f"  ! icacls failed on {path.name}: {result.stderr.strip()}")


def main() -> int:
    KEYS_DIR.mkdir(parents=True, exist_ok=True)

    if PRIVATE_KEY_PATH.exists():
        print(f"key already present, leaving untouched: {PRIVATE_KEY_PATH}")
        if not PUBLIC_KEY_PATH.exists():
            print(
                f"  ! public key missing: {PUBLIC_KEY_PATH} —"
                " delete the private key to regenerate"
            )
            return 1
        return 0

    private_key = ed25519.Ed25519PrivateKey.generate()

    PRIVATE_KEY_PATH.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    PUBLIC_KEY_PATH.write_bytes(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )

    restrict_permissions(PRIVATE_KEY_PATH)
    restrict_permissions(PUBLIC_KEY_PATH)

    print(f"wrote {PRIVATE_KEY_PATH}")
    print(f"wrote {PUBLIC_KEY_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
