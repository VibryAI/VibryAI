import sqlite3

import pytest

import db
from cognition import store
from services import aggregate_pipeline


@pytest.fixture
def aggregate_db(monkeypatch):
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


def _recording(recording_id: str, title: str, transcript: str, user_id: str = "u1"):
    db.upsert_recording(
        recording_id,
        user_id=user_id,
        title=title,
        filename=f"{title}.opus",
        transcript=transcript,
        status="completed",
        core_status="completed",
    )


def test_submit_aggregate_is_durable_and_idempotent(aggregate_db):
    _recording("rec-1", "第一节", "第一节培训内容")
    _recording("rec-2", "第二节", "第二节培训内容")

    aggregate, job, duplicate = aggregate_pipeline.submit_aggregate(
        user_id="u1",
        recording_ids=["rec-1", "rec-2"],
        title="两节培训专题总结",
    )
    repeated, repeated_job, repeated_duplicate = aggregate_pipeline.submit_aggregate(
        user_id="u1",
        recording_ids=["rec-1", "rec-2"],
        title="两节培训专题总结",
    )

    assert not duplicate
    assert repeated_duplicate
    assert aggregate["id"] == repeated["id"]
    assert aggregate["status"] == "queued"
    assert aggregate["recording_ids"] == ["rec-1", "rec-2"]
    assert job["id"] == repeated_job["id"]
    assert job["job_type"] == "aggregate_minutes"


def test_aggregate_requires_two_completed_owned_recordings(aggregate_db):
    _recording("rec-1", "第一节", "第一节培训内容")
    _recording("rec-other", "其他用户", "不可访问", user_id="u2")

    with pytest.raises(ValueError, match="at least two"):
        aggregate_pipeline.submit_aggregate(user_id="u1", recording_ids=["rec-1"])

    with pytest.raises(ValueError, match="not found"):
        aggregate_pipeline.submit_aggregate(
            user_id="u1", recording_ids=["rec-1", "rec-other"]
        )


def test_aggregate_worker_persists_markdown_source_and_message(
    aggregate_db, monkeypatch,
):
    _recording("rec-1", "第一节", "第一节培训内容")
    _recording("rec-2", "第二节", "第二节培训内容")
    calls = []

    def fake_summarize(transcript, title, context, user_id, persist_recording=True):
        calls.append({"transcript": transcript, "title": title, "context": context})
        return {
            "markdown": "# 专题总结\n\n## 核心目的\n整合两节培训。",
            "current_intent": "整合两节培训",
            "key_decisions": [],
            "action_items": [],
            "tags": ["培训"],
            "detailed_summary": "# 专题总结\n\n## 核心目的\n整合两节培训。",
        }

    monkeypatch.setattr("services.asr.summarize", fake_summarize)
    aggregate, job, _ = aggregate_pipeline.submit_aggregate(
        user_id="u1",
        recording_ids=["rec-1", "rec-2"],
        project_ids=[],
        title="两节培训专题总结",
    )

    aggregate_pipeline.process_aggregate_job(job)

    completed = store.get_recording_aggregate(aggregate["id"], "u1")
    source = store.get_source(completed["source_id"], "u1")
    matrix = store.memory_matrix("u1")

    assert completed["status"] == "completed"
    assert completed["summary_markdown"].startswith("# 专题总结")
    assert source["source_type"] == "topic_summary"
    assert source["derivation_type"] == "summary"
    assert source["metadata"]["recording_ids"] == ["rec-1", "rec-2"]
    assert matrix["messages"][-1]["message_type"] == "topic_summary"
    assert matrix["messages"][-1]["metadata"]["aggregate_id"] == aggregate["id"]
    assert len(calls) == 1
    assert "第一节培训内容" in calls[0]["transcript"]
    assert "第二节培训内容" in calls[0]["transcript"]


def test_long_aggregate_summarizes_chunks_before_final_merge(
    aggregate_db, monkeypatch,
):
    _recording("rec-1", "第一节", "甲" * 80)
    _recording("rec-2", "第二节", "乙" * 80)
    calls = []

    def fake_summarize(transcript, title, context, user_id, persist_recording=True):
        calls.append(title)
        return {
            "markdown": f"# 录音纪要\n\n## 核心目的\n{title}",
            "detailed_summary": f"# 录音纪要\n\n## 核心目的\n{title}",
        }

    monkeypatch.setattr("services.asr.summarize", fake_summarize)
    monkeypatch.setattr(aggregate_pipeline, "_CHUNK_CHAR_LIMIT", 60)
    aggregate, job, _ = aggregate_pipeline.submit_aggregate(
        user_id="u1", recording_ids=["rec-1", "rec-2"], title="长培训总结",
    )

    aggregate_pipeline.process_aggregate_job(job)

    assert store.get_recording_aggregate(aggregate["id"], "u1")["status"] == "completed"
    assert len(calls) == 3
    assert "第 1/2 部分" in calls[0]
    assert calls[-1] == "长培训总结"


def test_final_aggregate_failure_is_visible(aggregate_db):
    _recording("rec-1", "第一节", "第一节培训内容")
    _recording("rec-2", "第二节", "第二节培训内容")
    aggregate, job, _ = aggregate_pipeline.submit_aggregate(
        user_id="u1", recording_ids=["rec-1", "rec-2"],
    )

    aggregate_pipeline.on_aggregate_job_failed(job, "model unavailable", final=True)

    failed = store.get_recording_aggregate(aggregate["id"], "u1")
    assert failed["status"] == "failed"
    assert failed["error_text"] == "model unavailable"


def test_aggregate_api_routes_are_registered():
    from routers.cognition import router

    paths = {route.path for route in router.routes if hasattr(route, "path")}
    assert "/api/v2/aggregates" in paths
    assert "/api/v2/aggregates/{aggregate_id}" in paths
