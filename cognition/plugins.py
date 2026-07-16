"""Trusted built-in plugin discovery.

Plugins describe boundary adapters only. They do not receive a database handle;
their data enters the cognitive core through the Source contract.
"""

from __future__ import annotations

import json
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "plugins" / "builtin"
REQUIRED_FIELDS = {"id", "name", "version", "kind", "capabilities"}


def list_plugins() -> list[dict]:
    plugins: list[dict] = []
    for manifest_path in sorted(PLUGIN_ROOT.glob("*/manifest.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not REQUIRED_FIELDS.issubset(manifest):
            continue
        manifest["trusted"] = True
        manifest["manifest_path"] = str(manifest_path.relative_to(PLUGIN_ROOT.parent.parent))
        plugins.append(manifest)
    return plugins
