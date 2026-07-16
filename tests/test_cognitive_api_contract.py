"""Public v2 contract checks for the cognitive Dashboard and Agent control plane."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_cognitive_routes_replace_legacy_management_routes():
    from app.main import app

    expanded_routes = []
    for route in app.routes:
        original_router = getattr(route, "original_router", None)
        expanded_routes.extend(original_router.routes if original_router else [route])
    routes = {route.path for route in expanded_routes if hasattr(route, "path")}
    required = {
        "/api/v2/dashboard", "/api/v2/operations", "/api/v2/plugins",
        "/api/v2/jobs/{job_id}/retry", "/api/v2/insights/run",
        "/api/v2/projects/{project_id}/workspace", "/api/v2/projects/{project_id}/chat",
        "/api/v2/recordings/{recording_id}/content",
        "/api/v2/memory-matrix", "/api/v2/memory-matrix/chat",
        "/admin/api/v2/semantic/reindex",
        "/admin/api/v2/migrations/recording-summaries",
    }
    assert required.issubset(routes)
    project_methods = set().union(*[
        route.methods for route in expanded_routes
        if getattr(route, "path", None) == "/api/v2/projects/{project_id}"
    ])
    assert {"PATCH", "DELETE"}.issubset(project_methods)
    assert "/api/memories" not in routes
    assert "/api/wiki/status" not in routes
    assert "/api/v2/knowledge" not in routes
    assert "/admin/api/v2/migrations/wiki" not in routes


def test_mcp_control_plane_exposes_a_pasteable_client_configuration():
    from mcp_server import TOOLS
    from routers.cognition import get_operations
    import asyncio

    class RequestStub:
        headers = {}

    operations = asyncio.run(get_operations(RequestStub()))
    config = operations["mcp"]["client_config"]["mcpServers"]["vibry-ai"]

    assert config["command"]
    assert config["args"][-2:] == ["--user-id", "<your-user-id>"]
    assert {tool["name"] for tool in TOOLS} == set(operations["mcp"]["tools"])
