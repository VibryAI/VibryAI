"""Vibry AI Core — SQLite 数据库

录音、转写、纪要持久化。
线程安全（WAL 模式 + 每线程独立连接）。
从 VibryCard Flask Server 移植，新增 user_id 隔离支持。
"""

import secrets
import sqlite3, json, os, threading
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA_DIR = os.path.join(_ROOT, "data")
DB_PATH = os.path.join(_DATA_DIR, "vibrycard.db")

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
    os.makedirs(_DATA_DIR, mode=0o755, exist_ok=True)
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

        CREATE TABLE IF NOT EXISTS asr_config (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            app_id TEXT NOT NULL DEFAULT '',
            access_key TEXT NOT NULL DEFAULT '',
            asr_mode TEXT NOT NULL DEFAULT 'local',
            flash_url TEXT NOT NULL DEFAULT '',
            standard_url TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS model_config (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            chat_model TEXT NOT NULL DEFAULT '',
            chat_base_url TEXT NOT NULL DEFAULT '',
            chat_api_key TEXT NOT NULL DEFAULT '',
            embedding_model TEXT NOT NULL DEFAULT '',
            embedding_base_url TEXT NOT NULL DEFAULT '',
            embedding_api_key TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS api_tokens (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            token_prefix TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            last_used_at TEXT
        );

        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            color TEXT DEFAULT '#6366f1',
            sort_order INTEGER DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
    """)

    # ---- 初始化默认分类 ----
    cur = conn.execute("SELECT COUNT(*) as c FROM categories")
    if cur.fetchone()["c"] == 0:
        for i, name in enumerate(["未分类", "会议", "通话", "备忘"]):
            conn.execute(
                "INSERT INTO categories (name, color, sort_order) VALUES (?, ?, ?)",
                (name, ["#6b7280", "#6366f1", "#10b981", "#f59e0b"][i], i),
            )
        print("  [init] 插入默认分类")

    # ---- 兼容性迁移：为旧表添加新列 ----
    cur = conn.execute("PRAGMA table_info(recordings)")
    existing_cols = {row[1] for row in cur.fetchall()}
    for col, decl in [("audio_token", "TEXT DEFAULT ''"), ("audio_path", "TEXT DEFAULT ''")]:
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE recordings ADD COLUMN {col} {decl}")
            print(f"  [migrate] recordings + {col}")

    # Migrate ASR configuration fields introduced by the current control plane.
    cur = conn.execute("PRAGMA table_info(asr_config)")
    asr_cols = {row[1] for row in cur.fetchall()}
    for col, decl in [
        ("voice_mode", "TEXT NOT NULL DEFAULT 'cloud'"),
        ("summary_prompt", "TEXT DEFAULT ''"),
        ("insight_prompt", "TEXT DEFAULT ''"),
    ]:
        if col not in asr_cols:
            conn.execute(f"ALTER TABLE asr_config ADD COLUMN {col} {decl}")
            print(f"  [migrate] asr_config + {col}")

    # 迁移 recordings: 添加 VibryCard 独有列
    cur = conn.execute("PRAGMA table_info(recordings)")
    rec_cols = {row[1] for row in cur.fetchall()}
    for col, decl in [("insight_json", "TEXT DEFAULT ''"), ("utterances_json", "TEXT DEFAULT ''"), ("raw_wav_path", "TEXT DEFAULT ''")]:
        if col not in rec_cols:
            conn.execute(f"ALTER TABLE recordings ADD COLUMN {col} {decl}")
            print(f"  [migrate] recordings + {col}")

    # 迁移 usage_log: 添加 audio_seconds 列 (ASR 时长计费)
    cur = conn.execute("PRAGMA table_info(usage_log)")
    usage_cols = {row[1] for row in cur.fetchall()}
    if "audio_seconds" not in usage_cols:
        conn.execute("ALTER TABLE usage_log ADD COLUMN audio_seconds REAL DEFAULT 0")
        print("  [migrate] usage_log + audio_seconds")

    conn.commit()

    # Cognitive Core v2 uses the same primary database so evidence, projects
    # and legacy recordings can be joined without cross-database drift.
    from cognition.store import init_cognition_schema
    init_cognition_schema(conn)


