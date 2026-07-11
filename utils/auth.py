"""Vibry AI Core — Token-based authentication

Supports three modes:
1. Admin token (internal HMAC) → user_id = 'admin'
2. API token (DB lookup, vsk_xxx) → user_id = stored label
3. Bare string (backward compatible) → user_id = the string itself
4. No token → user_id = 'anonymous'
"""

from fastapi import Request


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
