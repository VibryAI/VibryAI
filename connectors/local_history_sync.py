"""Explicit-path importer for Codex/Claude Code JSONL history exports.

The connector runs on the machine holding the history. It never scans home
directories, so the user decides exactly which file is sent to VibryAI Server.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from urllib.request import Request, urlopen


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for value_item in value for item in _strings(value_item)]
    if isinstance(value, dict):
        preferred = [value.get(key) for key in ("content", "text", "message") if value.get(key)]
        return [item for candidate in preferred for item in _strings(candidate)]
    return []


def read_history(path: Path, max_chars: int) -> str:
    chunks: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        chunks.extend(_strings(record))
        if sum(len(chunk) for chunk in chunks) >= max_chars:
            break
    return "\n".join(chunks)[:max_chars].strip()


def upload(*, server: str, token: str, user_id: str, kind: str, path: Path, project_ids: list[str], max_chars: int) -> dict:
    content = read_history(path, max_chars)
    if not content:
        raise ValueError("no readable text found in history file")
    fingerprint = hashlib.sha256(path.read_bytes()).hexdigest()
    body = json.dumps({
        "content": content, "source_type": "agent_history", "origin": f"{kind}_local",
        "title": path.name, "external_id": f"{kind}:{fingerprint}", "project_ids": project_ids,
        "metadata": {"connector": "local_history_sync", "history_kind": kind, "local_path_name": path.name},
    }, ensure_ascii=False).encode("utf-8")
    request = Request(f"{server.rstrip('/')}/api/v2/sources", data=body, method="POST")
    request.add_header("Content-Type", "application/json; charset=utf-8")
    request.add_header("Authorization", f"Bearer {token or user_id}")
    with urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync an explicitly selected local Agent JSONL history file.")
    parser.add_argument("path", type=Path)
    parser.add_argument("--kind", choices=("codex", "claude_code"), required=True)
    parser.add_argument("--server", default="http://127.0.0.1:9999")
    parser.add_argument("--token", default="")
    parser.add_argument("--user-id", default="anonymous")
    parser.add_argument("--project-id", action="append", default=[])
    parser.add_argument("--max-chars", type=int, default=200_000)
    args = parser.parse_args()
    print(json.dumps(upload(
        server=args.server, token=args.token, user_id=args.user_id, kind=args.kind, path=args.path,
        project_ids=args.project_id, max_chars=max(1_000, min(args.max_chars, 2_000_000)),
    ), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
