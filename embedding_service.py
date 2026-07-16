#!/usr/bin/env python
"""Vibry AI — 本地 Embedding 服务

独立进程，暴露 OpenAI 兼容 /v1/embeddings 端点。
基于 fastembed (ONNX Runtime, CPU), 无需 GPU / PyTorch。

用法:
    pip install fastembed uvicorn
    python embedding_service.py                        # 默认 BGE-small
    EMBEDDING_MODEL=BAAI/bge-large-zh-v1.5 python embedding_service.py
    EMBEDDING_PORT=8010 python embedding_service.py

切换方式:
    后台 → Embedding 模型配置 → Base URL 填 http://127.0.0.1:8009/v1
    云端的 API Key 留空即可自动走本地服务。
"""

import os
import json
import time
import logging
from typing import Optional

MODEL_NAME = os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5")
HOST = os.getenv("EMBEDDING_HOST", "0.0.0.0")
PORT = int(os.getenv("EMBEDDING_PORT", "8009"))

# ---- Logging ----
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [embed] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("embed")

# ---- FastAPI App ----
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn

app = FastAPI(title="Vibry Embedding Service", version="0.1.0")

# ---- 可用模型 (预下载到本地缓存) ----
AVAILABLE_MODELS = [
    "BAAI/bge-small-zh-v1.5",   # 512d, ~100MB, 轻量快速
    "thenlper/gte-base",        # 768d, ~440MB, 阿里 GTE, 中文优秀
    "thenlper/gte-large",       # 1024d, ~1.2GB, 最高精度
]

# ---- Model (lazy load, supports runtime switch) ----
_embedding_model: Optional[object] = None
_model_name_loaded: str = ""


def _download_model(model_name: str):
    """预下载模型到 HuggingFace 缓存 (不加载到内存)"""
    try:
        from huggingface_hub import snapshot_download
        snapshot_download(model_name, resume_download=True)
        log.info(f"Cached: {model_name}")
    except ImportError:
        # 没有 huggingface_hub 时，fastembed 首次加载会自动下载
        pass


def preload_all_models():
    """启动时预下载所有模型到本地缓存"""
    log.info("Pre-caching models to disk (download once, switch instantly)...")
    for name in AVAILABLE_MODELS:
        try:
            _download_model(name)
        except Exception as e:
            log.warning(f"Cache failed for {name}: {e}")
    log.info("Pre-cache done.")


def get_model(model_name: str = ""):
    """加载或热切换模型 (从本地缓存，无需重新下载)"""
    global _embedding_model, _model_name_loaded

    target = model_name or MODEL_NAME

    if target not in AVAILABLE_MODELS:
        log.warning(f"Unknown model: {target}, using {MODEL_NAME}")
        target = MODEL_NAME

    # 同名已加载 → 直接复用
    if _embedding_model is not None and _model_name_loaded == target:
        return _embedding_model

    # 切换模型 → 释放旧的
    if _embedding_model is not None:
        log.info(f"Switching: {_model_name_loaded} → {target}")
        _embedding_model = None

    from fastembed import TextEmbedding
    t0 = time.perf_counter()
    _embedding_model = TextEmbedding(
        model_name=target,
        max_length=512,
        threads=os.cpu_count() or 4,
    )
    _ = list(_embedding_model.embed(["warmup"]))
    elapsed = (time.perf_counter() - t0) * 1000
    _model_name_loaded = target
    log.info(f"Loaded: {target} ({elapsed:.0f}ms)")

    return _embedding_model


# ============================================================
# OpenAI-compatible endpoints
# ============================================================

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "model_loaded": _model_name_loaded or MODEL_NAME,
        "available": AVAILABLE_MODELS,
        "backend": "fastembed (ONNX Runtime, CPU)",
    }


@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [{
            "id": MODEL_NAME,
            "object": "model",
            "created": 0,
            "owned_by": "vibry-ai",
        }],
    }


@app.post("/v1/embeddings")
async def embeddings(request: Request):
    """OpenAI-compatible embeddings endpoint"""
    t0 = time.perf_counter()

    body = await request.json()
    raw_input = body.get("input", "")

    # Normalize input: string → [string], list → as-is
    if isinstance(raw_input, str):
        texts = [raw_input]
    elif isinstance(raw_input, list):
        if raw_input and isinstance(raw_input[0], dict):
            # Multi-modal format: [{"type":"text","text":"hello"}]
            texts = [item.get("text", "") for item in raw_input]
        else:
            texts = [str(t) for t in raw_input]
    else:
        texts = [str(raw_input)]

    model_name = body.get("model", MODEL_NAME)
    encoding_format = body.get("encoding_format", "float")

    # Embed — 请求中的 model 名决定用哪个 BGE 变体
    try:
        model = get_model(model_name)
        vectors = list(model.embed(texts))
    except Exception as e:
        log.error(f"Embedding failed: {e}")
        return JSONResponse(
            {"error": {"message": str(e), "type": "embedding_error"}},
            status_code=500,
        )

    # Format response
    data = [
        {
            "object": "embedding",
            "embedding": vec.tolist() if hasattr(vec, "tolist") else list(vec),
            "index": i,
        }
        for i, vec in enumerate(vectors)
    ]

    elapsed = (time.perf_counter() - t0) * 1000
    log.info(
        f"Embedded {len(texts)} texts | "
        f"{len(data[0]['embedding'])}d | "
        f"{elapsed:.1f}ms"
    )

    return JSONResponse({
        "object": "list",
        "data": data,
        "model": model_name,
        "usage": {
            "prompt_tokens": sum(len(t) for t in texts),
            "total_tokens": sum(len(t) for t in texts),
        },
    })


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    print(f"""
╔══════════════════════════════════════════════════╗
║   Vibry Embedding Service (local)                ║
╠══════════════════════════════════════════════════╣
║   Port:    {PORT:<36} ║
║   Default: {MODEL_NAME:<36} ║
║   Backend: fastembed (ONNX Runtime, CPU)         ║
╠══════════════════════════════════════════════════╣
║   Available models (pre-cached):                 ║""")
    for m in AVAILABLE_MODELS:
        marker = " ← default" if m == MODEL_NAME else ""
        short = m.replace("BAAI/", "")
        print(f"║     {short:<42} ║".replace("║", "║") + marker)
    print(f"""╠══════════════════════════════════════════════════╣
║   后台 → Embedding 模型 → Model Name 填:           ║
║   BAAI/bge-small-zh-v1.5                         ║
║   BAAI/bge-base-zh-v1.5                          ║
║   BAAI/bge-large-zh-v1.5                          ║
║   Base URL: http://127.0.0.1:{PORT}/v1           ║
║   API Key 留空即可                                 ║
╚══════════════════════════════════════════════════╝
    """)

    # 只加载默认模型 (不预下载其他模型到磁盘，按需切换)

    get_model(MODEL_NAME)

    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
