# VibryAI Server Agent Integration

## MCP server

Run the MCP adapter on the same machine as VibryAI Server:

```json
{
  "mcpServers": {
    "vibry-ai": {
      "command": "python",
      "args": ["D:/VibryAI/VibryServer/mcp_server.py", "--user-id", "your_user_id"]
    }
  }
}
```

The adapter exposes four tools:

- `vibry_search_context`: evidence-bound claims, project, and insight context.
- `vibry_list_projects`: active project list.
- `vibry_project_brief`: source-backed project state and insights.
- `vibry_capture_source`: explicitly approved Agent output written as a queued Source.

The MCP adapter never exposes SQLite access. Its writes pass through the same
Source and durable-job contract used by VibryCard.

## Local Codex and Claude Code history

History collection must run locally because a remote VibryAI Server cannot and
should not read a developer's filesystem. The connector accepts one explicit
JSONL file and does not scan home directories.

```powershell
python D:/VibryAI/VibryServer/connectors/local_history_sync.py `
  D:/exports/codex-session.jsonl `
  --kind codex `
  --server http://127.0.0.1:9999 `
  --token YOUR_VIBRY_TOKEN `
  --project-id prj_example
```

For Claude Code, use `--kind claude_code`. Re-running the same exported file is
idempotent because the connector creates a content-hash external ID.

## Permissions and review

The first release treats built-in adapters as trusted. Keep Agent writes
explicit and review their Source and project assignments in Dashboard before
relying on them for future context.
