import sqlite3

import pytest

import db
from cognition import store
from services import recording_grouping
from services import task_suggestions


@pytest.fixture
def grouping_db(monkeypatch):
    import db.models as db_models

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE recordings (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL DEFAULT 'anonymous',
            title TEXT DEFAULT '', filename TEXT DEFAULT '', file_size INTEGER DEFAULT 0,
            duration_sec REAL DEFAULT 0, transcript TEXT DEFAULT '', transcript_chars INTEGER DEFAULT 0,
            summary_json TEXT DEFAULT '', summary_markdown TEXT DEFAULT '',
            tags TEXT DEFAULT '[]', category TEXT DEFAULT '未分类',
            status TEXT DEFAULT 'pending', created_at TEXT DEFAULT '', updated_at TEXT DEFAULT '',
            audio_token TEXT DEFAULT '', audio_path TEXT DEFAULT '', insight_json TEXT DEFAULT '',
            utterances_json TEXT DEFAULT '', raw_wav_path TEXT DEFAULT '',
            core_status TEXT DEFAULT 'pending', recording_insight_status TEXT DEFAULT 'pending',
            memory_insight_status TEXT DEFAULT 'pending', recording_insight_json TEXT DEFAULT '',
            memory_insight_json TEXT DEFAULT '', recording_insight_markdown TEXT DEFAULT '',
            memory_insight_markdown TEXT DEFAULT '', processing_error TEXT DEFAULT '',
            processing_version INTEGER DEFAULT 0, client_recording_id TEXT DEFAULT '', upload_path TEXT DEFAULT ''
        );
        CREATE TABLE analysis_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, recording_id TEXT, user_id TEXT,
            stage TEXT, status TEXT, input_size INTEGER DEFAULT 0, output_chars INTEGER DEFAULT 0,
            duration_ms INTEGER DEFAULT 0, error_msg TEXT DEFAULT '', created_at TEXT DEFAULT ''
        );
        CREATE TABLE usage_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, endpoint TEXT, model TEXT,
            prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0, duration_ms INTEGER DEFAULT 0,
            cost_rmb REAL DEFAULT 0, audio_seconds REAL DEFAULT 0, created_at TEXT DEFAULT ''
        );
        """
    )
    store.init_cognition_schema(conn)
    monkeypatch.setattr(store, "get_conn", lambda: conn)
    monkeypatch.setattr(db_models, "get_conn", lambda: conn)
    yield conn
    conn.close()


def _recording(
    recording_id: str, filename: str, *, user_id: str = "u1",
    duration_sec: float = 3600, category: str = "未分类",
):
    db.upsert_recording(
        recording_id,
        user_id=user_id,
        title=filename.rsplit(".", 1)[0],
        filename=filename,
        duration_sec=duration_sec,
        transcript=f"{recording_id} 的有效转写内容",
        tags='["培训"]' if category != "未分类" else "[]",
        category=category,
        status="completed",
        core_status="completed",
    )


def test_continuous_hour_splits_create_one_deduplicated_chat_card(grouping_db):
    _recording("rec-1", "note20260718-100000.opus")
    _recording("rec-2", "note20260718-110100.opus")
    _recording("rec-3", "note20260718-120200.opus")

    created = recording_grouping.discover_recording_groups("u1")
    repeated = recording_grouping.discover_recording_groups("u1")

    assert len(created) == 1
    assert repeated == []
    suggestion = created[0]
    assert suggestion["suggestion_type"] == "continuous"
    assert suggestion["recording_ids"] == ["rec-1", "rec-2", "rec-3"]

    thread = store.ensure_thread(
        user_id="u1", scope_type="global", scope_id="", title="Memory Matrix",
    )
    messages = store.list_messages(thread_id=thread["id"], user_id="u1")
    assert len(messages) == 1
    assert messages[0]["message_type"] == "recording_group_suggestion"
    assert messages[0]["metadata"]["status"] == "pending"


def test_same_day_topic_acceptance_reuses_async_aggregate_pipeline(grouping_db):
    _recording(
        "rec-a", "interview20260719-090000.opus", user_id="u2",
        duration_sec=900, category="产品培训",
    )
    _recording(
        "rec-b", "interview20260719-150000.opus", user_id="u2",
        duration_sec=900, category="产品培训",
    )

    created = recording_grouping.discover_recording_groups("u2")
    assert len(created) == 1
    assert created[0]["suggestion_type"] == "same_topic"

    suggestion, aggregate = recording_grouping.respond_to_group_suggestion(
        user_id="u2", suggestion_id=created[0]["id"], action="accept",
    )
    repeated, repeated_aggregate = recording_grouping.respond_to_group_suggestion(
        user_id="u2", suggestion_id=created[0]["id"], action="accept",
    )

    assert suggestion["status"] == "accepted"
    assert aggregate["status"] == "queued"
    assert aggregate["recording_ids"] == ["rec-a", "rec-b"]
    assert repeated["aggregate_id"] == aggregate["id"]
    assert repeated_aggregate["id"] == aggregate["id"]

    thread = store.ensure_thread(
        user_id="u2", scope_type="global", scope_id="", title="Memory Matrix",
    )
    message = store.list_messages(thread_id=thread["id"], user_id="u2")[0]
    assert message["metadata"]["status"] == "accepted"
    assert message["metadata"]["aggregate_id"] == aggregate["id"]


def test_group_suggestion_response_route_is_registered():
    from routers.cognition import router

    paths = {route.path for route in router.routes if hasattr(route, "path")}
    assert (
        "/api/v2/recording-group-suggestions/{suggestion_id}/respond" in paths
    )


def test_explicit_action_items_become_confirmable_tasks(grouping_db):
    source, _, _ = store.create_source(
        user_id="u3",
        source_type="recording",
        origin="test",
        external_id="minutes:rec-action",
        title="产品复盘",
        content=(
            "# 录音纪要\n\n## 核心目的\n整理发布计划。\n\n"
            "## 行动项\n- 周五前完成回归测试\n- 整理客户反馈"
        ),
        mime_type="text/markdown",
        metadata={"kind": "minutes"},
    )

    created = task_suggestions.publish_action_item_suggestions(source)
    repeated = task_suggestions.publish_action_item_suggestions(source)

    assert len(created) == 2
    assert repeated == []
    suggestion, task = task_suggestions.respond_to_task_suggestion(
        user_id="u3", suggestion_id=created[0]["id"], action="accept",
    )
    assert suggestion["status"] == "accepted"
    assert task["title"] == "周五前完成回归测试"
    assert task["status"] == "pending"

    thread = store.ensure_thread(
        user_id="u3", scope_type="global", scope_id="", title="Memory Matrix",
    )
    messages = store.list_messages(thread_id=thread["id"], user_id="u3")
    accepted = next(
        item for item in messages
        if item["metadata"].get("suggestion_id") == suggestion["id"]
    )
    assert accepted["metadata"]["status"] == "accepted"


def test_task_suggestion_response_route_is_registered():
    from routers.cognition import router

    paths = {route.path for route in router.routes if hasattr(route, "path")}
    assert "/api/v2/task-suggestions/{suggestion_id}/respond" in paths
