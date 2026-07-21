import json
import sqlite3

import pytest

import db
from cognition import store
from services import recording_pipeline


def test_existing_job_table_migrates_before_new_indexes():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE cognition_jobs (
            id TEXT PRIMARY KEY, user_id TEXT, source_id TEXT, job_type TEXT,
            payload_json TEXT, status TEXT, attempts INTEGER DEFAULT 0,
            max_attempts INTEGER DEFAULT 3, lease_until TEXT, error_text TEXT,
            created_at TEXT, started_at TEXT, finished_at TEXT
        )"""
    )
    store.init_cognition_schema(conn)
    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(cognition_jobs)")
    }
    assert {"recording_id", "dedupe_key", "lease_owner", "run_after"} <= columns
    conn.close()


@pytest.fixture
def pipeline_db(monkeypatch, tmp_path):
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
            processing_version INTEGER DEFAULT 0, client_recording_id TEXT DEFAULT '', upload_path TEXT DEFAULT '',
            audio_sha256 TEXT DEFAULT ''
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
    monkeypatch.setattr(recording_pipeline, "get_conn", lambda: conn)
    monkeypatch.setattr(recording_pipeline, "_UPLOAD_DIR", tmp_path)
    yield conn
    conn.close()


def test_submit_is_durable_and_idempotent(pipeline_db):
    recording, job, duplicate = recording_pipeline.submit_recording(
        audio_bytes=b"OggS-test-audio",
        title="note20260718-210000.opus",
        user_id="admin",
    )
    _, repeated_job, repeated_duplicate = recording_pipeline.submit_recording(
        audio_bytes=b"OggS-test-audio",
        title="note20260718-210000.opus",
        user_id="admin",
    )

    assert not duplicate
    assert repeated_duplicate
    assert recording["core_status"] == "queued"
    assert job["id"] == repeated_job["id"]
    assert recording["audio_sha256"]
    claimed = store.claim_next_job()
    assert claimed["job_type"] == "transcribe_recording"
    assert claimed["lease_owner"]
    assert store.renew_job_lease(claimed["id"], claimed["lease_owner"])


def test_streamed_file_submit_moves_upload_and_queues_transcription(
    pipeline_db, tmp_path,
):
    staged_path = tmp_path / "incoming.part"
    staged_path.write_bytes(b"OggS-streamed-audio")

    recording, job, duplicate = recording_pipeline.submit_recording_file(
        staged_path=staged_path,
        file_size=staged_path.stat().st_size,
        title="note20260718-210500.opus",
        user_id="admin",
    )

    assert not duplicate
    assert job["job_type"] == "transcribe_recording"
    assert not staged_path.exists()
    assert recording["recording_insight_status"] == "disabled"
    assert recording["upload_path"].endswith(".opus")
    assert recording["audio_sha256"]


def test_pending_standard_asr_releases_the_worker_between_polls(pipeline_db, monkeypatch):
    recording, _, _ = recording_pipeline.submit_recording(
        audio_bytes=b"OggS-standard-async", title="note20260721-180000.opus", user_id="admin",
    )
    import services.asr_providers as providers

    provider = providers.DoubaoStandardProvider()
    monkeypatch.setattr(providers, "get_asr_provider", lambda _mode=None: provider)
    monkeypatch.setattr(provider, "submit_tasks", lambda *_args, **_kwargs: [{
        "task_id": "task-1", "logid": "log-1", "standard_url": "https://example.test/submit",
        "submitted_at": 1.0, "chunk_index": 0, "offset_ms": 0,
    }])
    monkeypatch.setattr(provider, "poll_task", lambda _task: None)

    submit_job = store.claim_next_job({"transcribe_recording"})
    assert recording_pipeline.process_recording_job(submit_job)
    poll_job = store.claim_next_job({"poll_standard_asr"})
    assert poll_job is not None

    assert recording_pipeline.process_recording_job(poll_job) is False
    waiting = store.get_job(poll_job["id"], "admin")
    assert waiting["status"] == "queued"
    assert waiting["attempts"] == 0
    assert json.loads(waiting["progress_json"])["stage"] == "waiting_asr"


@pytest.mark.parametrize("suffix", [".ogg", ".wav", ".mp3", ".m4a", ".aac", ".flac"])
def test_streamed_file_submit_preserves_supported_audio_suffix(
    pipeline_db, tmp_path, suffix,
):
    staged_path = tmp_path / "incoming.part"
    staged_path.write_bytes(b"container-audio")

    recording, job, duplicate = recording_pipeline.submit_recording_file(
        staged_path=staged_path,
        file_size=staged_path.stat().st_size,
        title=f"imported-training{suffix}",
        user_id="admin",
    )

    assert not duplicate
    assert job["job_type"] == "transcribe_recording"
    assert recording["upload_path"].endswith(suffix)
    assert not staged_path.exists()


def test_summary_completes_core_before_memory_ingest(pipeline_db, monkeypatch):
    title = "note20260718-211000.opus"
    recording_id = db.generate_id(title)
    db.upsert_recording(
        recording_id,
        user_id="admin",
        title=title,
        transcript="这是一段已经完成的转写。",
        status="summarizing",
        core_status="summarizing",
    )

    def fake_summarize(
        transcript, title, context, user_id, persist_recording=True,
    ):
        assert persist_recording is False
        result = {
            "current_intent": "验证异步状态机",
            "key_decisions": ["摘要完成后立即可用"],
            "action_items": [],
            "tags": [],
            "detailed_summary": "核心记录已经完成。",
        }
        db.upsert_recording(recording_id, summary_json=json.dumps(result, ensure_ascii=False))
        return result

    monkeypatch.setattr("services.asr.summarize", fake_summarize)
    job = store.enqueue_job(
        user_id="admin",
        recording_id=recording_id,
        job_type="summarize_recording",
        dedupe_key=f"recording:{recording_id}:summarize_recording:v1",
    )
    recording_pipeline.process_recording_job(job)

    completed = db.get_recording(recording_id)
    jobs = store.list_recording_jobs(recording_id, "admin")
    assert completed["core_status"] == "completed"
    assert completed["recording_insight_status"] == "disabled"
    assert completed["memory_insight_status"] == "queued"
    assert {item["job_type"] for item in jobs} == {
        "summarize_recording", "memory_ingest",
    }


def test_recording_insight_uses_admin_prompt_and_keeps_legacy_schema(
    pipeline_db, monkeypatch,
):
    captured = {}

    monkeypatch.setattr(
        db,
        "get_asr_config",
        lambda: {"insight_prompt": "洞察 {name} / {role} / {context}"},
    )

    def fake_call_llm(model, messages, max_time):
        captured["system"] = messages[0]["content"]
        return {
            "choices": [{
                "message": {
                    "content": """# 录音洞察
## 核心洞察
异步状态需要可见
## 机会分析
体验更稳定
## 风险提示
失败不可见
## 行动建议
- 增加重试入口""",
                },
            }],
            "usage": {},
        }

    monkeypatch.setattr("services.asr.call_llm", fake_call_llm)
    result = recording_pipeline.generate_recording_insight(
        transcript="测试转写",
        title="测试录音",
        context="测试上下文",
        user_id="admin",
    )

    assert captured["system"].startswith("洞察 ")
    assert set(result) == {"markdown", "core_insight", "analysis", "action_suggestions"}
    assert result["core_insight"] == "异步状态需要可见"


def test_backfill_renders_existing_json_as_markdown(pipeline_db):
    db.upsert_recording(
        "rec_markdown_backfill",
        user_id="admin",
        summary_json=json.dumps({
            "current_intent": "整理项目进度",
            "key_decisions": ["周五发布"],
            "detailed_summary": "项目已进入发布阶段。",
        }, ensure_ascii=False),
        recording_insight_json=json.dumps({
            "core_insight": "发布时间已经明确",
            "analysis": {"opportunity": "按期交付", "risk": "测试时间不足"},
            "action_suggestions": ["补充回归测试"],
        }, ensure_ascii=False),
    )

    counts = recording_pipeline.backfill_markdown_fields()
    recording = db.get_recording("rec_markdown_backfill")

    assert counts["summary"] == 1
    assert counts["recording_insight"] == 1
    assert "## 核心目的" in recording["summary_markdown"]
    assert "## 核心洞察" in recording["recording_insight_markdown"]


def test_backfill_queues_only_memory_ingest_for_legacy_recording(pipeline_db):
    recording_id = "rec_legacy_insight"
    db.upsert_recording(
        recording_id,
        user_id="admin",
        title="legacy.opus",
        transcript="已有转写",
        summary_json=json.dumps({"current_intent": "旧摘要"}, ensure_ascii=False),
        insight_json=json.dumps({"current_intent": "错误摘要结构"}, ensure_ascii=False),
        recording_insight_json=json.dumps(
            {"current_intent": "错误摘要结构"}, ensure_ascii=False,
        ),
        status="completed",
        core_status="completed",
        recording_insight_status="completed",
        memory_insight_status="pending",
    )

    counts = recording_pipeline.backfill_completed_recording_jobs()
    jobs = store.list_recording_jobs(recording_id, "admin")

    assert counts == {"memory_ingest": 1}
    assert {job["job_type"] for job in jobs} == {"memory_ingest"}
    assert db.get_recording(recording_id)["recording_insight_status"] == "disabled"


def test_backfill_accepts_markdown_only_minutes(pipeline_db):
    recording_id = "rec_markdown_only"
    db.upsert_recording(
        recording_id,
        user_id="admin",
        title="markdown-only.opus",
        transcript="已有转写",
        summary_markdown="# 录音纪要\n\n## 主要内容\n\n完整纪要。",
        status="completed",
        core_status="completed",
    )

    counts = recording_pipeline.backfill_completed_recording_jobs()

    assert counts == {"memory_ingest": 1}
    jobs = store.list_recording_jobs(recording_id, "admin")
    assert {job["job_type"] for job in jobs} == {"memory_ingest"}


def test_refresh_batch_resumes_completed_minutes_at_memory_ingest(pipeline_db):
    recording_id = "rec_refresh_resume"
    db.upsert_recording(
        recording_id,
        user_id="admin",
        title="refresh.opus",
        transcript="已有转写",
        summary_markdown="# 旧纪要",
        status="completed",
        core_status="completed",
        memory_insight_status="queued",
    )
    refresh_job = store.enqueue_job(
        user_id="admin",
        recording_id=recording_id,
        job_type="summarize_recording",
        dedupe_key=f"recording:{recording_id}:summarize:refresh:batch-1",
    )
    store.complete_job(refresh_job["id"])
    memory_job = store.enqueue_job(
        user_id="admin",
        recording_id=recording_id,
        job_type="memory_ingest",
        dedupe_key=f"recording:{recording_id}:memory_ingest:v1",
    )
    store.complete_job(memory_job["id"])

    result = recording_pipeline.refresh_completed_recording_content(
        user_id="admin", refresh_id="batch-1",
    )

    resumed = store.get_job(memory_job["id"], "admin")
    assert result["queued"] == 0
    assert result["skipped"] == 1
    assert result["memory_requeued"] == 1
    assert resumed["status"] == "queued"


def test_refresh_batch_requeues_ingested_but_stale_cognition_source(pipeline_db):
    recording_id = "rec_refresh_stale_source"
    db.upsert_recording(
        recording_id,
        user_id="admin",
        title="stale.opus",
        transcript="已有转写",
        summary_markdown="# 新纪要",
        status="completed",
        core_status="completed",
        memory_insight_status="ingested",
    )
    source, source_job, _ = store.create_source(
        user_id="admin",
        source_type="recording",
        content="# 旧纪要",
        origin="vibry_card",
        external_id=f"recording_summary:{recording_id}",
    )
    pipeline_db.execute("UPDATE sources SET status='processed' WHERE id=?", (source["id"],))
    pipeline_db.commit()
    store.complete_job(source_job["id"])
    refresh_job = store.enqueue_job(
        user_id="admin",
        recording_id=recording_id,
        job_type="summarize_recording",
        dedupe_key=f"recording:{recording_id}:summarize:refresh:batch-2",
    )
    store.complete_job(refresh_job["id"])
    memory_job = store.enqueue_job(
        user_id="admin",
        recording_id=recording_id,
        job_type="memory_ingest",
        dedupe_key=f"recording:{recording_id}:memory_ingest:v1",
    )
    store.complete_job(memory_job["id"])

    result = recording_pipeline.refresh_completed_recording_content(
        user_id="admin", refresh_id="batch-2",
    )

    assert result["memory_requeued"] == 1
    assert store.get_job(memory_job["id"], "admin")["status"] == "queued"
    assert db.get_recording(recording_id)["memory_insight_status"] == "queued"


def test_optional_suggestion_failures_do_not_fail_completed_recording(
    pipeline_db, monkeypatch,
):
    from services import recording_grouping, task_suggestions

    recording_id = "rec_suggestion_isolation"
    db.upsert_recording(
        recording_id,
        user_id="admin",
        title="note20260718-220000.opus",
        filename="note20260718-220000.opus",
        transcript="有效转写",
        summary_markdown="# 录音纪要\n\n## 行动项\n- 完成测试",
        status="completed",
        core_status="completed",
        memory_insight_status="ingesting",
    )
    source, _, _ = store.create_source(
        user_id="admin",
        source_type="recording",
        content="# 录音纪要\n\n## 行动项\n- 完成测试",
        origin="test",
        external_id=f"recording_summary:{recording_id}",
        metadata={"recording_id": recording_id, "kind": "minutes"},
    )
    monkeypatch.setattr(
        task_suggestions,
        "publish_action_item_suggestions",
        lambda _: (_ for _ in ()).throw(RuntimeError("task card unavailable")),
    )
    monkeypatch.setattr(
        recording_grouping,
        "discover_recording_groups",
        lambda _: (_ for _ in ()).throw(RuntimeError("grouping unavailable")),
    )

    recording_pipeline.on_source_processed(source["id"])

    assert db.get_recording(recording_id)["memory_insight_status"] == "ingested"
