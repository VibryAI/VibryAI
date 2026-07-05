"""Vibry AI Core — OpenAI 兼容流式代理

职责:
1. 接收标准 OpenAI /v1/chat/completions 请求
2. 提取 user message → 检索 Mem0 记忆
3. 注入记忆到 system prompt
4. 转发到上游 LLM
5. 流式回传 SSE chunks
"""

import json
import logging
import time
from typing import AsyncGenerator

import httpx

from config import config
from memory_engine import search_memories, format_memories_for_prompt

log = logging.getLogger("vibry.proxy")

# ---------------------------------------------------------------------------
# HTTP 客户端（连接池复用）
# ---------------------------------------------------------------------------

_http_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(config.upstream.timeout),
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=50),
        )
    return _http_client


# ---------------------------------------------------------------------------
# 请求处理
# ---------------------------------------------------------------------------

def extract_user_message(messages: list[dict]) -> tuple[str | None, int | None]:
    """从消息列表中提取最后一条 user 消息

    Returns:
        (content, index) — content 为 None 表示未找到
    """
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                # 多模态：提取 text 部分
                text_parts = [p.get("text", "") for p in content if p.get("type") == "text"]
                content = " ".join(text_parts)
            return content, i
    return None, None


def inject_memories_into_messages(
    messages: list[dict],
    memory_text: str,
) -> list[dict]:
    """将记忆注入到消息列表的 system prompt 中

    策略:
    - 如果已有 system 消息 → 在开头插入记忆
    - 如果没有 system 消息 → 新建一条 system 消息
    - 不修改 user/assistant 消息

    Args:
        messages: 原始消息列表 (会被浅拷贝修改)
        memory_text: 格式化后的记忆文本

    Returns:
        修改后的消息列表
    """
    if not memory_text:
        return messages

    modified = [dict(m) for m in messages]  # 浅拷贝

    # 找到第一条 system 消息
    system_idx = None
    for i, msg in enumerate(modified):
        if msg.get("role") == "system":
            system_idx = i
            break

    if system_idx is not None:
        # 在现有 system content 前面插入记忆
        original = modified[system_idx].get("content", "")
        modified[system_idx]["content"] = f"{memory_text}\n{original}"
    else:
        # 新建 system 消息，插入到消息列表最前面
        modified.insert(0, {"role": "system", "content": memory_text})

    return modified


# ---------------------------------------------------------------------------
# 上游调用
# ---------------------------------------------------------------------------

def build_upstream_payload(
    modified_messages: list[dict],
    original_payload: dict,
) -> dict:
    """构建发送给上游 LLM 的请求体

    保留原始请求中的 model, temperature, max_tokens 等参数，
    但用配置中的 model 覆盖（如果配置了的话）。
    """
    upstream = config.upstream

    payload = {
        "model": upstream.model or original_payload.get("model", "gpt-3.5-turbo"),
        "messages": modified_messages,
    }

    # 透传可选参数
    for key in ("temperature", "top_p", "max_tokens", "n", "stop",
                "presence_penalty", "frequency_penalty", "stream"):
        if key in original_payload:
            payload[key] = original_payload[key]

    # 强制启用 stream（如果原始请求要求）
    if original_payload.get("stream", False):
        payload["stream"] = True

    return payload


async def stream_to_upstream(
    payload: dict,
) -> AsyncGenerator[bytes, None]:
    """流式请求上游 LLM，yield 原始 bytes chunks"""
    upstream = config.upstream
    url = f"{upstream.base_url.rstrip('/')}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {upstream.api_key}",
    }

    client = get_http_client()
    t0 = time.time()
    total_bytes = 0

    log.info(f"🔄 代理请求 → {upstream.model} | stream=True | messages={len(payload['messages'])}条")

    try:
        async with client.stream("POST", url, json=payload, headers=headers) as resp:
            if resp.status_code != 200:
                # 非流式错误 — 读取 body 并作为 SSE error 返回
                error_body = await resp.aread()
                log.error(f"❌ 上游错误 {resp.status_code}: {error_body.decode()[:300]}")
                error_chunk = {
                    "error": {
                        "message": f"Upstream error: {resp.status_code}",
                        "type": "upstream_error",
                        "code": resp.status_code,
                    }
                }
                yield f"data: {json.dumps(error_chunk, ensure_ascii=False)}\n\n".encode()
                yield b"data: [DONE]\n\n"
                return

            async for chunk in resp.aiter_bytes():
                total_bytes += len(chunk)
                yield chunk

        elapsed = (time.time() - t0) * 1000
        log.info(f"✅ 流式完成 | {total_bytes} bytes | {elapsed:.0f}ms")

    except httpx.TimeoutException:
        log.error(f"⏰ 上游超时 ({config.upstream.timeout}s)")
        yield f"data: {json.dumps({'error': {'message': 'Upstream timeout', 'type': 'timeout'}})}\n\n".encode()
        yield b"data: [DONE]\n\n"
    except Exception as e:
        log.error(f"❌ 代理异常: {e}")
        yield f"data: {json.dumps({'error': {'message': str(e), 'type': 'proxy_error'}})}\n\n".encode()
        yield b"data: [DONE]\n\n"


async def proxy_non_streaming(payload: dict) -> dict:
    """非流式代理请求"""
    upstream = config.upstream
    url = f"{upstream.base_url.rstrip('/')}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {upstream.api_key}",
    }

    client = get_http_client()
    t0 = time.time()

    log.info(f"🔄 非流式请求 → {upstream.model} | messages={len(payload['messages'])}条")

    resp = await client.post(url, json=payload, headers=headers)
    elapsed = (time.time() - t0) * 1000

    if resp.status_code != 200:
        log.error(f"❌ 上游错误 {resp.status_code}: {resp.text[:300]}")
        return {
            "error": {
                "message": f"Upstream error: {resp.status_code}",
                "type": "upstream_error",
                "code": resp.status_code,
            }
        }

    data = resp.json()
    log.info(f"✅ 非流式完成 | {elapsed:.0f}ms | tokens={data.get('usage', {})}")
    return data
