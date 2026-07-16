"""Authentication helpers for the VibryAI single-user deployment."""

import hashlib
import hmac
import os
import time

from fastapi import Request


_admin_signing_key = hashlib.sha256(
    os.getenv("ADMIN_PASSWORD", "vibry2024").encode()
).digest()

# VibryAI currently has one personal memory and knowledge space. Device tokens
# identify the caller but do not create separate tenants.
SINGLE_USER_ID = "admin"


def update_admin_signing_key(password: str):
    """Re-derive the admin signing key after a password change."""
    global _admin_signing_key
    _admin_signing_key = hashlib.sha256(password.encode()).digest()


def _verify_admin_token(token: str) -> bool:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return False
        payload = f"{parts[0]}.{parts[1]}"
        if time.time() > int(parts[1]):
            return False
        expected = hmac.new(
            _admin_signing_key, payload.encode(), hashlib.sha256
        ).hexdigest()[:32]
        return hmac.compare_digest(parts[2], expected)
    except (ValueError, IndexError):
        return False


def check_admin(request: Request) -> bool:
    """Check whether a request carries a valid admin token."""
    auth = request.headers.get("Authorization", "")
    return auth.startswith("Bearer ") and _verify_admin_token(auth[7:])


def resolve_user_id(request: Request) -> str:
    """Return the single personal identity for every connector request."""
    return SINGLE_USER_ID
