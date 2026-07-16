# VibryAI Server

VibryAI Server is the implementation of the Vibry.AI personal cognitive core.
It combines an OpenAI-compatible gateway, VibryCard transcription ingestion,
projects, evidence-backed claims, nightly insights, and trusted Agent
integrations in one server.

## Product Boundary

- **VibryAI Server**: cognitive core and Dashboard.
- **VibryCard**: voice capture client. Upload and transcription APIs remain
  compatible and write their transcript into the unified Source pipeline.
- **Agents and plugins**: external clients. They use OpenAI API, REST, or MCP;
  they never access the cognitive SQLite schema directly.

The runtime no longer depends on Mem0, Qdrant, Wiki, or a separate RAG service.

## Cognitive Model

1. **L0 Source**: recordings, documents, chats, web captures, and Agent history.
2. **L1 Claims**: evidence-linked claims across world, experience, observation,
   and opinion networks, with entity normalization and exact deduplication.
3. **L2 Projects**: multi-project memberships, goals, stages, and state.
4. **L3 Insights**: evidence-bound nightly or manually-triggered project insight.
5. **L4 Memory Matrix**: confirmed global cognition, rich conversation, and feedback history.

The gateway compiles Source evidence, Claims, project context, and evidence-bound
Insights directly. There is no separate Wiki or RAG knowledge layer.

## Run

```powershell
cd D:/VibryAI/VibryServer
python run.py
```

- Dashboard: <http://127.0.0.1:9999/admin>
- OpenAPI: <http://127.0.0.1:9999/docs>
- Health: <http://127.0.0.1:9999/api/health>

## Key APIs

- `POST /api/transcribe`: VibryCard JSON or multipart audio upload.
- `POST /api/v2/sources`: capture text, documents, Agent history, or other L0 evidence.
- `GET/POST /api/v2/projects`: project workspaces and memberships.
- `GET /api/v2/projects/{id}/workspace`: project background, chat, sources, tasks, insights, and logs.
- `POST /api/v2/projects/{id}/chat`: project-scoped conversation with memory write-back.
- `GET /api/v2/memory-matrix` and `POST /api/v2/memory-matrix/chat`: global L4 conversation and confirmation surface.
- `POST /api/v2/insights/run`: queue project insight generation.
- `POST /api/v2/context/build`: compile project-scoped cognitive context.
- `GET /api/v2/operations`: Dashboard control plane for jobs, plugins, and MCP.
- `POST /v1/chat/completions`: OpenAI-compatible AI gateway with cognitive context.

## Agent Integration

Run the MCP adapter locally:

```powershell
python mcp_server.py --user-id your_user_id
```

It exposes `vibry_search_context`, `vibry_list_projects`,
`vibry_project_brief`, and `vibry_capture_source`. For Codex and Claude Code
history imports, use the explicit-path connector described in
[AGENT_INTEGRATION.md](docs/AGENT_INTEGRATION.md).

## Verification

```powershell
python -m pytest -q
```

See [VibryServer_Refactor_Plan.md](docs/VibryServer_Refactor_Plan.md) for the
architecture, migration, privacy, and benchmark strategy.

See [VibryAI_Product_Requirements.md](docs/VibryAI_Product_Requirements.md) for
the product experience, memory feedback loop, project workspace, and L0-L4
acceptance criteria.
