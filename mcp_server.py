"""Minimal stdio MCP adapter for local Codex and Claude Code clients.

Run it beside VibryAI Server with ``python mcp_server.py --user-id <id>``.
It intentionally exposes application-level capabilities only; no client gets
direct access to the core SQLite schema.
"""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import json
import sys
from typing import Any

import db
from cognition import store
from cognition.context import compile_context


TOOLS = [
    {
        "name": "vibry_search_context",
        "description": "Retrieve evidence-bound personal, project, and professional context.",
        "inputSchema": {"type": "object", "properties": {
            "query": {"type": "string"}, "project_ids": {"type": "array", "items": {"type": "string"}},
            "token_budget": {"type": "integer", "minimum": 200, "maximum": 8000},
        }, "required": ["query"]},
    },
    {
        "name": "vibry_list_projects",
        "description": "List the user's projects and their current stage.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "vibry_project_brief",
        "description": "Read a project brief with source-backed claims and insights.",
        "inputSchema": {"type": "object", "properties": {"project_id": {"type": "string"}}, "required": ["project_id"]},
    },
    {
        "name": "vibry_capture_source",
        "description": "Write a user-approved external note or agent result into Vibry.AI as a queued Source.",
        "inputSchema": {"type": "object", "properties": {
            "content": {"type": "string"}, "title": {"type": "string"}, "source_type": {"type": "string"},
            "project_ids": {"type": "array", "items": {"type": "string"}}, "external_id": {"type": "string"},
        }, "required": ["content"]},
    },
]


def _result(value: Any) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}


def _call_tool(name: str, arguments: dict, user_id: str) -> dict:
    if name == "vibry_search_context":
        return _result(compile_context(
            user_id=user_id, query=str(arguments["query"]), project_ids=arguments.get("project_ids") or [],
            token_budget=int(arguments.get("token_budget", 1200)),
        ))
    if name == "vibry_list_projects":
        return _result({"projects": store.list_projects(user_id)})
    if name == "vibry_project_brief":
        brief = store.project_brief(str(arguments["project_id"]), user_id)
        return _result(brief or {"error": "project not found"})
    if name == "vibry_capture_source":
        source, job, duplicate = store.create_source(
            user_id=user_id, source_type=str(arguments.get("source_type") or "agent_history"),
            content=str(arguments["content"]), origin="mcp", title=str(arguments.get("title") or "MCP capture"),
            external_id=str(arguments.get("external_id") or ""), project_ids=arguments.get("project_ids") or [],
            metadata={"capture_mode": "mcp_user_approved"},
        )
        return _result({"source": source, "job": job, "duplicate": duplicate})
    return {"isError": True, "content": [{"type": "text", "text": f"Unknown tool: {name}"}]}


def handle(message: dict, user_id: str) -> dict | None:
    method = message.get("method")
    request_id = message.get("id")
    if method == "notifications/initialized":
        return None
    if method == "initialize":
        response = {"protocolVersion": message.get("params", {}).get("protocolVersion", "2024-11-05"), "capabilities": {"tools": {}}, "serverInfo": {"name": "vibry-ai", "version": "1.0.0"}}
    elif method == "tools/list":
        response = {"tools": TOOLS}
    elif method == "tools/call":
        params = message.get("params") or {}
        response = _call_tool(params.get("name", ""), params.get("arguments") or {}, user_id)
    else:
        response = {"error": {"code": -32601, "message": f"Method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": request_id, "result": response} if "error" not in response else {"jsonrpc": "2.0", "id": request_id, **response}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", default="anonymous")
    args = parser.parse_args()
    # SQLite bootstrap has legacy informational prints; MCP stdio must contain
    # JSON-RPC responses only, so keep those diagnostics on stderr.
    with redirect_stdout(sys.stderr):
        db.init_db()
    for line in sys.stdin:
        try:
            reply = handle(json.loads(line), args.user_id)
            if reply is not None:
                print(json.dumps(reply, ensure_ascii=False), flush=True)
        except Exception as exc:
            print(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": str(exc)}}), flush=True)


if __name__ == "__main__":
    main()
