"""Vibry AI Core — Admin panel API endpoints"""
import hashlib, hmac, os, secrets, time, logging
from pathlib import Path
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from app.config import config
from utils.auth import check_admin, update_admin_signing_key
import db

log = logging.getLogger("vibry.admin")
router = APIRouter()

BASE_DIR = Path(__file__).parent.parent
_admin_signing_key = hashlib.sha256(os.getenv("ADMIN_PASSWORD", "vibry2024").encode()).digest()


def _make_admin_token() -> str:
    from utils.auth import _admin_signing_key
    expiry = str(int(time.time()) + 86400)
    payload = f"admin.{expiry}"
    sig = hmac.new(_admin_signing_key, payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{payload}.{sig}"


# Backward-compatible alias for internal admin checks.
_check_admin = check_admin


def _require_admin(request: Request):
    if not _check_admin(request):
        raise HTTPException(status_code=401, detail="Admin login required")


def _hash_password(password: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), b"vibry_salt_2024", 200_000).hex()


def _verify_password(password: str, stored_hash: str) -> bool:
    return _hash_password(password) == stored_hash


def _get_admin_password_hash() -> str:
    admin = db.get_admin()
    pw_hash = admin.get("password_hash", "")
    if not pw_hash:
        env_pw = os.getenv("ADMIN_PASSWORD", "vibry2024")
        pw_hash = _hash_password(env_pw)
        db.set_admin_password(pw_hash)
    return pw_hash


# ============================================================
# Favicon
# ============================================================

@router.get("/favicon.ico")
async def favicon():
    ico_path = BASE_DIR / "static" / "favicon.ico"
    if ico_path.exists():
        return FileResponse(str(ico_path), media_type="image/x-icon")
    raise HTTPException(status_code=404)


# ============================================================
# Admin Panel
# ============================================================

@router.get("/admin")
@router.get("/admin/")
async def admin_panel():
    html_path = BASE_DIR / "static" / "admin_panel.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    raise HTTPException(status_code=404, detail="admin_panel.html not found")


# ============================================================
# Auth
# ============================================================

@router.post("/admin/api/login")
async def admin_login(request: Request):
    body = await request.json()
    pwd = body.get("password", "")
    code = body.get("code", "")
    if code:
        if db.verify_and_clear_code(code):
            return JSONResponse({"ok": True, "token": _make_admin_token()})
        return JSONResponse({"ok": False, "error": "Invalid code"}, status_code=401)
    stored = _get_admin_password_hash()
    if not _verify_password(pwd, stored):
        return JSONResponse({"ok": False, "error": "Wrong password"}, status_code=401)
    return JSONResponse({"ok": True, "token": _make_admin_token()})


@router.get("/admin/api/verify")
async def admin_verify(request: Request):
    _require_admin(request)
    return JSONResponse({"ok": True})


# ============================================================
# Dashboard
# ============================================================

@router.get("/admin/api/stats")
async def admin_stats(request: Request):
    _require_admin(request)
    stats = db.get_stats()
    usage = db.get_usage_summary()
    return JSONResponse({
        "recordings": stats["total"], "completed": stats["completed"],
        "failed": stats["failed"], "total_calls": usage["total_calls"],
        "total_tokens": usage["total_tokens"], "total_cost": usage["total_cost_rmb"],
        "total_audio_seconds": usage["total_audio_seconds"],
        "token_cost": usage["token_cost_rmb"],
        "audio_cost": usage["audio_cost_rmb"],
        "usage_by_user": db.get_usage_by_user(), "recent_usage": db.get_usage_recent(50),
    })


# ============================================================
# Config (models, ASR, prompts)
# ============================================================

@router.get("/admin/api/config")
async def admin_get_config(request: Request):
    _require_admin(request)
    asr_cfg = db.get_asr_config()
    model_cfg = db.get_model_config()
    return JSONResponse({
        # Chat model
        "chat_model": model_cfg.get("chat_model") or config.chat.model,
        "chat_base_url": model_cfg.get("chat_base_url") or config.chat.base_url,
        "chat_api_key": model_cfg.get("chat_api_key") or config.chat.api_key,
        # Embedding model
        "embedding_model": model_cfg.get("embedding_model") or config.embedding.model,
        "embedding_base_url": model_cfg.get("embedding_base_url") or config.embedding.base_url,
        "embedding_api_key": model_cfg.get("embedding_api_key") or config.embedding.api_key,
        "asr_mode": config.asr.mode,
        "server_host": config.server.host, "server_port": config.server.port,
        "asr_voice_mode": asr_cfg.get("voice_mode", "cloud"),
        "doubao_asr_app_id": asr_cfg.get("app_id", ""),
        "doubao_asr_access_key": asr_cfg.get("access_key", ""),
        "doubao_asr_flash_url": asr_cfg.get("flash_url", ""),
        "doubao_asr_standard_url": asr_cfg.get("standard_url", ""),
        "summary_prompt": asr_cfg.get("summary_prompt", ""),
        "insight_prompt": asr_cfg.get("insight_prompt", ""),
    })


@router.post("/admin/api/config")
async def admin_set_config(request: Request):
    _require_admin(request)
    body = await request.json()
    changes = []

    # ---- Chat model config ----
    chat_fields = {"chat_model": "chat_model", "chat_base_url": "chat_base_url", "chat_api_key": "chat_api_key"}
    chat_db = {}
    for jk, dk in chat_fields.items():
        if jk in body:
            val = body[jk]
            chat_db[dk] = val
            changes.append(jk)
    if chat_db:
        # 更新 in-memory config
        if chat_db.get("chat_model"):
            config.chat.model = chat_db["chat_model"]
        if chat_db.get("chat_base_url"):
            config.chat.base_url = chat_db["chat_base_url"]
        if chat_db.get("chat_api_key"):
            config.chat.api_key = chat_db["chat_api_key"]

    # ---- Embedding model config ----
    emb_fields = {"embedding_model": "embedding_model", "embedding_base_url": "embedding_base_url", "embedding_api_key": "embedding_api_key"}
    emb_db = {}
    for jk, dk in emb_fields.items():
        if jk in body:
            val = body[jk]
            emb_db[dk] = val
            changes.append(jk)
    if emb_db:
        if emb_db.get("embedding_model"):
            config.embedding.model = emb_db["embedding_model"]
        if emb_db.get("embedding_base_url"):
            config.embedding.base_url = emb_db["embedding_base_url"]
        if emb_db.get("embedding_api_key"):
            config.embedding.api_key = emb_db["embedding_api_key"]

    # Persist chat + embedding to DB (merge with existing values)
    if chat_db or emb_db:
        existing = db.get_model_config()
        db.set_model_config(
            chat_model=chat_db.get("chat_model", existing.get("chat_model", "")),
            chat_base_url=chat_db.get("chat_base_url", existing.get("chat_base_url", "")),
            chat_api_key=chat_db.get("chat_api_key", existing.get("chat_api_key", "")),
            embedding_model=emb_db.get("embedding_model", existing.get("embedding_model", "")),
            embedding_base_url=emb_db.get("embedding_base_url", existing.get("embedding_base_url", "")),
            embedding_api_key=emb_db.get("embedding_api_key", existing.get("embedding_api_key", "")),
        )

    for key, field in [("asr_mode", "mode")]:
        if key in body:
            setattr(config.asr, field, body[key])
            changes.append(key)
    asr_keys = {
        "doubao_asr_app_id": "app_id", "doubao_asr_access_key": "access_key",
        "doubao_asr_flash_url": "flash_url", "doubao_asr_standard_url": "standard_url",
    }
    asr_db_args = {}
    for jk, dk in asr_keys.items():
        if jk in body:
            val = body[jk]
            asr_db_args[dk] = val
            setattr(config.doubao_asr, dk, val)
            changes.append(jk)

    asr_mode_val = body.get("asr_mode", "")
    voice_mode_val = body.get("asr_voice_mode", "")
    if voice_mode_val:
        changes.append("asr_voice_mode")

    for k in ("summary_prompt", "insight_prompt"):
        if k in body:
            changes.append(k)

    if asr_db_args or asr_mode_val or voice_mode_val or any(
        k in body for k in ("summary_prompt", "insight_prompt")
    ):
        db.set_asr_config(
            app_id=asr_db_args.get("app_id", config.doubao_asr.app_id),
            access_key=asr_db_args.get("access_key", config.doubao_asr.access_key),
            asr_mode=asr_mode_val or config.asr.mode,
            voice_mode=voice_mode_val or config.doubao_asr.voice_mode,
            flash_url=asr_db_args.get("flash_url", config.doubao_asr.flash_url),
            standard_url=asr_db_args.get("standard_url", config.doubao_asr.standard_url),
            summary_prompt=body.get("summary_prompt", ""),
            insight_prompt=body.get("insight_prompt", ""),
        )
    log.info(f"Config updated: {', '.join(changes)}")
    return JSONResponse({"ok": True, "changes": changes})


# ============================================================
# Billing & Logs
# ============================================================

@router.get("/admin/api/billing")
async def admin_billing(request: Request):
    _require_admin(request)
    return JSONResponse({
        "summary": db.get_usage_summary(), "by_user": db.get_usage_by_user(),
        "recent": db.get_usage_recent(100),
    })


@router.get("/admin/api/logs")
async def admin_logs(request: Request, lines: int = 100):
    _require_admin(request)
    log_path = BASE_DIR / "data" / "logs" / "server.log"
    if not log_path.exists():
        return JSONResponse({"lines": ["Log file not found"]})
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        all_lines = f.readlines()
    tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
    return JSONResponse({"lines": [l.rstrip() for l in tail]})


# ============================================================
# Account
# ============================================================

@router.get("/admin/api/admin-profile")
async def admin_profile(request: Request):
    _require_admin(request)
    admin = db.get_admin()
    return JSONResponse({"email": admin.get("email", ""), "has_email": bool(admin.get("email", ""))})


@router.post("/admin/api/change-password")
async def admin_change_password(request: Request):
    _require_admin(request)
    body = await request.json()
    old_pw = body.get("old_password", "")
    new_pw = body.get("new_password", "")
    if len(new_pw) < 4:
        raise HTTPException(status_code=400, detail="Password too short")
    if not _verify_password(old_pw, _get_admin_password_hash()):
        raise HTTPException(status_code=403, detail="Wrong old password")
    db.set_admin_password(_hash_password(new_pw))
    update_admin_signing_key(new_pw)
    return JSONResponse({"ok": True})


@router.post("/admin/api/set-email")
async def admin_set_email(request: Request):
    _require_admin(request)
    body = await request.json()
    email = body.get("email", "").strip()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Invalid email")
    db.set_admin_email(email)
    return JSONResponse({"ok": True, "email": email})


@router.get("/admin/api/email-config")
async def admin_get_email_config(request: Request):
    _require_admin(request)
    from services.email import get_email_status
    return JSONResponse(get_email_status())


@router.post("/admin/api/forgot-password")
async def admin_forgot_password(request: Request):
    body = await request.json()
    email = body.get("email", "").strip()
    admin = db.get_admin()
    admin_email = admin.get("email", "")
    if not admin_email:
        raise HTTPException(status_code=400, detail="Admin email not set")
    if email != admin_email:
        return JSONResponse({"ok": True, "sent": False, "hint": "If email matches, code sent"})
    code = secrets.token_hex(3)[:6].upper()
    from datetime import datetime, timedelta
    expiry = (datetime.now() + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    db.set_verification_code(code, expiry)
    from services.email import send_verification_code
    ok = send_verification_code(admin_email, code)
    return JSONResponse({"ok": True, "sent": ok, "hint": "Code sent" if ok else "Email API failed — check server log for code"})


@router.post("/admin/api/reset-password")
async def admin_reset_password(request: Request):
    body = await request.json()
    code = (body.get("code", "")).upper().strip()
    new_pw = body.get("new_password", "")
    if len(new_pw) < 4:
        raise HTTPException(status_code=400, detail="Password too short")
    if not db.verify_and_clear_code(code):
        raise HTTPException(status_code=403, detail="Invalid code")
    db.set_admin_password(_hash_password(new_pw))
    update_admin_signing_key(new_pw)
    return JSONResponse({"ok": True})


# ============================================================
# Personality & Chat History
# ============================================================

@router.get("/admin/api/personality")
async def admin_get_personality(request: Request):
    _require_admin(request)
    return JSONResponse({"prompt": db.get_personality()})


@router.post("/admin/api/personality")
async def admin_set_personality(request: Request):
    _require_admin(request)
    body = await request.json()
    prompt = body.get("prompt", "")
    if not prompt.strip():
        raise HTTPException(status_code=400, detail="prompt required")
    db.set_personality(prompt)
    return JSONResponse({"ok": True})


@router.get("/admin/api/chat-history")
async def admin_chat_history(request: Request, user_id: str = "admin", conversation_id: str = None, limit: int = 50):
    _require_admin(request)
    # conversation_id 不传时返回该用户全部消息；传入时按会话过滤
    messages = db.get_chat_history(user_id, conversation_id=conversation_id, limit=limit)
    conversations = db.get_chat_conversations(user_id)
    return JSONResponse({"messages": messages, "conversations": conversations})


# ============================================================
# API Token Management
# ============================================================

@router.get("/admin/api/tokens")
async def admin_list_tokens(request: Request):
    _require_admin(request)
    return JSONResponse({"tokens": db.list_api_tokens()})


@router.post("/admin/api/tokens")
async def admin_create_token(request: Request):
    _require_admin(request)
    body = await request.json()
    user_id = body.get("user_id", "").strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")
    result = db.create_api_token(user_id)
    return JSONResponse({"ok": True, **result})


@router.delete("/admin/api/tokens/{tid}")
async def admin_delete_token(request: Request, tid: str):
    _require_admin(request)
    db.delete_api_token(tid)
    return JSONResponse({"ok": True})
