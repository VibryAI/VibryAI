"""A small durable worker for cognitive jobs.

Jobs are persisted in SQLite, so a process restart does not lose incoming
recordings or documents. A dedicated process can replace this thread later
without changing the queue contract.
"""

from __future__ import annotations

import logging
import json
import threading

from cognition import store
from cognition.pipeline import process_source
from cognition.insights import generate_project_insights

log = logging.getLogger("vibry.cognition.worker")


class CognitiveWorker:
    def __init__(
        self, poll_seconds: float = 0.5, *, name: str = "cognition",
        job_types: set[str] | None = None,
    ):
        self._poll_seconds = poll_seconds
        self._name = name
        self._job_types = job_types
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"vibry-{self._name}", daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            job = None
            heartbeat_stop = None
            heartbeat_thread = None
            try:
                job = store.claim_next_job(self._job_types)
                if not job:
                    self._stop.wait(self._poll_seconds)
                    continue
                heartbeat_stop = threading.Event()
                heartbeat_thread = threading.Thread(
                    target=self._heartbeat,
                    args=(job["id"], job.get("lease_owner", ""), heartbeat_stop),
                    name=f"vibry-{self._name}-heartbeat-{job['id'][-8:]}",
                    daemon=True,
                )
                heartbeat_thread.start()
                if job["job_type"] == "process_source":
                    process_source(job["source_id"])
                    from services.recording_pipeline import on_source_processed
                    on_source_processed(job["source_id"])
                elif job["job_type"] == "project_insight":
                    payload = json.loads(job.get("payload_json") or "{}")
                    generate_project_insights(
                        payload["project_id"], job["user_id"],
                        payload.get("trigger", "scheduled"),
                    )
                elif job["job_type"] in {
                    "transcribe_recording", "summarize_recording", "recording_insight",
                    "memory_ingest", "memory_insight",
                }:
                    from services.recording_pipeline import process_recording_job
                    process_recording_job(job)
                elif job["job_type"] == "aggregate_minutes":
                    from services.aggregate_pipeline import process_aggregate_job
                    process_aggregate_job(job)
                else:
                    raise ValueError(f"unknown job type: {job['job_type']}")
                store.complete_job(job["id"], job.get("lease_owner"))
            except Exception as exc:
                if job:
                    status = store.fail_job(job["id"], str(exc), job.get("lease_owner"))
                    if job.get("job_type") == "aggregate_minutes":
                        from services.aggregate_pipeline import on_aggregate_job_failed
                        on_aggregate_job_failed(job, str(exc), status == "failed")
                    else:
                        from services.recording_pipeline import on_recording_job_failed
                        on_recording_job_failed(job, str(exc), status == "failed")
                log.exception("Cognitive job failed: %s", exc)
                self._stop.wait(self._poll_seconds)
            finally:
                if heartbeat_stop:
                    heartbeat_stop.set()
                if heartbeat_thread:
                    heartbeat_thread.join(timeout=1.0)

    def _heartbeat(self, job_id: str, lease_owner: str, stop: threading.Event) -> None:
        if not lease_owner:
            return
        while not stop.wait(30.0):
            try:
                if not store.renew_job_lease(job_id, lease_owner):
                    return
            except Exception as exc:
                log.warning("Job lease heartbeat failed for %s: %s", job_id, exc)
