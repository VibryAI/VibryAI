"""Vibry AI Core — Database Models (CRUD, Config, Stats)"""

import json, os, secrets, sqlite3
from datetime import datetime

from db.connection import get_conn, DB_PATH


# 模型定价 (RMB per 1M tokens) — 用于 chat/summarize/insight 等 LLM 接口
MODEL_PRICES = {
    "doubao-seed-2-1-turbo-260628": {"prompt": 2.0, "completion": 6.0},
    "doubao-seed-2-1-pro-260628": {"prompt": 4.0, "completion": 12.0},
    "doubao-seed-2-0-mini-260428": {"prompt": 0.5, "completion": 1.5},
    "doubao-embedding-text-240715": {"prompt": 0.5, "completion": 0.0},
    "default": {"prompt": 2.0, "completion": 6.0},
}

# ASR 按时长计费 (RMB per 分钟) — 用于 transcribe/voice 等语音转写接口
ASR_PRICES = {
    "doubao_flash": 0.10,       # 豆包极速版
    "doubao_standard": 0.30,    # 豆包标准版（说话人分离）
    "funasr_server": 0.0,       # 本地 FunASR 服务，不计费
    "default": 0.10,
}


def log_usage(
    user_id: str,
    endpoint: str,
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    duration_ms: int = 0,
    audio_seconds: float = 0,
) -> int:
    """记录 API 调用用量

    两种计费模式（互斥）:
    - Token 计费 (chat/summarize/insight): prompt_tokens × token价格 + completion_tokens × token价格
    - 时长计费 (transcribe/voice): audio_seconds / 60 × ASR价格
    """
    if audio_seconds > 0:
        # ASR 时长计费
        asr_price = ASR_PRICES.get(model, ASR_PRICES["default"])
        cost = audio_seconds / 60 * asr_price
    else:
        # LLM token 计费
        prices = MODEL_PRICES.get(model, MODEL_PRICES["default"])
        cost = (prompt_tokens * prices["prompt"] + completion_tokens * prices["completion"]) / 1_000_000

    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO usage_log (user_id, endpoint, model, prompt_tokens, completion_tokens, total_tokens, duration_ms, audio_seconds, cost_rmb)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (user_id, endpoint, model, prompt_tokens, completion_tokens, total_tokens, duration_ms, audio_seconds, round(cost, 6)),
    )
    conn.commit()
    return cur.lastrowid


def get_usage_summary(user_id: str = None) -> dict:
    """获取用量摘要（区分 token 计费和时长计费）"""
    conn = get_conn()
    if user_id:
        row = conn.execute(
            """SELECT
                COALESCE(SUM(total_tokens),0) as tokens,
                COALESCE(SUM(cost_rmb),0) as cost,
                COUNT(*) as calls,
                COALESCE(SUM(audio_seconds),0) as audio_sec,
                COALESCE(SUM(CASE WHEN audio_seconds > 0 THEN cost_rmb ELSE 0 END),0) as audio_cost,
                COALESCE(SUM(CASE WHEN audio_seconds = 0 THEN cost_rmb ELSE 0 END),0) as token_cost
               FROM usage_log WHERE user_id=?""",
            (user_id,),
        ).fetchone()
    else:
        row = conn.execute(
            """SELECT
                COALESCE(SUM(total_tokens),0) as tokens,
                COALESCE(SUM(cost_rmb),0) as cost,
                COUNT(*) as calls,
                COALESCE(SUM(audio_seconds),0) as audio_sec,
                COALESCE(SUM(CASE WHEN audio_seconds > 0 THEN cost_rmb ELSE 0 END),0) as audio_cost,
                COALESCE(SUM(CASE WHEN audio_seconds = 0 THEN cost_rmb ELSE 0 END),0) as token_cost
               FROM usage_log"""
        ).fetchone()
    return {
        "total_tokens": row["tokens"],
        "total_cost_rmb": round(row["cost"], 4),
        "total_calls": row["calls"],
        "total_audio_seconds": round(row["audio_sec"], 1),
        "token_cost_rmb": round(row["token_cost"], 4),
        "audio_cost_rmb": round(row["audio_cost"], 4),
    }


def get_usage_by_user() -> list[dict]:
    """按用户分组的用量（含时长维度）"""
    conn = get_conn()
    rows = conn.execute(
        """SELECT user_id,
            SUM(total_tokens) as tokens,
            SUM(cost_rmb) as cost,
            COUNT(*) as calls,
            COALESCE(SUM(audio_seconds),0) as audio_sec
           FROM usage_log GROUP BY user_id ORDER BY cost DESC"""
    ).fetchall()
    return [dict(r) for r in rows]


# ---- Chat History ----

def save_chat_message(
    user_id: str,
    role: str,
    content: str,
    conversation_id: str = "default",
    model: str = "",
    tokens: int = 0,
) -> int:
    """保存一条聊天消息"""
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO chat_messages (user_id, conversation_id, role, content, model, tokens) VALUES (?,?,?,?,?,?)",
        (user_id, conversation_id, role, content, model, tokens),
    )
    conn.commit()
    return cur.lastrowid


def get_chat_history(
    user_id: str,
    conversation_id: str = None,
    limit: int = 50,
) -> list[dict]:
    """获取聊天历史（conversation_id=None 时返回该用户全部会话消息）"""
    conn = get_conn()
    if conversation_id is not None:
        rows = conn.execute(
            "SELECT * FROM chat_messages WHERE user_id=? AND conversation_id=? ORDER BY created_at DESC LIMIT ?",
            (user_id, conversation_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM chat_messages WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [_row_to_dict(r) for r in reversed(rows)]


def get_chat_conversations(user_id: str) -> list[dict]:
    """获取用户的所有会话列表"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT conversation_id, COUNT(*) as msg_count, MAX(created_at) as last_msg FROM chat_messages WHERE user_id=? GROUP BY conversation_id ORDER BY last_msg DESC",
        (user_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---- Personality ----

DEFAULT_PERSONALITY = """你是 Vibry AI，用户的数字孪生战略副驾。你拥有用户的长期记忆，了解他们的偏好、项目和决策历史。

沟通风格：
- 简洁直接，避免废话
- 数据驱动，注重逻辑
- 主动提醒用户过往的决策和偏好
- 在相关时引用历史记忆

你的价值在于：记住用户说过的一切，在合适的时机提供上下文。"""


def get_personality() -> str:
    """获取当前 Personality system prompt"""
    conn = get_conn()
    conn.execute("INSERT OR IGNORE INTO personality (id, system_prompt) VALUES (1, ?)", (DEFAULT_PERSONALITY,))
    conn.commit()
    row = conn.execute("SELECT system_prompt FROM personality WHERE id=1").fetchone()
    return row["system_prompt"] if row else DEFAULT_PERSONALITY


# ---- Admin Account ----

def get_admin() -> dict:
    """获取管理员信息"""
    conn = get_conn()
    conn.execute("INSERT OR IGNORE INTO admin_users (id, email, password_hash) VALUES (1, '', '')")
    conn.commit()
    row = conn.execute("SELECT * FROM admin_users WHERE id=1").fetchone()
    return dict(row) if row else {}


def set_admin_email(email: str) -> bool:
    conn = get_conn()
    conn.execute("UPDATE admin_users SET email=?, updated_at=datetime('now','localtime') WHERE id=1", (email,))
    conn.commit()
    return True


def set_admin_password(password_hash: str) -> bool:
    conn = get_conn()
    conn.execute("UPDATE admin_users SET password_hash=?, updated_at=datetime('now','localtime') WHERE id=1", (password_hash,))
    conn.commit()
    return True


def set_verification_code(code: str, expiry: str) -> bool:
    conn = get_conn()
    conn.execute("UPDATE admin_users SET verification_code=?, code_expiry=? WHERE id=1", (code, expiry))
    conn.commit()
    return True


def verify_and_clear_code(code: str) -> bool:
    """验证码校验 + 一次性清除"""
    conn = get_conn()
    row = conn.execute("SELECT verification_code, code_expiry FROM admin_users WHERE id=1").fetchone()
    if not row or row["verification_code"] != code:
        return False
    if row["code_expiry"] < __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"):
        return False
    conn.execute("UPDATE admin_users SET verification_code='', code_expiry='' WHERE id=1")
    conn.commit()
    return True


def set_personality(prompt: str) -> bool:
    """更新 Personality system prompt"""
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO personality (id, system_prompt, updated_at) VALUES (1, ?, datetime('now','localtime'))",
        (prompt,),
    )
    conn.commit()
    return True


# ---- ASR Config (DB-persisted, env vars as fallback) ----

DEFAULT_ASR_CONFIG = {
    "app_id": os.getenv("DOUBAO_ASR_APP_ID", ""),
    "access_key": os.getenv("DOUBAO_ASR_ACCESS_KEY", ""),
    "asr_mode": os.getenv("ASR_MODE", "cloud"),
    "voice_mode": os.getenv("ASR_VOICE_MODE", "cloud"),
    "flash_url": os.getenv("DOUBAO_ASR_FLASH_URL",
        "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash"),
    "standard_url": os.getenv("DOUBAO_ASR_STANDARD_URL",
        "https://openspeech-direct.zijieapi.com/api/v3/auc/bigmodel/submit"),
    "summary_prompt": os.getenv("SUMMARY_PROMPT", ""),
    "insight_prompt": os.getenv("INSIGHT_PROMPT", ""),
}


def get_asr_config() -> dict:
    """获取 ASR 配置（DB 优先，env vars 兜底）"""
    conn = get_conn()
    row = conn.execute("SELECT * FROM asr_config WHERE id=1").fetchone()
    if row:
        result = dict(row)
        result.pop("id", None)
        result.pop("updated_at", None)
        # 兼容旧记录无 voice_mode
        if "voice_mode" not in result:
            result["voice_mode"] = "cloud"
        return result
    set_asr_config(**DEFAULT_ASR_CONFIG)
    return dict(DEFAULT_ASR_CONFIG)


def set_asr_config(app_id: str = "", access_key: str = "", asr_mode: str = "cloud",
                   voice_mode: str = "cloud",
                   flash_url: str = "", standard_url: str = "",
                   summary_prompt: str = "", insight_prompt: str = "") -> bool:
    """更新 ASR 配置到数据库"""
    conn = get_conn()
    conn.execute(
        """INSERT OR REPLACE INTO asr_config
           (id, app_id, access_key, asr_mode, voice_mode, flash_url, standard_url,
            summary_prompt, insight_prompt, updated_at)
           VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))""",
        (app_id, access_key, asr_mode, voice_mode, flash_url, standard_url,
         summary_prompt, insight_prompt),
    )
    conn.commit()
    return True


# ---- Model Config (Chat + Embedding) ----

DEFAULT_MODEL_CONFIG = {
    "chat_model": os.getenv("CHAT_MODEL", ""),
    "chat_base_url": os.getenv("CHAT_BASE_URL", ""),
    "chat_api_key": os.getenv("CHAT_API_KEY", ""),
    "embedding_model": os.getenv("EMBEDDING_MODEL", ""),
    "embedding_base_url": os.getenv("EMBEDDING_BASE_URL", ""),
    "embedding_api_key": os.getenv("EMBEDDING_API_KEY", ""),
}


def get_model_config() -> dict:
    """获取 Chat/Embedding 模型配置（DB 优先，env vars 兜底）"""
    conn = get_conn()
    row = conn.execute("SELECT * FROM model_config WHERE id=1").fetchone()
    if row:
        result = dict(row)
        result.pop("id", None)
        result.pop("updated_at", None)
        return result
    set_model_config(**DEFAULT_MODEL_CONFIG)
    return dict(DEFAULT_MODEL_CONFIG)


def set_model_config(
    chat_model: str = "", chat_base_url: str = "", chat_api_key: str = "",
    embedding_model: str = "", embedding_base_url: str = "", embedding_api_key: str = "",
) -> bool:
    """更新 Chat/Embedding 模型配置到数据库"""
    conn = get_conn()
    conn.execute(
        """INSERT OR REPLACE INTO model_config
           (id, chat_model, chat_base_url, chat_api_key,
            embedding_model, embedding_base_url, embedding_api_key,
            updated_at)
           VALUES (1, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))""",
        (chat_model, chat_base_url, chat_api_key,
         embedding_model, embedding_base_url, embedding_api_key),
    )
    conn.commit()
    return True


def count_usage() -> int:
    conn = get_conn()
    return int(conn.execute("SELECT COUNT(*) AS count FROM usage_log").fetchone()["count"])


def get_usage_recent(limit: int = 50, offset: int = 0) -> list[dict]:
    """最近用量记录"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM usage_log ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (max(1, limit), max(0, offset)),
    ).fetchall()
    return [dict(r) for r in rows]


def generate_id(filename: str = "") -> str:
    """从文件名生成记录 ID，格式: rec_YYYYMMDD_HHMMSS"""
    import re
    m = re.search(r"(\d{8}-\d{6})", filename)
    if m:
        return f"rec_{m.group(1)}"
    return f"rec_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def generate_token() -> str:
    """生成每录音独立的访问 token（32字符 hex）"""
    return secrets.token_hex(16)


def get_audio_info(rec_id: str) -> dict | None:
    """获取录音的音频路径和 token（用于音频端点鉴权）"""
    conn = get_conn()
    row = conn.execute(
        "SELECT audio_path, audio_token FROM recordings WHERE id=?", (rec_id,)
    ).fetchone()
    if row is None:
        return None
    return {"audio_path": row["audio_path"] or "", "audio_token": row["audio_token"] or ""}


# ---- Recording CRUD ----

def upsert_recording(rec_id: str, **kwargs) -> dict | None:
    """创建或更新录音记录"""
    conn = get_conn()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    cur = conn.execute("SELECT id FROM recordings WHERE id=?", (rec_id,))
    exists = cur.fetchone() is not None

    if exists:
        sets = ", ".join(f"{k}=?" for k in kwargs.keys())
        values = list(kwargs.values()) + [rec_id]
        if sets:
            sets += ", updated_at=?"
            values.insert(-1, now) if len(values) > 1 else values.append(now)
            conn.execute(f"UPDATE recordings SET {sets} WHERE id=?", values)
    else:
        fields = ["id"] + list(kwargs.keys()) + ["created_at", "updated_at"]
        placeholders = ", ".join("?" * len(fields))
        values = [rec_id] + list(kwargs.values()) + [now, now]
        conn.execute(
            f"INSERT INTO recordings ({', '.join(fields)}) VALUES ({placeholders})",
            values,
        )

    conn.commit()
    return get_recording(rec_id)


def get_recording(rec_id: str) -> dict | None:
    conn = get_conn()
    row = conn.execute("SELECT * FROM recordings WHERE id=?", (rec_id,)).fetchone()
    if row is None:
        return None
    return _row_to_dict(row)


def list_recordings(
    status: str = None,
    user_id: str = None,
    category: str = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    conn = get_conn()
    conditions = []
    params = []

    if status:
        conditions.append("status=?")
        params.append(status)
    if user_id:
        conditions.append("user_id=?")
        params.append(user_id)
    if category:
        conditions.append("category=?")
        params.append(category)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.extend([limit, offset])

    rows = conn.execute(
        f"SELECT * FROM recordings {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        params,
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def count_recordings(
    status: str = None, user_id: str = None, category: str = None,
) -> int:
    conn = get_conn()
    conditions = []
    params = []
    if status:
        conditions.append("status=?")
        params.append(status)
    if user_id:
        conditions.append("user_id=?")
        params.append(user_id)
    if category:
        conditions.append("category=?")
        params.append(category)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    return int(conn.execute(
        f"SELECT COUNT(*) AS count FROM recordings {where}", params,
    ).fetchone()["count"])


def delete_recording(rec_id: str) -> bool:
    conn = get_conn()
    conn.execute("DELETE FROM analysis_log WHERE recording_id=?", (rec_id,))
    conn.execute("DELETE FROM recordings WHERE id=?", (rec_id,))
    conn.commit()
    return True


def update_tags(rec_id: str, tags: list[str], category: str = None) -> dict | None:
    conn = get_conn()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if category:
        conn.execute(
            "UPDATE recordings SET tags=?, category=?, updated_at=? WHERE id=?",
            (json.dumps(tags, ensure_ascii=False), category, now, rec_id),
        )
    else:
        conn.execute(
            "UPDATE recordings SET tags=?, updated_at=? WHERE id=?",
            (json.dumps(tags, ensure_ascii=False), now, rec_id),
        )
    conn.commit()
    return get_recording(rec_id)


# ---- Analysis Log ----

def log_analysis(
    recording_id: str,
    stage: str,
    status: str,
    *,
    user_id: str = "anonymous",
    input_size: int = 0,
    output_chars: int = 0,
    duration_ms: int = 0,
    error_msg: str = "",
) -> int:
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO analysis_log
           (recording_id, user_id, stage, status, input_size, output_chars, duration_ms, error_msg)
           VALUES (?,?,?,?,?,?,?,?)""",
        (recording_id, user_id, stage, status, input_size, output_chars, duration_ms, error_msg),
    )
    conn.commit()
    return cur.lastrowid


def get_analysis_log(recording_id: str) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM analysis_log WHERE recording_id=? ORDER BY id", (recording_id,)
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


# ---- Stats ----

def get_stats(user_id: str = None) -> dict:
    conn = get_conn()
    user_filter = "WHERE user_id=?" if user_id else ""
    params = (user_id,) if user_id else ()

    total = conn.execute(f"SELECT COUNT(*) as c FROM recordings {user_filter}", params).fetchone()["c"]
    completed = conn.execute(
        f"SELECT COUNT(*) as c FROM recordings {user_filter} {'AND' if user_id else 'WHERE'} status='completed'",
        params,
    ).fetchone()["c"]
    failed = conn.execute(
        f"SELECT COUNT(*) as c FROM recordings {user_filter} {'AND' if user_id else 'WHERE'} status='failed'",
        params,
    ).fetchone()["c"]
    total_chars = conn.execute(
        f"SELECT COALESCE(SUM(transcript_chars),0) as c FROM recordings {user_filter}", params
    ).fetchone()["c"]

    return {
        "total": total,
        "completed": completed,
        "failed": failed,
        "pending": total - completed - failed,
        "total_transcript_chars": total_chars,
    }


# ---- Helpers ----

def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    for field in (
        "tags", "summary_json", "insight_json",
        "recording_insight_json", "memory_insight_json",
    ):
        if field in d and isinstance(d[field], str):
            try:
                d[field] = json.loads(d[field])
            except (json.JSONDecodeError, TypeError):
                d[field] = [] if field == "tags" else {}
    return d


# ============================================================
# API Token Management
# ============================================================

import hashlib

def generate_token_id() -> str:
    """Short random ID for token records."""
    return secrets.token_hex(6)


def generate_api_token() -> tuple[str, str, str]:
    """Create a new API token. Returns (full_token, token_prefix, token_hash)."""
    full = "vsk_" + secrets.token_hex(32)
    prefix = full[:12]  # "vsk_xxxxxxxx"
    h = hashlib.sha256(full.encode()).hexdigest()
    return full, prefix, h


def create_api_token(user_id: str) -> dict:
    """Create and store a new API token. Returns {id, token, ...}."""
    tid = generate_token_id()
    full, prefix, hash_ = generate_api_token()
    conn = get_conn()
    conn.execute(
        """INSERT OR REPLACE INTO api_tokens (id, user_id, token_hash, token_prefix, created_at)
           VALUES (?, ?, ?, ?, datetime('now','localtime'))""",
        (tid, user_id.strip(), hash_, prefix),
    )
    conn.commit()
    return {"id": tid, "user_id": user_id.strip(), "token": full, "token_prefix": prefix}


def list_api_tokens() -> list[dict]:
    """List all tokens (never return full token)."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, user_id, token_prefix, created_at, last_used_at FROM api_tokens ORDER BY created_at DESC"
    ).fetchall()
    return [{k: row[k] for k in row.keys()} for row in rows]


def delete_api_token(tid: str) -> bool:
    """Delete an API token by id."""
    conn = get_conn()
    conn.execute("DELETE FROM api_tokens WHERE id = ?", (tid,))
    conn.commit()
    return True


def resolve_token(token: str) -> str | None:
    """Look up an API token and return the mapped user_id, or None."""
    h = hashlib.sha256(token.encode()).hexdigest()
    conn = get_conn()
    row = conn.execute(
        "SELECT user_id FROM api_tokens WHERE token_hash = ?", (h,)
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE api_tokens SET last_used_at = datetime('now','localtime') WHERE token_hash = ?", (h,)
        )
        conn.commit()
        return row["user_id"]
    return None


# ============================================================
# Categories Management
# ============================================================

def list_categories() -> list[dict]:
    """列出所有分类，按 sort_order 排序"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM categories ORDER BY sort_order, id"
    ).fetchall()
    return [dict(r) for r in rows]


def create_category(name: str, color: str = "#6366f1", sort_order: int = 0) -> dict:
    """新建分类"""
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO categories (name, color, sort_order) VALUES (?, ?, ?)",
        (name.strip(), color, sort_order),
    )
    conn.commit()
    return {"id": cur.lastrowid, "name": name.strip(), "color": color, "sort_order": sort_order}


def update_category(cat_id: int, name: str = None, color: str = None, sort_order: int = None) -> bool:
    """修改分类"""
    conn = get_conn()
    sets = []
    params = []
    if name is not None:
        sets.append("name=?")
        params.append(name.strip())
    if color is not None:
        sets.append("color=?")
        params.append(color)
    if sort_order is not None:
        sets.append("sort_order=?")
        params.append(sort_order)
    if not sets:
        return False
    params.append(cat_id)
    conn.execute(f"UPDATE categories SET {', '.join(sets)} WHERE id=?", params)
    conn.commit()
    return True


def delete_category(cat_id: int) -> bool:
    """删除分类，关联录音的 category 改为'未分类'"""
    conn = get_conn()
    # 先获取分类名
    row = conn.execute("SELECT name FROM categories WHERE id=?", (cat_id,)).fetchone()
    if not row:
        return False
    cat_name = row["name"]
    # 不允许删除'未分类'
    if cat_name == "未分类":
        return False
    # 关联录音改为'未分类'
    conn.execute("UPDATE recordings SET category='未分类' WHERE category=?", (cat_name,))
    conn.execute("DELETE FROM categories WHERE id=?", (cat_id,))
    conn.commit()
    return True
