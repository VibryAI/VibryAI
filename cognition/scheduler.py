"""Daily scheduler for dirty project insight jobs."""

from __future__ import annotations

import logging
import threading
from datetime import datetime

from cognition import store

log = logging.getLogger("vibry.cognition.scheduler")


class CognitiveScheduler:
    def __init__(self, nightly_time: str = "02:30", poll_seconds: float = 30.0):
        self._nightly_time = nightly_time
        self._poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="vibry-cognition-scheduler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def run_once(self) -> int:
        count = 0
        for project in store.dirty_projects():
            store.enqueue_job(
                user_id=project["user_id"], job_type="project_insight",
                payload={"project_id": project["id"], "trigger": "nightly"},
            )
            count += 1
        return count

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                now = datetime.now()
                date_key = now.strftime("%Y-%m-%d")
                if now.strftime("%H:%M") >= self._nightly_time and store.get_meta("nightly_insights_date") != date_key:
                    count = self.run_once()
                    store.set_meta("nightly_insights_date", date_key)
                    log.info("Queued nightly insights for %s dirty projects", count)
            except Exception as exc:
                log.exception("Cognitive scheduler failed: %s", exc)
            self._stop.wait(self._poll_seconds)
