import json
import sqlite3

import pytest

from cognition import store
from cognition import migration
from cognition.context import compile_context
from cognition.insights import generate_project_insights
from cognition.pipeline import process_source
from cognition.scheduler import CognitiveScheduler


@pytest.fixture
def cognition_db(monkeypatch):
    from cognition import semantic

    monkeypatch.setenv("COGNITION_SEMANTIC_MODE", "local")
    monkeypatch.setattr(
        semantic, "_fastembed_vectors",
        lambda texts: ("fastembed:test", [semantic.vectorize(text) for text in texts]),
    )
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    store.init_cognition_schema(conn)
    monkeypatch.setattr(store, "get_conn", lambda: conn)
    yield conn
    conn.close()


def test_source_is_idempotent_and_creates_durable_job(cognition_db):
    source, job, duplicate = store.create_source(
        user_id="u1", source_type="chat", content="讨论 VibryAI 重构",
        origin="codex", external_id="thread-1:turn-1",
    )
    again, again_job, again_duplicate = store.create_source(
        user_id="u1", source_type="chat", content="讨论 VibryAI 重构",
        origin="codex", external_id="thread-1:turn-1",
    )

    assert not duplicate
    assert again_duplicate
    assert source["id"] == again["id"]
    assert job["id"] == again_job["id"]
    assert job["status"] == "queued"


def test_refresh_source_replaces_stale_evidence_and_preserves_projects(cognition_db):
    project = store.create_project(user_id="u1", name="培训项目")
    source, _, _ = store.create_source(
        user_id="u1",
        source_type="recording",
        content="旧纪要：第一版培训计划。",
        external_id="recording_summary:rec-1",
        project_ids=[project["id"]],
    )
    process_source(source["id"])
    old_claim_ids = {
        item["id"] for item in store.list_claims_for_source(source["id"])
    }

    refreshed, job, changed = store.refresh_source_content(
        source_id=source["id"],
        user_id="u1",
        content="新纪要：培训改为两天，并增加销售演练。",
        metadata={"kind": "minutes"},
    )

    assert changed
    assert refreshed["id"] == source["id"]
    assert refreshed["status"] == "queued"
    assert refreshed["metadata"]["project_ids"] == [project["id"]]
    assert job["job_type"] == "process_source"
    assert store.list_claims_for_source(source["id"]) == []
    assert not old_claim_ids.intersection(
        row["id"] for row in cognition_db.execute("SELECT id FROM claims_v2")
    )


def test_processing_creates_claim_evidence_and_project_membership(cognition_db):
    project = store.create_project(
        user_id="u1", name="VibryAI", tags=["记忆系统"], goal="重构认知内核"
    )
    source, job, _ = store.create_source(
        user_id="u1", source_type="recording",
        content='{"key_decisions":["VibryAI 采用统一 Source 层"],"tags":["记忆系统"]}',
        origin="vibry_card", project_ids=[project["id"]],
    )

    claimed = store.claim_next_job()
    assert claimed["id"] == job["id"]
    result = process_source(source["id"])
    store.complete_job(job["id"])

    claims = store.list_claims_for_source(source["id"])
    memberships = store.list_memberships(project["id"], "source")
    brief = store.project_brief(project["id"], "u1")

    assert result["claim_count"] >= 2
    assert claims[0]["evidence"][0]["source_id"] == source["id"]
    assert memberships[0]["status"] == "confirmed"
    assert brief["claims"]


def test_claim_write_merges_exact_duplicates_and_normalizes_entities(cognition_db):
    first, _, _ = store.create_source(user_id="u1", source_type="manual", content="first evidence")
    second, _, _ = store.create_source(user_id="u1", source_type="manual", content="second evidence")
    initial = store.create_claim(
        user_id="u1", source_id=first["id"], content="Alice owns the project review.",
        network="experience", entities=["Alice"], confidence=0.9,
    )
    merged = store.create_claim(
        user_id="u1", source_id=second["id"], content="Alice owns the project review.",
        network="experience", entities=["Alice"], confidence=0.9,
    )

    assert initial["id"] == merged["id"]
    assert merged["entities"] == ["Alice"]
    assert {item["source_id"] for item in merged["evidence"]} == {first["id"], second["id"]}


def test_semantic_lane_persists_vectors_and_suggests_fuzzy_claim_relations(cognition_db):
    first, _, _ = store.create_source(
        user_id="u1", source_type="manual", content="Pricing validation before the launch is required."
    )
    second, _, _ = store.create_source(
        user_id="u1", source_type="manual", content="Pricing validation before the launch remains required."
    )

    process_source(first["id"])
    process_source(second["id"])

    vector_count = cognition_db.execute(
        "SELECT COUNT(*) FROM semantic_vectors WHERE object_type='claim'"
    ).fetchone()[0]
    relation_count = cognition_db.execute(
        "SELECT COUNT(*) FROM claim_relations WHERE relation_type='similar' AND status='suggested'"
    ).fetchone()[0]

    assert vector_count == 2
    assert relation_count == 1


def test_semantic_persistence_batches_provider_work(cognition_db, monkeypatch):
    from cognition import semantic

    provider_calls: list[list[str]] = []

    def fake_remote_vectors(texts):
        provider_calls.append(texts)
        return "remote:test", [semantic.vectorize(text) for text in texts]

    monkeypatch.setattr(semantic, "_remote_enabled", lambda: True)
    monkeypatch.setattr(semantic, "_remote_vectors", fake_remote_vectors)
    semantic.persist_many([
        ("u1", "claim", "one", "first claim"),
        ("u1", "claim", "two", "second claim"),
    ])

    assert provider_calls == [["first claim", "second claim"]]
    assert cognition_db.execute("SELECT COUNT(*) FROM semantic_vectors WHERE model_id='remote:test'").fetchone()[0] == 2


def test_local_semantic_mode_requires_fastembed_and_never_silently_hashes(cognition_db, monkeypatch):
    from cognition import semantic

    monkeypatch.setenv("COGNITION_SEMANTIC_MODE", "local")
    monkeypatch.setattr(
        semantic, "_fastembed_vectors",
        lambda texts: ("fastembed:BAAI/bge-small-zh-v1.5", [semantic.vectorize(text) for text in texts]),
    )

    semantic.persist_many([("u1", "claim", "claim-fastembed", "semantic memory")])

    assert cognition_db.execute(
        "SELECT COUNT(*) FROM semantic_vectors WHERE model_id='fastembed:BAAI/bge-small-zh-v1.5'"
    ).fetchone()[0] == 1


def test_local_semantic_mode_surfaces_fastembed_failure(cognition_db, monkeypatch):
    from cognition import semantic

    monkeypatch.setattr(
        semantic, "_fastembed_vectors",
        lambda _texts: (_ for _ in ()).throw(RuntimeError("FastEmbed unavailable")),
    )

    with pytest.raises(RuntimeError, match="FastEmbed unavailable"):
        semantic.encode_many(["must not silently hash"])


def test_structured_recording_summaries_are_the_memory_input(cognition_db, monkeypatch):
    cognition_db.execute(
        """CREATE TABLE recordings (
            id TEXT PRIMARY KEY, user_id TEXT, title TEXT, filename TEXT, created_at TEXT,
            summary_json TEXT, category TEXT, status TEXT, duration_sec REAL
        )"""
    )
    cognition_db.execute(
        """INSERT INTO recordings VALUES (?,?,?,?,?,?,?,?,?)""",
        ("rec-summary", "u1", "Meeting", "meeting.opus", "2026-07-14T00:00:00+00:00",
         json.dumps({
             "current_intent": "Finalize launch plan",
             "key_decisions": ["Validate pricing before launch"],
             "action_items": ["Prepare customer interviews"],
             "tags": ["launch", "pricing"],
             "detailed_summary": "The team agreed to validate pricing with customer interviews before launch.",
         }), "business", "completed", 60.0),
    )
    monkeypatch.setattr(migration, "get_conn", lambda: cognition_db)

    imported = migration.import_legacy_recording_summaries(dry_run=False)
    source_id = imported["source_ids"][0]
    processed = process_source(source_id)

    assert imported["queued"] == 1
    assert store.get_source(source_id, "u1")["derivation_type"] == "summary"
    assert processed["claim_count"] >= 4

    raw_source, _, _ = store.create_source(
        user_id="u1", source_type="recording", content="Raw transcript", origin="legacy_recordings",
        external_id="recording:rec-summary",
    )
    store.create_claim(
        user_id="u1", source_id=raw_source["id"], content="Raw transcript claim", network="experience",
    )
    removed = migration.remove_legacy_recording_transcripts()

    assert removed["sources"] == 1
    assert store.get_source(raw_source["id"], "u1") is None
    assert store.get_source(source_id, "u1") is not None


def test_unrelated_project_is_not_auto_assigned_from_its_own_profile(cognition_db):
    project = store.create_project(
        user_id="u1", name="Pricing strategy", description="margin analysis and launch pricing", goal="improve unit economics"
    )
    source, _, _ = store.create_source(
        user_id="u1", source_type="manual", content="Meteorology notes: tidal patterns and sediment sampling."
    )

    process_source(source["id"])

    memberships = store.list_memberships(project["id"], "source")
    assert all(item["object_id"] != source["id"] for item in memberships)


def test_projects_keep_empty_tags_as_a_list_for_dashboard_clients(cognition_db):
    project = store.create_project(user_id="u1", name="No tags yet")
    # Simulate the malformed value written by the early v2 implementation.
    cognition_db.execute("UPDATE projects_v2 SET tags_json='{}' WHERE id=?", (project["id"],))
    cognition_db.commit()

    restored = store.get_project(project["id"], "u1")

    assert restored["tags"] == []
    assert cognition_db.execute("SELECT tags_json FROM projects_v2 WHERE id=?", (project["id"],)).fetchone()[0] == "{}"


def test_source_can_be_manually_labeled_with_multiple_projects(cognition_db):
    first = store.create_project(user_id="u1", name="First")
    second = store.create_project(user_id="u1", name="Second")
    source, _, _ = store.create_source(user_id="u1", source_type="manual", content="An evidence item")

    labels = store.set_source_projects(user_id="u1", source_id=source["id"], project_ids=[first["id"], second["id"]])
    source_row = store.list_sources("u1")[0]
    store.set_source_projects(user_id="u1", source_id=source["id"], project_ids=[second["id"]])

    assert {item["id"] for item in labels if item["status"] == "confirmed"} == {first["id"], second["id"]}
    assert {item["id"] for item in source_row["projects"]} == {first["id"], second["id"]}
    assert store.list_memberships(first["id"], "source")[0]["status"] == "rejected"


def test_deleting_project_keeps_sources_and_cleans_project_records(cognition_db):
    project = store.create_project(user_id="u1", name="Duplicate")
    source, _, _ = store.create_source(
        user_id="u1", source_type="manual", content="Keep this source", project_ids=[project["id"]]
    )
    store.add_membership(
        project_id=project["id"], object_type="source", object_id=source["id"],
        assignment_source="user", confidence=1.0, status="confirmed",
    )
    store.create_insight(
        user_id="u1", project_id=project["id"], insight_type="summary",
        title="Duplicate insight", content="Remove with project", confidence=0.8,
    )
    store.enqueue_job(user_id="u1", job_type="project_insight", payload={"project_id": project["id"]})

    deleted = store.delete_project(project_id=project["id"], user_id="u1")

    assert deleted and deleted["removed"]["memberships"] == 1
    assert deleted["removed"]["insights"] == 1
    assert deleted["removed"]["jobs"] == 1
    assert store.get_project(project["id"], "u1") is None
    assert store.get_source(source["id"], "u1") is not None
    metadata = cognition_db.execute("SELECT metadata_json FROM sources WHERE id=?", (source["id"],)).fetchone()[0]
    assert project["id"] not in metadata


def test_context_can_be_scoped_to_multiple_projects(cognition_db):
    first = store.create_project(user_id="u1", name="VibryAI")
    second = store.create_project(user_id="u1", name="客户研究")
    source, _, _ = store.create_source(
        user_id="u1", source_type="manual", content="VibryAI 的客户研究需要验证定价假设",
        project_ids=[first["id"], second["id"]],
    )
    process_source(source["id"])

    result = store.search_context("u1", "客户研究 定价", [first["id"], second["id"]])
    assert result["claims"]


def test_dirty_project_creates_evidence_bound_insight(cognition_db, monkeypatch):
    project = store.create_project(user_id="u1", name="VibryAI")
    source, _, _ = store.create_source(
        user_id="u1", source_type="manual", content="VibryAI 决定先完成统一 Source 层",
        project_ids=[project["id"]],
    )
    process_source(source["id"])
    assert store.dirty_projects()[0]["id"] == project["id"]

    scheduler = CognitiveScheduler()
    assert scheduler.run_once() == 1
    job = store.claim_next_job()
    assert job["job_type"] == "process_source"  # original source job remains durable

    monkeypatch.setattr(
        "cognition.insights._llm_insights",
        lambda brief: [{"type": "recommendation", "title": "下一步", "content": "建立统一 Source API", "confidence": 0.8, "claim_ids": [brief["claims"][0]["id"]]}],
    )
    insights = generate_project_insights(project["id"], "u1")

    assert insights[0]["evidence"][0]["claim_id"]
    assert not store.dirty_projects()


def test_context_compiler_returns_cited_untrusted_evidence(cognition_db):
    source, _, _ = store.create_source(
        user_id="u1", source_type="manual", content="用户决定优先建设项目化记忆系统"
    )
    process_source(source["id"])

    result = compile_context(user_id="u1", query="项目化记忆", token_budget=300)

    assert "untrusted historical evidence" in result["context"]
    assert source["id"] in result["context"]
    assert result["estimated_tokens"] <= 300


def test_dashboard_and_mcp_tools_share_the_cognitive_contract(cognition_db):
    project = store.create_project(user_id="u1", name="VibryAI")
    source, _, _ = store.create_source(
        user_id="u1", source_type="manual", content="VibryAI context is evidence-backed.",
        project_ids=[project["id"]],
    )
    process_source(source["id"])

    snapshot = store.dashboard_snapshot("u1")
    from mcp_server import handle
    reply = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "vibry_project_brief", "arguments": {"project_id": project["id"]},
    }}, "u1")

    assert snapshot["counts"]["projects"] == 1
    assert json.loads(reply["result"]["content"][0]["text"])["project"]["id"] == project["id"]


def test_jobs_are_visible_and_failed_jobs_can_be_retried(cognition_db):
    _source, job, _ = store.create_source(user_id="u1", source_type="manual", content="retry evidence")
    store.fail_job(job["id"], "temporary failure")
    # Force the terminal failure state so retry behavior is deterministic.
    cognition_db.execute("UPDATE cognition_jobs SET attempts=max_attempts WHERE id=?", (job["id"],))
    store.fail_job(job["id"], "terminal failure")

    assert store.list_jobs("u1")[0]["status"] == "failed"
    retried = store.retry_job(job["id"], "u1")
    assert retried["status"] == "queued"
    assert retried["run_after"] is None


def test_failed_jobs_back_off_and_stuck_jobs_are_recovered(cognition_db):
    queued = store.enqueue_job(user_id="u1", job_type="memory_ingest")
    claimed = store.claim_next_job({"memory_ingest"})
    assert claimed["id"] == queued["id"]

    status = store.fail_job(
        claimed["id"], "temporary", claimed["lease_owner"],
    )
    delayed = store.get_job(claimed["id"], "u1")

    assert status == "queued"
    assert delayed["run_after"] is not None
    cognition_db.execute(
        "UPDATE cognition_jobs SET status='running',lease_until='2000-01-01T00:00:00+00:00',run_after=NULL WHERE id=?",
        (claimed["id"],),
    )
    cognition_db.commit()
    recovered = store.recover_stuck_jobs()

    assert recovered == {"released": 1, "failed": 0}
    assert store.get_job(claimed["id"], "u1")["status"] == "queued"
    snapshot = store.queue_snapshot()
    assert snapshot["queued"] == 1
    assert snapshot["by_type"]["memory_ingest"]["queued"] == 1


def test_worker_job_lanes_claim_only_allowed_types(cognition_db):
    store.enqueue_job(user_id="u1", job_type="transcribe_recording")
    memory_job = store.enqueue_job(user_id="u1", job_type="memory_ingest")

    claimed = store.claim_next_job({"memory_ingest", "process_source"})

    assert claimed["id"] == memory_job["id"]
    assert claimed["job_type"] == "memory_ingest"


def test_memory_matrix_auto_accepts_high_confidence_and_only_asks_low_confidence(
    cognition_db,
):
    high = store.create_l4_item(
        user_id="u1", title="稳定偏好", content_html="偏好完整纪要", confidence=0.9,
    )
    medium = store.create_l4_item(
        user_id="u1", title="待观察", content_html="可能偏好日报", confidence=0.65,
    )
    low = store.create_l4_item(
        user_id="u1", title="需要确认", content_html="可能负责销售", confidence=0.3,
    )

    matrix = store.memory_matrix("u1")
    visible_ids = {item["id"] for item in matrix["profile"]}

    assert high["status"] == "confirmed"
    assert high["id"] in visible_ids
    assert medium["id"] not in visible_ids
    assert low["id"] in visible_ids
    assert matrix["pending_confirmations"] == 1
