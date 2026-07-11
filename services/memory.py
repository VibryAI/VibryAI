"""Vibry AI Core — Mem0 长时记忆引擎

优先使用 Mem0 v2 语义检索（Qdrant + 火山引擎多模态 Embedding）。
如果 Mem0 初始化失败，自动降级为关键词匹配引擎。

对外接口:
- add_memory(text, user_id, metadata) → list[dict]
- search_memories(query, user_id, top_k, threshold) → list[dict]
- format_memories_for_prompt(memories) → str
"""

import logging
from typing import Optional

from app.config import config

log = logging.getLogger("vibry.memory")

# ---------------------------------------------------------------------------
# 引擎选择
# ---------------------------------------------------------------------------
_engine = None  # "mem0" | "simple"


def _init_engine():
    """尝试初始化 Mem0，失败则回退到关键词匹配"""
    global _engine

    if _engine is not None:
        return _engine

    try:
        from mem0 import Memory
        from mem0.configs.base import MemoryConfig
        from services.embedder import VolcengineEmbedder

        llm_cfg = config.upstream
        mem_cfg = config.memory

        log.info("🧠 初始化 Mem0 语义记忆引擎...")
        log.info(f"   Embedding: {llm_cfg.embedding_model} (直连火山引擎)")
        log.info(f"   向量库: Qdrant 本地 ({mem_cfg.qdrant_path})")

        # 自定义 embedder — 直接调火山引擎 multimodal API
        embedder = VolcengineEmbedder(
            model=llm_cfg.embedding_model,
            api_key=llm_cfg.api_key,
            base_url=llm_cfg.base_url,
        )

        # 构建 config — embedding_dims 确保 collection 用正确维度
        dims = mem_cfg.embedding_dims
        mem0_config = MemoryConfig(
            llm={
                "provider": "openai",
                "config": {
                    "model": llm_cfg.model,
                    "api_key": llm_cfg.api_key,
                    "openai_base_url": llm_cfg.base_url.rstrip("/") + "/",
                },
            },
            embedder={
                "provider": "openai",
                "config": {
                    "model": llm_cfg.embedding_model,
                    "api_key": llm_cfg.api_key,
                    "openai_base_url": llm_cfg.base_url.rstrip("/") + "/",
                    "embedding_dims": dims,
                },
            },
            vector_store={
                "provider": "qdrant",
                "config": {
                    "collection_name": mem_cfg.collection,
                    "path": mem_cfg.qdrant_path,
                    "host": "localhost",
                    "embedding_model_dims": dims,
                },
            },
        )

        client = Memory(mem0_config)
        # 替换 embedder 为直连火山引擎多模态版本
        client.embedding_model = embedder

        # 连接测试
        try:
            client.search("test", filters={"user_id": "__vibry_probe__"}, limit=1)
        except Exception as probe_err:
            log.warning(f"⚠️ Mem0 连接测试失败 ({probe_err})，降级")
            raise probe_err

        global _mem0_client
        _mem0_client = client
        _engine = "mem0"
        log.info("✅ Mem0 语义记忆引擎就绪")
        return _engine

    except Exception as e:
        log.warning(f"⚠️ Mem0 不可用 ({e})，降级为关键词匹配引擎")
        _engine = "simple"
        return _engine


# ---------------------------------------------------------------------------
# Mem0 实现
# ---------------------------------------------------------------------------
_mem0_client: Optional["Memory"] = None


def _get_mem0():
    global _mem0_client
    if _mem0_client is not None:
        return _mem0_client
    _init_engine()
    return _mem0_client


def _add_mem0(text: str, user_id: str, metadata: dict | None = None) -> list[dict]:
    m = _get_mem0()
    if m is None:
        raise RuntimeError("Mem0 not initialized")
    result = m.add(text, user_id=user_id, metadata=metadata or {})
    log.info(f"💾 [mem0] 记忆已写入 [user={user_id}]: {text[:80]}...")
    return result


def _search_mem0(query: str, user_id: str, top_k: int, threshold: float) -> list[dict]:
    m = _get_mem0()
    if m is None:
        return []
    results = m.search(query, filters={"user_id": user_id}, limit=top_k)
    # Mem0 v2 返回 list[dict] 或 list[str]，统一处理
    formatted = []
    for r in results:
        if isinstance(r, dict):
            score = r.get("score", 0.5)
            text = r.get("memory", "")
        else:
            score = 0.5  # Mem0 v2 字符串结果默认分数
            text = str(r)
        if score >= threshold:
            formatted.append({"memory": text, "score": score})
    if formatted:
        scores = ", ".join(f"{r['score']:.2f}" for r in formatted)
        log.info(f"🔍 [mem0] 检索记忆 [user={user_id}]: {len(formatted)}条 (scores: {scores})")
    else:
        log.info(f"🔍 [mem0] 检索记忆 [user={user_id}]: 无匹配")
    return formatted


# ---------------------------------------------------------------------------
# 简单关键词匹配实现 (降级方案)
# ---------------------------------------------------------------------------
_simple_store: dict[str, list[dict]] = {}
_simple_counter: int = 0


def _add_simple(text: str, user_id: str, metadata: dict | None = None) -> list[dict]:
    global _simple_counter
    _simple_counter += 1
    mem = {
        "id": f"mem_{_simple_counter}",
        "memory": text,
        "metadata": metadata or {},
        "created_at": __import__("datetime").datetime.now().isoformat(),
    }
    _simple_store.setdefault(user_id, []).append(mem)
    log.info(f"💾 [simple] 记忆已写入 [user={user_id}]: {text[:80]}...")
    return [mem]


def _search_simple(query: str, user_id: str, top_k: int, threshold: float) -> list[dict]:
    user_mems = _simple_store.get(user_id, [])
    if not user_mems:
        return []

    query_lower = query.lower()
    query_words = set(query_lower.split())
    scored = []

    for mem in user_mems:
        text_lower = mem["memory"].lower()
        score = 0.0
        if query_lower in text_lower:
            score = 0.8
        else:
            text_words = set(text_lower.split())
            common = query_words & text_words
            if common:
                score = min(0.6, len(common) / max(len(query_words), 1) * 0.6)
        if score >= threshold:
            scored.append({**mem, "score": score})

    scored.sort(key=lambda x: x["score"], reverse=True)
    results = scored[:top_k]

    if results:
        scores = ", ".join(f"{r['score']:.2f}" for r in results)
        log.info(f"🔍 [simple] 检索记忆 [user={user_id}]: {len(results)}条 (scores: {scores})")
    else:
        log.info(f"🔍 [simple] 检索记忆 [user={user_id}]: 无匹配")
    return results


# ---------------------------------------------------------------------------
# 公共接口
# ---------------------------------------------------------------------------

def add_memory(
    text: str,
    user_id: str,
    *,
    metadata: Optional[dict] = None,
) -> list[dict]:
    _init_engine()
    if _engine == "mem0":
        return _add_mem0(text, user_id, metadata)
    return _add_simple(text, user_id, metadata)


def search_memories(
    query: str,
    user_id: str,
    *,
    top_k: Optional[int] = None,
    threshold: Optional[float] = None,
) -> list[dict]:
    _init_engine()
    mem_cfg = config.memory
    top_k = top_k or mem_cfg.top_k
    threshold = threshold or mem_cfg.threshold

    if _engine == "mem0":
        return _search_mem0(query, user_id, top_k, threshold)
    return _search_simple(query, user_id, top_k, threshold)


def format_memories_for_prompt(memories: list[dict]) -> str:
    if not memories:
        return ""

    lines = ["## 🧠 长时记忆 (Vibry AI)", ""]
    lines.append("以下是从你的历史记录中检索到的相关信息：")
    lines.append("")

    for i, mem in enumerate(memories, 1):
        text = mem.get("memory", "")
        score = mem.get("score", 0)
        lines.append(f"{i}. {text}  `[相关度: {score:.0%}]`")

    lines.append("")
    lines.append("---")
    lines.append("请基于以上记忆上下文回答用户的问题。如果记忆与当前问题无关，请忽略。")
    lines.append("")

    return "\n".join(lines)


def get_mem0():
    """获取 Mem0 客户端（用于健康检查）"""
    _init_engine()
    if _engine == "mem0":
        return _get_mem0()
    return None
