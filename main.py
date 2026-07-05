"""Vibry AI Core — FastAPI Memory Proxy Server

统一的「数字前额叶」记忆中间层 + AI 分析后端：
- OpenAI 兼容 API (/v1/chat/completions) → 记忆注入 + 流式代理
- 语音转文字 (/api/transcribe) → FunASR 本地 / Doubao 云端
- 会议纪要 (/api/summarize) → 结构化摘要 + 数据库持久化
- 录音管理 (/api/recordings) → CRUD + 标签 + 统计
- 用户隔离（Bearer token = user_id）

启动: python main.py
端口: 9999
"""

import asyncio
import base64
import sys

# ★ Windows 强制 UTF-8 输出（解决日志中文乱码）
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
import hashlib
import hmac
import os
import secrets
from pathlib import Path
import json
import logging
import re
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException, UploadFile, File, Form
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from config import config
from memory_engine import (
    get_mem0,
    add_memory,
    search_memories,
    format_memories_for_prompt,
)
from proxy import (
    extract_user_message,
    inject_memories_into_messages,
    build_upstream_payload,
    stream_to_upstream,
    proxy_non_streaming,
)

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
LOG_FILE = Path(__file__).parent / "server_output.log"
logging.basicConfig(
    level=getattr(logging, config.server.log_level.upper(), logging.INFO),
    format="[%(asctime)s] %(levelname)-5s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("vibry")

# ---------------------------------------------------------------------------
# ASR / 摘要队列锁（防止并发调用 GPU 模型 OOM）
# ---------------------------------------------------------------------------
_asr_lock = asyncio.Lock()
_summary_lock = asyncio.Lock()
_asr_queue = 0
_summary_queue = 0

# ---------------------------------------------------------------------------
# 应用生命周期
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动/关闭钩子"""
    # 初始化数据库
    import db
    db.init_db()
    log.info("🗄️ SQLite 数据库已初始化")

    log.info("=" * 55)
    log.info("🧠 Vibry AI Core — 数字前额叶记忆代理 + AI 分析后端")
    log.info(f"   上游模型: {config.upstream.model}")
    log.info(f"   ASR 模式: {config.asr.mode}")
    log.info(f"   记忆引擎: Mem0 ({config.memory.vector_store})")
    log.info(f"   监听: http://{config.server.host}:{config.server.port}")
    log.info(f"   使用方式: Base URL = http://localhost:{config.server.port}/v1")
    log.info("=" * 55)
    yield
    from proxy import _http_client
    if _http_client:
        await _http_client.aclose()
    log.info("👋 Vibry AI Core 已关闭")

app = FastAPI(
    title="Vibry AI Core",
    description="即插即用的数字前额叶 — OpenAI 兼容记忆代理 + ASR + 纪要",
    version="0.2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def get_user_id(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return "anonymous"


# ═══════════════════════════════════════════════════════════════════════════
# OpenAI 兼容端点
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [
            {
                "id": config.upstream.model,
                "object": "model",
                "created": 0,
                "owned_by": "vibry-ai",
            },
            {
                "id": config.upstream.embedding_model,
                "object": "model",
                "created": 0,
                "owned_by": "vibry-ai",
            },
        ],
    }


@app.post("/v1/embeddings")
async def embeddings_proxy(request: Request):
    """Embedding 代理 — 标准 OpenAI 格式 → 火山引擎多模态格式

    Mem0 调用此端点获得向量，内部翻译为火山引擎 multimodal 格式。
    """
    import httpx

    body = await request.json()
    model = body.get("model", config.upstream.embedding_model)
    raw_input = body.get("input", "")

    # 翻译 input: 标准格式 → 多模态格式
    if isinstance(raw_input, str):
        multimodal_input = [{"type": "text", "text": raw_input}]
    elif isinstance(raw_input, list):
        if raw_input and isinstance(raw_input[0], str):
            multimodal_input = [{"type": "text", "text": t} for t in raw_input]
        else:
            multimodal_input = raw_input  # 已经是多模态格式
    else:
        multimodal_input = [{"type": "text", "text": str(raw_input)}]

    upstream_payload = {
        "model": model,
        "input": multimodal_input,
    }
    if "encoding_format" in body:
        upstream_payload["encoding_format"] = body["encoding_format"]
    if "dimensions" in body:
        upstream_payload["dimensions"] = body["dimensions"]

    multimodal_url = f"{config.upstream.base_url.rstrip('/')}/embeddings/multimodal"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config.upstream.api_key}",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(multimodal_url, json=upstream_payload, headers=headers)

    if resp.status_code != 200:
        log.error(f"❌ Embedding 上游错误: {resp.text[:300]}")
        raise HTTPException(status_code=502, detail=f"Embedding upstream error: {resp.status_code}")

    upstream_data = resp.json()

    # 翻译返回: 多模态格式 → 标准 OpenAI 格式
    emb_data = upstream_data.get("data", {})
    if isinstance(emb_data, dict):
        # 单条 → 包装为数组
        embedding_vector = emb_data.get("embedding", [])
        standard_data = [{"object": "embedding", "embedding": embedding_vector, "index": 0}]
    elif isinstance(emb_data, list):
        standard_data = [
            {"object": "embedding", "embedding": item.get("embedding", []), "index": i}
            for i, item in enumerate(emb_data)
        ]
    else:
        standard_data = []

    usage = upstream_data.get("usage", {})

    # 计费记录
    tok = usage.get("total_tokens", usage.get("prompt_tokens", 0))
    if tok:
        import db
        db.log_usage("system", "/v1/embeddings", model,
            prompt_tokens=tok, total_tokens=tok,
            duration_ms=int((time.time() - time.time()) * 1000))  # approximate

    return JSONResponse({
        "object": "list",
        "data": standard_data,
        "model": model,
        "usage": usage,
    })


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    t0 = time.time()
    user_id = get_user_id(request)

    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    messages = body.get("messages", [])
    if not messages:
        raise HTTPException(status_code=400, detail="messages is required")

    is_stream = body.get("stream", False)

    user_msg, _ = extract_user_message(messages)
    log.info(f"📩 [user={user_id}] query: {user_msg[:100] if user_msg else '(空)'}... | stream={is_stream}")

    # 检索记忆
    memory_text = ""
    memories_found = []
    if user_msg:
        try:
            memories_found = search_memories(user_msg, user_id=user_id)
            memory_text = format_memories_for_prompt(memories_found)
        except Exception as e:
            log.warning(f"⚠️ 记忆检索失败 (降级): {e}")

    # 注入 Personality + 记忆
    import db
    personality = db.get_personality()
    if personality:
        personality_block = f"## 🎭 人格设定 (Vibry AI)\n{personality}\n\n---\n"
        if memory_text:
            memory_text = personality_block + memory_text
        else:
            memory_text = personality_block

    modified_messages = inject_memories_into_messages(messages, memory_text)
    upstream_payload = build_upstream_payload(modified_messages, body)

    # 保存用户消息到数据库
    if user_msg:
        db.save_chat_message(user_id, "user", user_msg, conversation_id=user_id, model=upstream_payload.get("model", ""))

    if is_stream:
        async def sse_generator():
            async for chunk in stream_to_upstream(upstream_payload):
                yield chunk
        elapsed = (time.time() - t0) * 1000
        log.info(f"⚡ [user={user_id}] 流式开始 (前处理 {elapsed:.0f}ms, 记忆 {len(memory_text)}chars)")
        return StreamingResponse(
            sse_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
                "X-Vibry-Memories": str(len(memory_text)),
            },
        )
    else:
        result = await proxy_non_streaming(upstream_payload)
        elapsed = (time.time() - t0) * 1000

        # 保存 assistant 回复
        assistant_text = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        usage = result.get("usage", {})
        if assistant_text:
            import db
            db.save_chat_message(user_id, "assistant", assistant_text, conversation_id=user_id,
                model=upstream_payload.get("model", ""), tokens=usage.get("total_tokens", 0))

        # 计费记录
        if usage:
            import db
            db.log_usage(user_id, "/v1/chat/completions", upstream_payload.get("model", ""),
                prompt_tokens=usage.get("prompt_tokens", 0), completion_tokens=usage.get("completion_tokens", 0),
                total_tokens=usage.get("total_tokens", 0), duration_ms=int(elapsed))

        # 附加记忆信息到响应
        result["_vibry_memories"] = {
            "count": len(memories_found),
            "items": [{"memory": m.get("memory", ""), "score": m.get("score", 0)} for m in memories_found[:3]],
        }

        log.info(f"⚡ [user={user_id}] 非流式完成 ({elapsed:.0f}ms)")
        return JSONResponse(content=result)


# ═══════════════════════════════════════════════════════════════════════════
# ASR 语音转文字
# ═══════════════════════════════════════════════════════════════════════════

@app.post("/api/transcribe")
async def api_transcribe(request: Request):
    """语音转文字 — 支持 JSON base64 和 multipart 两种方式

    JSON:  {"audio_base64": "...", "title": "录音文件名"}
    Form:  audio=@file.wav, title=录音文件名
    """
    global _asr_queue
    _asr_queue += 1
    qpos = _asr_queue
    user_id = get_user_id(request)
    t0 = time.time()

    content_type = request.headers.get("content-type", "")

    # ---- 解析请求 ----
    if "application/json" in content_type:
        data = await request.json()
        audio_b64 = data.get("audio_base64", "")
        title = data.get("title", "")
        if not audio_b64:
            _asr_queue -= 1
            raise HTTPException(status_code=400, detail="缺少 audio_base64")
        audio_bytes = base64.b64decode(audio_b64)

    elif "multipart" in content_type:
        form = await request.form()
        audio_file = form.get("audio")
        if audio_file is None:
            _asr_queue -= 1
            raise HTTPException(status_code=400, detail="缺少 audio 字段")
        audio_bytes = await audio_file.read()
        title = form.get("title", "")
    else:
        _asr_queue -= 1
        raise HTTPException(status_code=400, detail="需要 application/json 或 multipart/form-data")

    size_kb = len(audio_bytes) / 1024
    if qpos > 1:
        log.info(f"🎤 ASR排队 #{qpos} | {size_kb:.1f}KB | 等待...")

    # ---- 串行锁 ----
    async with _asr_lock:
        _asr_queue -= 1
        log.info(f"🎤 ASR开始 #{qpos} [user={user_id}] | {size_kb:.1f}KB | mode={config.asr.mode}")

        # 在线程池中执行 CPU 密集型 ASR
        from asr_engine import transcribe
        result = await asyncio.to_thread(transcribe, audio_bytes, title, user_id)

    elapsed = (time.time() - t0) * 1000
    if result.get("error"):
        # 422 = 未识别到有效语音（与 Flask 版本兼容）
        status = 422 if "未识别" in str(result.get("error", "")) else 500
        log.error(f"❌ ASR失败 ({elapsed:.0f}ms): {result.get('error')}")
        return JSONResponse(result, status_code=status)
    log.info(f"✅ ASR完成 ({elapsed:.0f}ms) | {len(result['text'])}字符")
    return JSONResponse({
        "text": result["text"],
        "audio_url": result.get("audio_url"),
        "audio_token": result.get("audio_token"),
    })


# ═══════════════════════════════════════════════════════════════════════════
# 会议纪要
# ═══════════════════════════════════════════════════════════════════════════

@app.post("/api/summarize")
async def api_summarize(request: Request):
    """生成会议纪要

    POST /api/summarize
    {
        "transcript": "转写文本",
        "title": "录音标题",
        "context": "额外上下文"
    }
    """
    global _summary_queue
    _summary_queue += 1
    qpos = _summary_queue
    user_id = get_user_id(request)
    t0 = time.time()

    data = await request.json()
    transcript = data.get("transcript", "")
    title = data.get("title", "录音")
    context = data.get("context", "")

    if not transcript:
        _summary_queue -= 1
        raise HTTPException(status_code=400, detail="transcript 不能为空")

    char_count = len(transcript)
    if qpos > 1:
        log.info(f"🧠 纪要排队 #{qpos} | {title} | {char_count}字符...")

    async with _summary_lock:
        _summary_queue -= 1
        log.info(f"🧠 纪要开始 #{qpos} [user={user_id}] | {title} | {char_count}字符")

        from asr_engine import summarize
        result = await asyncio.to_thread(summarize, transcript, title, context, user_id)

    elapsed = (time.time() - t0) * 1000
    if "error" in result:
        log.error(f"❌ 纪要失败 ({elapsed:.0f}ms): {result['error']}")
        return JSONResponse({"error": result["error"]}, status_code=500)
    log.info(f"✅ 纪要完成 ({elapsed:.0f}ms) | {len(result.get('key_decisions', []))}决策")
    return JSONResponse(result)


# ═══════════════════════════════════════════════════════════════════════════
# 洞察 API — 基于长期记忆的商业咨询视角
# ═══════════════════════════════════════════════════════════════════════════

def _parse_insight(raw: str) -> dict:
    """从 LLM 返回中提取洞察 JSON"""
    json_str = raw
    match = re.search(r'\{[\s\S]*\}', raw)
    if match:
        json_str = match.group()
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        return {"core_insight": "", "analysis": {"opportunity": "", "risk": ""}, "action_suggestions": []}


@app.post("/api/insight")
async def api_insight(request: Request):
    """AI 洞察 — 商业咨询视角的战略分析

    POST /api/insight
    {
        "transcript": "转写文本",
        "record_title": "录音标题",
        "context": "额外上下文"
    }
    """
    global _summary_queue
    _summary_queue += 1
    qpos = _summary_queue
    user_id = get_user_id(request)
    t0 = time.time()

    data = await request.json()
    transcript = data.get("transcript", "")
    title = data.get("record_title", "录音")
    context = data.get("context", "")

    if not transcript:
        _summary_queue -= 1
        raise HTTPException(status_code=400, detail="transcript 不能为空")

    char_count = len(transcript)
    if qpos > 1:
        log.info(f"💡 洞察排队 #{qpos} | {title} | {char_count}字符")

    # 构建 insight prompt
    insight_prompt = config.prompt.insight_prompt if hasattr(config, 'prompt') and config.prompt.insight_prompt else ""
    if not insight_prompt:
        # 回退：用 summary prompt 替代
        insight_prompt = config.summary.system_prompt
    insight_prompt = insight_prompt.replace("{name}", config.summary.user_name)
    insight_prompt = insight_prompt.replace("{role}", config.summary.user_role)
    insight_prompt = insight_prompt.replace("{context}", config.summary.user_context)

    async with _summary_lock:
        _summary_queue -= 1
        log.info(f"💡 洞察开始 #{qpos} [user={user_id}] | {title} | {char_count}字符")

        messages = [
            {"role": "system", "content": insight_prompt},
            {"role": "user", "content": f"录音标题：{title}\n\n转写内容：\n{transcript}\n\n额外上下文：{context}"},
        ]

        from asr_engine import call_llm
        model = config.summary.model or config.upstream.model
        api_t0 = time.time()
        result = call_llm(model, messages, max_time=180)
        api_time = time.time() - api_t0

        if "error" in result:
            elapsed = (time.time() - t0) * 1000
            log.error(f"❌ 洞察失败 ({elapsed:.0f}ms): {result['error']}")
            return JSONResponse({"error": str(result["error"])}, status_code=500)

        raw = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        parsed = _parse_insight(raw)

        elapsed = (time.time() - t0) * 1000
        suggestions = len(parsed.get("action_suggestions", []))
        log.info(f"✅ 洞察完成 | API{api_time:.1f}s | 建议{suggestions}条 | 总{elapsed:.0f}ms")

        return JSONResponse(parsed)
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/asr-mode")
async def get_asr_mode():
    return JSONResponse({"asr_mode": config.asr.mode})


@app.post("/api/asr-mode")
async def set_asr_mode(request: Request):
    data = await request.json()
    mode = data.get("mode", config.asr.mode)
    if mode in ("local", "cloud"):
        config.asr.mode = mode
        log.info(f"🔄 ASR 模式切换: {mode}")
    return JSONResponse({"asr_mode": config.asr.mode})


# ═══════════════════════════════════════════════════════════════════════════
# 录音记录 CRUD
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/recordings")
async def list_recordings(
    request: Request,
    status: str = None,
    limit: int = 50,
    offset: int = 0,
):
    """列出录音记录"""
    import db
    user_id = get_user_id(request)
    recordings = db.list_recordings(status=status, user_id=user_id, limit=limit, offset=offset)
    stats = db.get_stats(user_id=user_id)
    return JSONResponse({"recordings": recordings, "stats": stats})


@app.get("/api/recordings/{rec_id}")
async def get_recording(request: Request, rec_id: str):
    """获取单个录音详情"""
    import db
    rec = db.get_recording(rec_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    logs_data = db.get_analysis_log(rec_id)
    rec["analysis_log"] = logs_data
    return JSONResponse(rec)


@app.delete("/api/recordings/{rec_id}")
async def delete_recording(rec_id: str):
    """删除录音记录"""
    import db
    db.delete_recording(rec_id)
    log.info(f"🗑️ 删除记录: {rec_id}")
    return JSONResponse({"ok": True})


@app.patch("/api/recordings/{rec_id}/tags")
async def update_recording_tags(request: Request, rec_id: str):
    """更新标签和分类"""
    import db
    data = await request.json()
    tags = data.get("tags", [])
    category = data.get("category")
    rec = db.update_tags(rec_id, tags, category)
    if rec is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    log.info(f"🏷️ 更新标签: {rec_id} → {tags} [{category}]")
    return JSONResponse(rec)


# ═══════════════════════════════════════════════════════════════════════════
# 音频播放（带 token 鉴权 + Range 支持）
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/audio/{rec_id}")
async def serve_audio(request: Request, rec_id: str):
    """流式返回清晰化后的 WAV（带 token 鉴权，支持 Range 请求）"""
    import db
    token = request.query_params.get("token", "")
    info = db.get_audio_info(rec_id)
    if info is None:
        raise HTTPException(status_code=404, detail="录音不存在")
    if not info["audio_token"] or token != info["audio_token"]:
        raise HTTPException(status_code=403, detail="token 无效")
    if not info["audio_path"]:
        raise HTTPException(status_code=404, detail="音频未就绪")

    audio_dir = config.audio.audio_dir if hasattr(config, 'audio') else "audio"
    filepath = os.path.join(audio_dir, info["audio_path"])
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="音频文件丢失")

    from fastapi.responses import FileResponse
    # FileResponse 天然支持 Range/206，just_audio 流式播放+seek 需要
    return FileResponse(filepath, media_type="audio/wav")


# ═══════════════════════════════════════════════════════════════════════════
# 统计
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/stats")
async def get_stats(request: Request):
    import db
    user_id = get_user_id(request)
    return JSONResponse(db.get_stats(user_id=user_id))


# ═══════════════════════════════════════════════════════════════════════════
# 记忆管理
# ═══════════════════════════════════════════════════════════════════════════

@app.post("/api/memories")
async def api_add_memory(request: Request):
    user_id = get_user_id(request)
    body = await request.json()
    text = body.get("text", "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    metadata = body.get("metadata", {})
    try:
        result = add_memory(text, user_id=user_id, metadata=metadata)
    except Exception as e:
        log.error(f"❌ 记忆写入失败: {e}")
        raise HTTPException(status_code=500, detail=f"记忆写入失败: {e}")
    return JSONResponse({"ok": True, "user_id": user_id, "result": result})


@app.get("/api/memories")
async def api_search_memories(request: Request, q: str = "", top_k: int = 10):
    user_id = get_user_id(request)
    if not q.strip():
        raise HTTPException(status_code=400, detail="query parameter 'q' is required")
    try:
        results = search_memories(q, user_id=user_id, top_k=top_k, threshold=0.0)
    except Exception as e:
        log.error(f"❌ 记忆检索失败: {e}")
        raise HTTPException(status_code=500, detail=f"记忆检索失败: {e}")
    return JSONResponse({"user_id": user_id, "query": q, "count": len(results), "memories": results})


# ═══════════════════════════════════════════════════════════════════════════
# 健康检查
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/health")
async def health():
    try:
        _ = get_mem0()
        mem0_status = "ok"
    except Exception as e:
        mem0_status = f"unavailable: {e}"

    return JSONResponse({
        "status": "ok",
        "version": "0.2.0",
        "server": f"http://{config.server.host}:{config.server.port}",
        "upstream": config.upstream.model,
        "asr_mode": config.asr.mode,
        "mem0": mem0_status,
        "memory_config": {
            "top_k": config.memory.top_k,
            "threshold": config.memory.threshold,
            "vector_store": config.memory.vector_store,
        },
        "queue": {"asr": _asr_queue, "summary": _summary_queue},
    })


# ═══════════════════════════════════════════════════════════════════════════
# 密码工具
# ═══════════════════════════════════════════════════════════════════════════

def _hash_password(password: str) -> str:
    """PBKDF2-SHA256 密码哈希"""
    salt = b"vibry_salt_2024"
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000).hex()


def _verify_password(password: str, stored_hash: str) -> bool:
    return _hash_password(password) == stored_hash


def _get_admin_password_hash() -> str:
    """获取管理员密码哈希（DB 优先，否则用环境变量初始化）"""
    import db
    admin = db.get_admin()
    pw_hash = admin.get("password_hash", "")
    if not pw_hash:
        # 首次初始化：用环境变量密码写入 DB
        env_pw = os.getenv("ADMIN_PASSWORD", "vibry2024")
        pw_hash = _hash_password(env_pw)
        db.set_admin_password(pw_hash)
        log.info("🔐 管理员密码已初始化到数据库")
    return pw_hash


# ═══════════════════════════════════════════════════════════════════════════
# Admin 认证 (HMAC 签名 token)
# ═══════════════════════════════════════════════════════════════════════════

_admin_signing_key = hashlib.sha256(os.getenv("ADMIN_PASSWORD", "vibry2024").encode()).digest()

def _make_admin_token() -> str:
    """生成 HMAC 签名 token: payload.expiry.signature"""
    expiry = str(int(time.time()) + 86400)  # 24h
    payload = f"admin.{expiry}"
    sig = hmac.new(_admin_signing_key, payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{payload}.{sig}"

def _verify_admin_token(token: str) -> bool:
    """验证 HMAC token"""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return False
        payload = f"{parts[0]}.{parts[1]}"
        expiry = int(parts[1])
        if time.time() > expiry:
            return False
        expected_sig = hmac.new(_admin_signing_key, payload.encode(), hashlib.sha256).hexdigest()[:32]
        return hmac.compare_digest(parts[2], expected_sig)
    except (ValueError, IndexError):
        return False

def _check_admin(request: Request) -> bool:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    return _verify_admin_token(auth[7:])

def _require_admin(request: Request):
    if not _check_admin(request):
        raise HTTPException(status_code=401, detail="未授权：需要管理员登录")


# ═══════════════════════════════════════════════════════════════════════════
# Admin 管理面板 & API
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/admin")
@app.get("/admin/")
async def admin_panel():
    """管理控制台页面"""
    html_path = Path(__file__).parent / "admin_panel.html"
    if html_path.exists():
        from fastapi.responses import HTMLResponse
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    raise HTTPException(status_code=404, detail="admin_panel.html not found")


@app.post("/admin/api/login")
async def admin_login(request: Request):
    """管理员登录（密码或验证码）"""
    body = await request.json()
    pwd = body.get("password", "")
    code = body.get("code", "")

    import db
    # 验证码登录
    if code:
        if db.verify_and_clear_code(code):
            token = _make_admin_token()
            return JSONResponse({"ok": True, "token": token})
        return JSONResponse({"ok": False, "error": "验证码错误或已过期"}, status_code=401)

    # 密码登录
    stored_hash = _get_admin_password_hash()
    if not _verify_password(pwd, stored_hash):
        return JSONResponse({"ok": False, "error": "密码错误"}, status_code=401)
    token = _make_admin_token()
    return JSONResponse({"ok": True, "token": token})


@app.get("/admin/api/verify")
async def admin_verify(request: Request):
    _require_admin(request)
    return JSONResponse({"ok": True})


@app.get("/admin/api/stats")
async def admin_stats(request: Request):
    _require_admin(request)
    import db
    stats = db.get_stats()
    usage = db.get_usage_summary()
    return JSONResponse({
        "recordings": stats["total"],
        "completed": stats["completed"],
        "failed": stats["failed"],
        "total_calls": usage["total_calls"],
        "total_tokens": usage["total_tokens"],
        "total_cost": usage["total_cost_rmb"],
        "usage_by_user": db.get_usage_by_user(),
        "recent_usage": db.get_usage_recent(50),
        "memories_count": "N/A",
    })


@app.get("/admin/api/config")
async def admin_get_config(request: Request):
    _require_admin(request)
    return JSONResponse({
        "upstream_model": config.upstream.model,
        "embedding_model": config.upstream.embedding_model,
        "asr_mode": config.asr.mode,
        "memory_top_k": config.memory.top_k,
        "memory_threshold": config.memory.threshold,
        "server_host": config.server.host,
        "server_port": config.server.port,
    })


@app.post("/admin/api/config")
async def admin_set_config(request: Request):
    _require_admin(request)
    body = await request.json()
    changes = []
    for key, field in [
        ("upstream_model", "model"), ("embedding_model", "embedding_model"),
    ]:
        if key in body:
            setattr(config.upstream, field, body[key])
            changes.append(key)
    for key, field in [("asr_mode", "mode")]:
        if key in body:
            setattr(config.asr, field, body[key])
            changes.append(key)
    for key, field in [("memory_top_k", "top_k"), ("memory_threshold", "threshold")]:
        if key in body:
            setattr(config.memory, field, body[key])
            changes.append(key)
    log.info(f"⚙️ 配置已更新: {', '.join(changes)}")
    return JSONResponse({"ok": True, "changes": changes})


@app.get("/admin/api/billing")
async def admin_billing(request: Request):
    _require_admin(request)
    import db
    return JSONResponse({
        "summary": db.get_usage_summary(),
        "by_user": db.get_usage_by_user(),
        "recent": db.get_usage_recent(100),
    })


@app.get("/admin/api/logs")
async def admin_logs(request: Request, lines: int = 100):
    _require_admin(request)
    log_path = Path(__file__).parent / "server_output.log"
    if not log_path.exists():
        return JSONResponse({"lines": ["日志文件不存在"]})
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        all_lines = f.readlines()
    tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
    return JSONResponse({"lines": [l.rstrip() for l in tail]})


# ═══════════════════════════════════════════════════════════════════════════
# Admin: 账户管理 (修改密码 / 忘记密码 / 邮箱配置)
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/admin/api/admin-profile")
async def admin_profile(request: Request):
    _require_admin(request)
    import db
    admin = db.get_admin()
    return JSONResponse({
        "email": admin.get("email", ""),
        "has_email": bool(admin.get("email", "")),
    })


@app.post("/admin/api/change-password")
async def admin_change_password(request: Request):
    """修改密码：需要旧密码 + 新密码"""
    _require_admin(request)
    body = await request.json()
    old_pw = body.get("old_password", "")
    new_pw = body.get("new_password", "")
    if len(new_pw) < 4:
        raise HTTPException(status_code=400, detail="新密码至少4位")

    import db
    stored_hash = _get_admin_password_hash()
    if not _verify_password(old_pw, stored_hash):
        raise HTTPException(status_code=403, detail="旧密码错误")

    db.set_admin_password(_hash_password(new_pw))
    # 更新 signing key
    global _admin_signing_key
    _admin_signing_key = hashlib.sha256(new_pw.encode()).digest()
    log.info("🔐 管理员密码已更改")
    return JSONResponse({"ok": True})


@app.post("/admin/api/set-email")
async def admin_set_email(request: Request):
    """设置管理员邮箱"""
    _require_admin(request)
    body = await request.json()
    email = body.get("email", "").strip()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="邮箱格式不正确")
    import db
    db.set_admin_email(email)
    log.info(f"📧 管理员邮箱已设置: {email}")
    return JSONResponse({"ok": True, "email": email})


@app.get("/admin/api/email-config")
async def admin_get_email_config(request: Request):
    _require_admin(request)
    return JSONResponse({
        "smtp_host": os.getenv("SMTP_HOST", "smtp.qq.com"),
        "smtp_port": int(os.getenv("SMTP_PORT", "587")),
        "smtp_user": os.getenv("SMTP_USER", ""),
        "smtp_configured": bool(os.getenv("SMTP_USER", "") and os.getenv("SMTP_PASS", "")),
    })


@app.post("/admin/api/forgot-password")
async def admin_forgot_password(request: Request):
    """忘记密码：发送验证码到管理员邮箱"""
    body = await request.json()
    email = body.get("email", "").strip()

    import db
    admin = db.get_admin()
    admin_email = admin.get("email", "")
    if not admin_email:
        raise HTTPException(status_code=400, detail="管理员邮箱未设置，无法发送验证码")
    if email != admin_email:
        return JSONResponse({"ok": True, "sent": False, "hint": "如果邮箱匹配，验证码已发送"})

    # 生成6位验证码，5分钟有效
    code = secrets.token_hex(3)[:6].upper()
    from datetime import datetime, timedelta
    expiry = (datetime.now() + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    db.set_verification_code(code, expiry)

    from email_service import send_verification_code
    ok = send_verification_code(admin_email, code)
    return JSONResponse({"ok": True, "sent": ok, "hint": "如果邮箱匹配，验证码已发送" if ok else "邮件发送失败，请检查SMTP配置"})


@app.post("/admin/api/reset-password")
async def admin_reset_password(request: Request):
    """用验证码重置密码"""
    body = await request.json()
    code = (body.get("code", "")).upper().strip()
    new_pw = body.get("new_password", "")
    if len(new_pw) < 4:
        raise HTTPException(status_code=400, detail="新密码至少4位")

    import db
    if not db.verify_and_clear_code(code):
        raise HTTPException(status_code=403, detail="验证码错误或已过期")

    db.set_admin_password(_hash_password(new_pw))
    global _admin_signing_key
    _admin_signing_key = hashlib.sha256(new_pw.encode()).digest()
    log.info("🔐 密码已通过验证码重置")
    return JSONResponse({"ok": True})


# ═══════════════════════════════════════════════════════════════════════════
# Admin: Personality & Chat History
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/admin/api/personality")
async def admin_get_personality(request: Request):
    _require_admin(request)
    import db
    return JSONResponse({"prompt": db.get_personality()})


@app.post("/admin/api/personality")
async def admin_set_personality(request: Request):
    _require_admin(request)
    body = await request.json()
    prompt = body.get("prompt", "")
    if not prompt.strip():
        raise HTTPException(status_code=400, detail="prompt is required")
    import db
    db.set_personality(prompt)
    log.info("🎭 Personality 已更新")
    return JSONResponse({"ok": True})


@app.get("/admin/api/chat-history")
async def admin_chat_history(request: Request, user_id: str = "admin", limit: int = 50):
    _require_admin(request)
    import db
    messages = db.get_chat_history(user_id, conversation_id=user_id, limit=limit)
    conversations = db.get_chat_conversations(user_id)
    return JSONResponse({"messages": messages, "conversations": conversations})


# ═══════════════════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=config.server.host,
        port=config.server.port,
        log_level=config.server.log_level.lower(),
        reload=False,
    )
