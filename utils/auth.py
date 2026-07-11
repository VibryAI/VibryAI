"""Vibry AI Core — Token-based authentication

Supports three modes:
1. Admin token (internal HMAC) → user_id = 'admin'
2. API token (DB lookup, vsk_xxx) → user_id = stored label
3. Bare string (backward compatible) → user_id = the string itself
4. No token → user_id = 'anonymous'
"""

import hashlib
import hmac
import os
import time

from fastapi import Request


# ---------------------------------------------------------------------------
# Admin token signing key (shared across routers)
# ---------------------------------------------------------------------------

_admin_signing_key = hashlib.sha256(os.getenv("ADMIN_PASSWORD", "vibry2024").encode()).digest()


def update_admin_signing_key(password: str):
    """Re-derive signing key after password change (called by admin router)."""
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
        expected = hmac.new(_admin_signing_key, payload.encode(), hashlib.sha256).hexdigest()[:32]
        return hmac.compare_digest(parts[2], expected)
    except (ValueError, IndexError):
        return False


def check_admin(request: Request) -> bool:
    """Check if the request carries a valid admin token."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    return _verify_admin_token(auth[7:])


# ---------------------------------------------------------------------------
# User resolution
# ---------------------------------------------------------------------------

def resolve_user_id(request: Request) -> str:
    """Extract user identity from Authorization: Bearer <token> header."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return "anonymous"

    token = auth[7:].strip()
    if not token:
        return "anonymous"

    # Admin token — internal HMAC format
    if token.startswith("admin.") and len(token) > 40:
        return "admin"

    # API token — vsk_xxx format, lookup in DB
    if token.startswith("vsk_"):
        try:
            import db
            user = db.resolve_token(token)
            if user:
                return user
        except Exception:
            pass

    # Fallback: bare string as user_id (legacy / anonymous mode)
    return token
