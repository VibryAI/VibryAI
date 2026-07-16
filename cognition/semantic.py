"""Persistent semantic lanes with an OpenAI-compatible embedding provider.

When a configured embedding provider is unavailable, a deterministic local
feature lane preserves the retrieval and clustering contract. This lets a
fresh local installation work immediately without silently losing data, while
production deployments use the model selected in the existing Model settings.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import urllib.error
import urllib.request
import uuid

from cognition import store


DIMENSIONS = 256
_FASTEMBED_MODELS: dict[str, object] = {}
_FASTEMBED_ERRORS: dict[str, str] = {}
_FASTEMBED_LOCK = threading.Lock()


def _tokens(text: str) -> list[str]:
    words = re.findall(r"[A-Za-z0-9_]{2,}|[\u4e00-\u9fff]+", text.lower())
    cjk = "".join(char for char in text.lower() if "\u4e00" <= char <= "\u9fff")
    grams = [cjk[index:index + 2] for index in range(max(0, len(cjk) - 1))]
    return words + grams


def vectorize(text: str) -> list[float]:
    values = [0.0] * DIMENSIONS
    for token in _tokens(text):
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        slot = int.from_bytes(digest[:4], "big") % DIMENSIONS
        values[slot] += 1.0 if digest[4] & 1 else -1.0
    magnitude = math.sqrt(sum(value * value for value in values))
    return [value / magnitude for value in values] if magnitude else values


def _normalize(values: list[float]) -> list[float]:
    magnitude = math.sqrt(sum(value * value for value in values))
    return [value / magnitude for value in values] if magnitude else values


def _remote_enabled() -> bool:
    mode = os.getenv("COGNITION_SEMANTIC_MODE", "auto").lower()
    if mode in {"local", "fastembed"}:
        return False
    try:
        from app.config import config
        return bool(config.embedding.api_key and config.embedding.base_url.startswith(("http://", "https://")))
    except Exception:
        return False


def _fastembed_model_name() -> str:
    from app.config import config
    return config.embedding.model or "BAAI/bge-small-zh-v1.5"


def _fastembed_vectors(texts: list[str]) -> tuple[str, list[list[float]]]:
    """Embed locally with FastEmbed. Failure is explicit: never silently hash."""
    model_name = _fastembed_model_name()
    try:
        from fastembed import TextEmbedding
    except ImportError as exc:
        raise RuntimeError(
            "FastEmbed is required for local semantic retrieval. Install the fastembed dependency."
        ) from exc

    with _FASTEMBED_LOCK:
        model = _FASTEMBED_MODELS.get(model_name)
        if model is None:
            try:
                model = TextEmbedding(model_name=model_name)
            except Exception as exc:
                message = f"Unable to initialize FastEmbed model {model_name}: {exc}"
                _FASTEMBED_ERRORS[model_name] = message
                raise RuntimeError(message) from exc
            _FASTEMBED_MODELS[model_name] = model
            _FASTEMBED_ERRORS.pop(model_name, None)

    try:
        vectors = [_normalize([float(value) for value in vector]) for vector in model.embed(texts)]
    except Exception as exc:
        message = f"FastEmbed model {model_name} failed while encoding text: {exc}"
        _FASTEMBED_ERRORS[model_name] = message
        raise RuntimeError(message) from exc
    if len(vectors) != len(texts) or not all(vectors):
        message = f"FastEmbed model {model_name} did not return every requested embedding."
        _FASTEMBED_ERRORS[model_name] = message
        raise RuntimeError(message)
    return f"fastembed:{model_name}", vectors


def _remote_vectors(texts: list[str]) -> tuple[str, list[list[float]]]:
    from app.config import config

    payload = json.dumps({"model": config.embedding.model, "input": texts}).encode("utf-8")
    request = urllib.request.Request(
        f"{config.embedding.base_url.rstrip('/')}/embeddings",
        data=payload,
        headers={"Authorization": f"Bearer {config.embedding.api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    timeout = min(max(int(config.embedding.timeout), 3), 20)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.loads(response.read().decode("utf-8"))
    rows = body.get("data") or []
    vectors = [_normalize([float(value) for value in row["embedding"]]) for row in rows]
    if len(vectors) != len(texts) or not all(vectors):
        raise ValueError("embedding response did not contain every requested vector")
    return f"remote:{config.embedding.model}", vectors


def encode_many(texts: list[str]) -> tuple[str, list[list[float]]]:
    """Return remote or local FastEmbed vectors without silent quality degradation."""
    if _remote_enabled():
        try:
            return _remote_vectors(texts)
        except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Configured remote embedding provider failed: {exc}") from exc
    return _fastembed_vectors(texts)


def encode(text: str) -> tuple[str, list[float]]:
    model_id, vectors = encode_many([text])
    return model_id, vectors[0]


def dot(left: list[float], right: list[float]) -> float:
    return max(0.0, sum(a * b for a, b in zip(left, right)))


def similarity(left: str, right: str) -> float:
    _model_id, vectors = encode_many([left, right])
    return dot(vectors[0], vectors[1])


def similarities(query: str, candidates: list[str]) -> tuple[str, list[float]]:
    """Score a query against many candidates with one provider request."""
    if not candidates:
        return "none", []
    model_id, vectors = encode_many([query, *candidates])
    return model_id, [dot(vectors[0], vector) for vector in vectors[1:]]


def status() -> dict:
    """Safe control-plane description; credentials are intentionally omitted."""
    if _remote_enabled():
        from app.config import config
        return {"mode": "remote", "model_id": f"remote:{config.embedding.model}"}
    model_name = _fastembed_model_name()
    if model_name in _FASTEMBED_ERRORS:
        return {"mode": "fastembed_error", "model_id": f"fastembed:{model_name}", "error": _FASTEMBED_ERRORS[model_name]}
    return {
        "mode": "fastembed" if model_name in _FASTEMBED_MODELS else "fastembed_pending",
        "model_id": f"fastembed:{model_name}",
    }


def persist(*, user_id: str, object_type: str, object_id: str, text: str) -> None:
    persist_many([(user_id, object_type, object_id, text)])


def persist_many(items: list[tuple[str, str, str, str]]) -> None:
    """Persist a homogeneous or mixed object batch with one embedding request."""
    if not items:
        return
    conn = store.get_conn()
    model_id, vectors = encode_many([item[3] for item in items])
    rows = []
    for (user_id, object_type, object_id, text), vector in zip(items, vectors):
        rows.append((
            object_type, object_id, user_id, model_id,
            hashlib.sha256(text.encode("utf-8")).hexdigest(), json.dumps(vector), store._now(),
        ))
    conn.executemany(
        """INSERT INTO semantic_vectors (object_type,object_id,user_id,model_id,text_hash,vector_json,updated_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(object_type,object_id,model_id) DO UPDATE SET text_hash=excluded.text_hash,
           vector_json=excluded.vector_json,updated_at=excluded.updated_at""",
        rows,
    )
    conn.commit()


def rebuild_all_vectors(*, user_id: str | None = None, batch_size: int = 64) -> dict:
    """Re-embed active claims after a local model change."""
    conn = store.get_conn()
    where, args = "", []
    if user_id:
        where, args = " AND user_id=?", [user_id]
    claims = conn.execute(
        f"SELECT id,user_id,content FROM claims_v2 WHERE status='active'{where} ORDER BY id", args
    ).fetchall()
    items = [
        (row["user_id"], "claim", row["id"], row["content"])
        for row in claims
    ]
    # Load and validate the configured provider even when the database is empty.
    model_id, _ = encode_many(["Vibry.AI semantic index health check"])
    batch_size = max(1, min(batch_size, 256))
    for start in range(0, len(items), batch_size):
        batch = items[start:start + batch_size]
        persist_many(batch)
        for _item_user_id, object_type, object_id, _text in batch:
            conn.execute(
                "DELETE FROM semantic_vectors WHERE object_type=? AND object_id=? AND model_id != ?",
                (object_type, object_id, model_id),
            )
        conn.commit()
    return {"model_id": model_id, "claims": len(claims), "total": len(items)}


def suggest_claim_relations(claim: dict, threshold: float = 0.72) -> list[dict]:
    conn = store.get_conn()
    rows = conn.execute(
        """SELECT id,content FROM claims_v2 WHERE user_id=? AND network=? AND status='active' AND id != ?
           ORDER BY created_at DESC LIMIT 200""",
        (claim["user_id"], claim["network"], claim["id"]),
    ).fetchall()
    model_id, scores = similarities(claim["content"], [row["content"] for row in rows])
    suggestions = []
    for row, score in zip(rows, scores):
        if score < threshold:
            continue
        conn.execute(
            """INSERT OR IGNORE INTO claim_relations (
               id,source_claim_id,target_claim_id,relation_type,confidence,reason_json,created_at
            ) VALUES (?,?,?,?,?,?,?)""",
            (f"rel_{uuid.uuid4().hex}", claim["id"], row["id"], "similar", score,
             json.dumps({"lane": model_id, "threshold": threshold}), store._now()),
        )
        suggestions.append({"target_claim_id": row["id"], "confidence": score})
    conn.commit()
    return suggestions
