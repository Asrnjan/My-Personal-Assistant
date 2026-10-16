"""Print a fresh VAPID key pair for Web Push, ready to paste into .env.

Run with the backend's Python (it needs ``cryptography``):

    backend/.venv/bin/python scripts/generate_vapid_keys.py
"""

from __future__ import annotations

import base64

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def generate() -> tuple[str, str]:
    """Return (public, private): the uncompressed P-256 point the browser needs and
    the raw 32-byte private scalar pywebpush accepts, both base64url without padding."""
    key = ec.generate_private_key(ec.SECP256R1())
    private = key.private_numbers().private_value.to_bytes(32, "big")
    public = key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    return b64url(public), b64url(private)


def main() -> None:
    public, private = generate()
    print(f"VAPID_PUBLIC_KEY={public}")
    print(f"VAPID_PRIVATE_KEY={private}")


if __name__ == "__main__":
    main()
