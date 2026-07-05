"""Vibry AI Core — SQLite 数据库

录音、转写、纪要持久化。
线程安全（WAL 模式 + 每线程独立连接）。
从 VibryCard Flask Server 移植，新增 user_id 隔离支持。
"""

import secrets
import sqlite3, json, os, threading
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vibrycard.db")

# 线程安全：每个线程使用独立连接
_local = threading.local()


def get_conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(DB_PATH)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA foreign_keys=ON")
    return _local.conn


def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS recordings (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL DEFAULT 'anonymous',
            title TEXT NOT NULL DEFAULT '',
            filename TEXT NOT NULL DEFAULT '',
            file_size INTEGER DEFAULT 0,
            duration_sec REAL DEFAULT 0,
            transcript TEXT DEFAULT '',
            transcript_chars INTEGER DEFAULT 0,
            summary_json TEXT DEFAULT '',
            tags TEXT DEFAULT '[]',
            category TEXT DEFAULT '未分类',
            status TEXT DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS analysis_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recording_id TEXT NOT NULL,
            user_id TEXT NOT NULL DEFAULT 'anonymous',
            stage TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'start',
            input_size INTEGER DEFAULT 0,
            output_chars INTEGER DEFAULT 0,
            duration_ms INTEGER DEFAULT 0,
            error_msg TEXT DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (recording_id) REFERENCES recordings(id)
        );

        CREATE INDEX IF NOT EXISTS idx_recordings_created ON recordings(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_recordings_status ON recordings(status);
        CREATE INDEX IF NOT EXISTS idx_recordings_user ON recordings(user_id);
        CREATE INDEX IF NOT EXISTS idx_log_recording ON analysis_log(recording_id);

        CREATE TABLE IF NOT EXISTS usage_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL DEFAULT 'anonymous',
            endpoint TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0,
            duration_ms INTEGER DEFAULT 0,
            cost_rmb REAL DEFAULT 0.0,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );

        CREATE INDEX IF NOT EXISTS idx_usage_user ON usage_log(user_id);
        CREATE INDEX IF NOT EXISTS idx_usage_created ON usage_log(created_at DESC);

        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL DEFAULT 'anonymous',
            conversation_id TEXT NOT NULL DEFAULT 'default',
            role TEXT NOT NULL DEFAULT 'user',
            content TEXT NOT NULL DEFAULT '',
            model TEXT DEFAULT '',
            tokens INTEGER DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );

        CREATE INDEX IF NOT EXISTS idx_chat_user ON chat_messages(user_id);
        CREATE INDEX IF NOT EXISTS idx_chat_conv ON chat_messages(conversation_id);
        CREATE INDEX IF NOT EXISTS idx_chat_created ON chat_messages(created_at DESC);

        CREATE TABLE IF NOT EXISTS personality (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            system_prompt TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS admin_users (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            email TEXT NOT NULL DEFAULT '',
            password_hash TEXT NOT NULL DEFAULT '',
            verification_code TEXT DEFAULT '',
            code_expiry TEXT DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
    """)

    # ---- 兼容性迁移：为旧表添加新列（每录音独立token + 音频路径）----
    cur = conn.execute("PRAGMA table_info(recordings)")
    existing_cols = {row[1] for row in cur.fetchall()}
    for col, decl in [("audio_token", "TEXT DEFAULT ''"), ("audio_path", "TEXT DEFAULT ''")]:
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE recordings ADD COLUMN {col} {decl}")
            print(f"  [migrate] recordings + {col}")

    conn.commit()


# ---- Usage / Billing ----

# 模型定价 (RMB per 1M tokens)
MODEL_PRICES = {
    "doubao-seed-2-1-turbo-260628": {"prompt": 2.0, "completion": 6.0},
    "doubao-seed-2-1-pro-260628": {"prompt": 4.0, "completion": 12.0},
    "doubao-seed-2-0-mini-260428": {"prompt": 0.5, "completion": 1.5},
    "doubao-embedding-vision-251215": {"prompt": 0.5, "completion": 0.0},
    "default": {"prompt": 2.0, "completion": 6.0},
}


def log_usage(
    user_id: str,
    endpoint: str,
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    duration_ms: int = 0,
) -> int:
    """记录 API 调用用量"""
    prices = MODEL_PRICES.get(model, MODEL_PRICES["default"])
    cost = (prompt_tokens * prices["prompt"] + completion_tokens * prices["completion"]) / 1_000_000

    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO usage_log (user_id, endpoint, model, prompt_tokens, completion_tokens, total_tokens, duration_ms, cost_rmb)
           VALUES (?,?,?,?,?,?,?,?)""",
        (user_id, endpoint, model, prompt_tokens, completion_tokens, total_tokens, duration_ms, round(cost, 6)),
    )
    conn.commit()
    return cur.lastrowid


def get_usage_summary(user_id: str = None) -> dict:
    """获取用量摘要"""
    conn = get_conn()
    if user_id:
        row = conn.execute(
            "SELECT COALESCE(SUM(total_tokens),0) as tokens, COALESCE(SUM(cost_rmb),0) as cost, COUNT(*) as calls FROM usage_log WHERE user_id=?",
            (user_id,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COALESCE(SUM(total_tokens),0) as tokens, COALESCE(SUM(cost_rmb),0) as cost, COUNT(*) as calls FROM usage_log"
        ).fetchone()
    return {"total_tokens": row["tokens"], "total_cost_rmb": round(row["cost"], 4), "total_calls": row["calls"]}


def get_usage_by_user() -> list[dict]:
    """按用户分组的用量"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT user_id, SUM(total_tokens) as tokens, SUM(cost_rmb) as cost, COUNT(*) as calls FROM usage_log GROUP BY user_id ORDER BY cost DESC"
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
    conversation_id: str = "default",
    limit: int = 50,
) -> list[dict]:
    """获取聊天历史"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM chat_messages WHERE user_id=? AND conversation_id=? ORDER BY created_at DESC LIMIT ?",
        (user_id, conversation_id, limit),
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


def get_usage_recent(limit: int = 50) -> list[dict]:
    """最近用量记录"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM usage_log ORDER BY created_at DESC LIMIT ?", (limit,)
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

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.extend([limit, offset])

    rows = conn.execute(
        f"SELECT * FROM recordings {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        params,
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


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
    for field in ("tags", "summary_json"):
        if field in d and isinstance(d[field], str):
            try:
                d[field] = json.loads(d[field])
            except (json.JSONDecodeError, TypeError):
                d[field] = [] if field == "tags" else {}
    return d
