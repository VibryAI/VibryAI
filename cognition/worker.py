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
    def __init__(self, poll_seconds: float = 0.5):
        self._poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="vibry-cognition", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            job = None
            try:
                job = store.claim_next_job()
                if not job:
                    self._stop.wait(self._poll_seconds)
                    continue
                if job["job_type"] == "process_source":
                    process_source(job["source_id"])
                    store.complete_job(job["id"])
                elif job["job_type"] == "project_insight":
                    payload = json.loads(job.get("payload_json") or "{}")
                    generate_project_insights(payload["project_id"], job["user_id"])
                    store.complete_job(job["id"])
                else:
                    raise ValueError(f"unknown job type: {job['job_type']}")
            except Exception as exc:
                if job:
                    store.fail_job(job["id"], str(exc))
                log.exception("Cognitive job failed: %s", exc)
                self._stop.wait(self._poll_seconds)
