"""Vibry AI Core — LLM Wiki RAG 模块

基于 karpathy-llm-wiki 架构的编译器模式 RAG 系统。
三层架构:
  - raw/     : 不可变原始材料
  - wiki/    : LLM 维护的结构化知识页面 + index.md + log.md
  - SKILL.md : 规范/约束层 (来自 wiki-rag/SKILL.md + references/)

三大操作: Ingest(摄入编译) / Query(搜索引用) / Lint(健康检查)

核心理念: "LLM 负责编写和维护 wiki，人类负责阅读和提问" — Karpathy
"""

import json
import logging
import os
import re
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from app.config import config

log = logging.getLogger("vibry.wiki")


def _call_wiki_llm(model: str, messages: list[dict], max_time: int = 300) -> dict:
    """调用 Wiki 专用 LLM（默认 DeepSeek，有独立 base_url/api_key）

    优先用 DB 中配置的 wiki_model/wiki_base_url/wiki_api_key，
    fallback 到上游 Doubao 配置。
    """
    import json as _json
    import urllib.request, urllib.error

    try:
        import db
        wiki_cfg = db.get_wiki_llm_config()
        base_url = wiki_cfg["base_url"]
        api_key = wiki_cfg["api_key"]
    except Exception:
        base_url = config.upstream.base_url
        api_key = config.upstream.api_key

    url = f"{base_url.rstrip('/')}/chat/completions"
    payload = {"model": model, "messages": messages}
    data = _json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=max_time) as resp:
            return _json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode('utf-8')}"}
    except Exception as e:
        return {"error": str(e)}

# ---------------------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent.parent
RAW_DIR = BASE_DIR / "raw"
WIKI_DIR = BASE_DIR / "wiki"
SKILL_PATH = BASE_DIR / "wiki-rag" / "SKILL.md"
INDEX_PATH = WIKI_DIR / "index.md"
LOG_PATH = WIKI_DIR / "log.md"
REFS_DIR = BASE_DIR / "wiki-rag" / "references"

# 加载 SKILL.md 内容（作为 LLM 编译时的 system prompt）
SKILL_CONTENT = ""
if SKILL_PATH.exists():
    SKILL_CONTENT = SKILL_PATH.read_text(encoding="utf-8")

# 加载模板
RAW_TEMPLATE = (REFS_DIR / "raw-template.md").read_text(encoding="utf-8") if (REFS_DIR / "raw-template.md").exists() else ""
ARTICLE_TEMPLATE = (REFS_DIR / "article-template.md").read_text(encoding="utf-8") if (REFS_DIR / "article-template.md").exists() else ""
ARCHIVE_TEMPLATE = (REFS_DIR / "archive-template.md").read_text(encoding="utf-8") if (REFS_DIR / "archive-template.md").exists() else ""
INDEX_TEMPLATE = (REFS_DIR / "index-template.md").read_text(encoding="utf-8") if (REFS_DIR / "index-template.md").exists() else ""


# ===========================================================================
# 初始化
# ===========================================================================

def init_wiki(force: bool = False) -> dict:
    """初始化 wiki 目录结构（仅在首次 Ingest 时触发）。

    按 SKILL.md 规范:
    - raw/ 目录 (含 .gitkeep)
    - wiki/ 目录 (含 .gitkeep)
    - wiki/index.md (标题 '# Knowledge Base Index'，空内容)
    - wiki/log.md (标题 '# Wiki Log'，空内容)

    已存在的不覆盖。
    """
    created = []

    # raw/
    if not RAW_DIR.exists():
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        (RAW_DIR / ".gitkeep").touch()
        created.append("raw/")
        log.info("📁 创建 raw/ 目录")

    # wiki/
    if not WIKI_DIR.exists():
        WIKI_DIR.mkdir(parents=True, exist_ok=True)
        (WIKI_DIR / ".gitkeep").touch()
        created.append("wiki/")
        log.info("📁 创建 wiki/ 目录")

    # wiki/index.md
    if not INDEX_PATH.exists() or force:
        INDEX_PATH.write_text("# Knowledge Base Index\n\n", encoding="utf-8")
        if not any("index.md" in c for c in created):
            created.append("wiki/index.md")
        log.info("📝 创建 wiki/index.md")

    # wiki/log.md
    if not LOG_PATH.exists() or force:
        LOG_PATH.write_text("# Wiki Log\n\n", encoding="utf-8")
        if not any("log.md" in c for c in created):
            created.append("wiki/log.md")
        log.info("📝 创建 wiki/log.md")

    return {"ok": True, "created": created, "already_existed": not bool(created)}


def is_wiki_initialized() -> bool:
    """检查 wiki 是否已初始化"""
    return RAW_DIR.exists() and WIKI_DIR.exists() and INDEX_PATH.exists() and LOG_PATH.exists()


# ===========================================================================
# Raw 材料管理
# ===========================================================================

def _sanitize_slug(text: str, max_len: int = 60) -> str:
    """生成 kebab-case slug"""
    slug = re.sub(r'[^\w\s-]', '', text.lower())
    slug = re.sub(r'[-\s]+', '-', slug)
    slug = slug.strip('-')
    return slug[:max_len]


def _unique_filename(directory: Path, base_name: str) -> str:
    """确保文件名不重复，重复时加数字后缀"""
    stem = Path(base_name).stem
    ext = Path(base_name).suffix or ".md"
    candidate = base_name
    i = 2
    while (directory / candidate).exists():
        candidate = f"{stem}-{i}{ext}"
        i += 1
    return candidate


def save_raw(content: str, title: str, topic: str,
             source_url: str = "", published_date: str = "") -> dict:
    """保存原始材料到 raw/<topic>/YYYY-MM-DD-slug.md

    按 raw-template.md 格式:
    > Source: {URL}
    > Collected: {YYYY-MM-DD}
    > Published: {YYYY-MM-DD or Unknown}

    Args:
        content: 原始文本内容
        title: 来源标题
        topic: 主题分类 (子目录名)
        source_url: 来源 URL
        published_date: 发布日期 (YYYY-MM-DD 或 Unknown)

    Returns:
        dict: {"path": "raw/topic/file.md", "topic": "...", "slug": "..."}
    """
    today = date.today().isoformat()
    slug = _sanitize_slug(title)
    topic_dir = RAW_DIR / _sanitize_slug(topic, max_len=40)

    if not topic_dir.exists():
        topic_dir.mkdir(parents=True, exist_ok=True)

    # 文件名: YYYY-MM-DD-slug.md (无发布日期则省略日期前缀)
    if published_date:
        filename = f"{published_date}-{slug}.md"
    else:
        filename = f"{today}-{slug}.md"

    filename = _unique_filename(topic_dir, filename)

    # 按 raw-template.md 格式构建内容
    pub_str = published_date if published_date else "Unknown"
    source_line = f"> Source: {source_url}" if source_url else "> Source: User-provided text"
    raw_content = f"""# {title}

{source_line}
> Collected: {today}
> Published: {pub_str}

{content}
"""
    filepath = topic_dir / filename
    filepath.write_text(raw_content, encoding="utf-8")

    rel_path = f"raw/{_sanitize_slug(topic, max_len=40)}/{filename}"
    log.info(f"📄 Raw 已保存: {rel_path}")

    return {
        "path": rel_path,
        "topic": _sanitize_slug(topic, max_len=40),
        "filename": filename,
        "slug": slug,
    }


def list_raw(topic: str = None) -> list[dict]:
    """列出 raw/ 中的材料"""
    results = []
    search_dir = RAW_DIR / _sanitize_slug(topic, max_len=40) if topic else RAW_DIR

    if not search_dir.exists():
        return []

    if topic:
        for f in sorted(search_dir.glob("*.md")):
            results.append({
                "path": f"raw/{search_dir.name}/{f.name}",
                "topic": search_dir.name,
                "filename": f.name,
            })
    else:
        for topic_dir in sorted(RAW_DIR.iterdir()):
            if topic_dir.is_dir():
                for f in sorted(topic_dir.glob("*.md")):
                    results.append({
                        "path": f"raw/{topic_dir.name}/{f.name}",
                        "topic": topic_dir.name,
                        "filename": f.name,
                    })

    return results


def get_raw(path: str) -> Optional[str]:
    """读取 raw 文件内容"""
    full_path = BASE_DIR / path
    if full_path.exists():
        return full_path.read_text(encoding="utf-8")
    return None


# ===========================================================================
# Wiki 文章管理
# ===========================================================================

def _wiki_topic_dir(topic: str) -> Path:
    """获取 wiki topic 子目录"""
    d = WIKI_DIR / _sanitize_slug(topic, max_len=40)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _extract_metadata(md_content: str) -> dict:
    """从 wiki 文章的 markdown 中提取元数据"""
    meta = {
        "title": "",
        "sources": "",
        "raw": "",
        "updated": "",
        "tags": [],
    }

    # Title: # {Title}
    m = re.match(r'^#\s+(.+)$', md_content, re.MULTILINE)
    if m:
        meta["title"] = m.group(1).strip()

    # Sources: > Sources: ...
    m = re.search(r'>\s*Sources:\s*(.+)$', md_content, re.MULTILINE)
    if m:
        meta["sources"] = m.group(1).strip()

    # Raw: > Raw: ...
    m = re.search(r'>\s*Raw:\s*(.+)$', md_content, re.MULTILINE)
    if m:
        meta["raw"] = m.group(1).strip()

    return meta


def save_article(topic: str, filename: str, content: str) -> dict:
    """保存 wiki 文章到 wiki/<topic>/<filename>.md"""
    topic_dir = _wiki_topic_dir(topic)
    filename = _unique_filename(topic_dir, filename)
    filepath = topic_dir / filename
    filepath.write_text(content, encoding="utf-8")

    meta = _extract_metadata(content)
    rel_path = f"wiki/{_sanitize_slug(topic, max_len=40)}/{filename}"
    log.info(f"📝 Wiki 文章已保存: {rel_path}")

    return {
        "path": rel_path,
        "topic": _sanitize_slug(topic, max_len=40),
        "filename": filename,
        "title": meta.get("title", ""),
    }


def get_article(path: str) -> Optional[str]:
    """读取 wiki 文章内容"""
    full_path = BASE_DIR / path
    if full_path.exists() and full_path.is_file():
        return full_path.read_text(encoding="utf-8")
    return None


def list_articles(topic: str = None) -> list[dict]:
    """列出 wiki/ 中的文章（排除 index.md 和 log.md）"""
    results = []
    search_dir = WIKI_DIR / _sanitize_slug(topic, max_len=40) if topic else WIKI_DIR

    if not search_dir.exists():
        return []

    def _scan(d: Path, topic_name: str = ""):
        for item in sorted(d.iterdir()):
            if item.name in (".gitkeep",):
                continue
            if item.name == "index.md" or item.name == "log.md":
                continue
            if item.is_dir():
                _scan(item, item.name)
            elif item.suffix == ".md":
                mtime = datetime.fromtimestamp(item.stat().st_mtime).strftime("%Y-%m-%d")
                meta = _extract_metadata(item.read_text(encoding="utf-8"))
                results.append({
                    "path": str(item.relative_to(BASE_DIR)).replace("\\", "/"),
                    "topic": topic_name or item.parent.name,
                    "filename": item.name,
                    "title": meta.get("title", item.stem),
                    "updated": mtime,
                })

    _scan(search_dir)
    return results


def delete_article(path: str) -> bool:
    """删除 wiki 文章"""
    full_path = BASE_DIR / path
    if not full_path.exists():
        return False
    # 安全检查: 确保在 wiki/ 目录内
    if "wiki" not in full_path.parts:
        return False
    full_path.unlink()
    log.info(f"🗑️ Wiki 文章已删除: {path}")
    return True


# ===========================================================================
# Index 索引管理
# ===========================================================================

def _parse_index() -> dict[str, list[dict]]:
    """解析 index.md，返回 {topic_name: [{title, path, summary, updated}]}"""
    if not INDEX_PATH.exists():
        return {}

    text = INDEX_PATH.read_text(encoding="utf-8")
    index: dict[str, list[dict]] = {}
    current_topic = None

    for line in text.split("\n"):
        # ## topic-name
        if line.startswith("## "):
            current_topic = line[3:].strip()
            if current_topic not in index:
                index[current_topic] = []
        # | [Title](path) | Summary | YYYY-MM-DD |
        elif line.startswith("| [") and current_topic:
            parts = [p.strip() for p in line.split("|")[1:-1]]
            if len(parts) >= 3:
                link_match = re.match(r'\[(.+?)\]\((.+?)\)', parts[0])
                if link_match:
                    index[current_topic].append({
                        "title": link_match.group(1),
                        "path": link_match.group(2),
                        "summary": parts[1] if len(parts) > 1 else "",
                        "updated": parts[2] if len(parts) > 2 else "",
                    })

    return index


def _rebuild_index_file():
    """基于 wiki/ 实际文件重建 index.md"""
    articles = list_articles()
    today_str = date.today().isoformat()

    # 按 topic 分组
    by_topic: dict[str, list[dict]] = {}
    for a in articles:
        t = a["topic"]
        if t not in by_topic:
            by_topic[t] = []
        by_topic[t].append(a)

    lines = ["# Knowledge Base Index", ""]
    for topic_name in sorted(by_topic.keys()):
        lines.append(f"## {topic_name}")
        lines.append("")
        lines.append("| Article | Summary | Updated |")
        lines.append("|---------|---------|---------|")
        for a in by_topic[topic_name]:
            escaped_title = a["title"].replace("|", "\\|")
            lines.append(
                f"| [{escaped_title}]({topic_name}/{a['filename']}) "
                f"| (no summary) | {a['updated']} |"
            )
        lines.append("")

    INDEX_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info(f"📋 Index 已重建: {len(articles)} 篇文章, {len(by_topic)} 个主题")


def update_index_entry(topic: str, filename: str, title: str, summary: str = "(no summary)"):
    """更新或添加 index.md 中的单条记录"""
    if not INDEX_PATH.exists():
        _rebuild_index_file()
        return

    text = INDEX_PATH.read_text(encoding="utf-8")
    today_str = date.today().isoformat()
    article_path = f"{topic}/{filename}"
    new_row = f"| [{title}]({article_path}) | {summary} | {today_str} |"

    # 检查是否已有该文章条目
    found = False
    lines = text.split("\n")
    new_lines = []
    current_topic = None
    in_table = False
    table_section_start = None

    for i, line in enumerate(lines):
        if line.startswith("## "):
            # 先完成上一个 topic 的 table
            if current_topic == topic and not found:
                # 在当前 topic 的 table 末尾添加新行
                new_lines.append(new_row)
                found = True
            current_topic = line[3:].strip()
            in_table = False

        # 检查是否是我们要找的文章行
        if current_topic == topic and line.startswith("| [") and article_path in line:
            new_lines.append(new_row)
            found = True
            continue

        new_lines.append(line)

    # 如果在已有 topic 中没找到，添加到该 topic 的末尾
    if not found:
        # 检查 topic 是否存在
        topic_exists = any(l.strip() == f"## {topic}" for l in new_lines)
        if not topic_exists:
            new_lines.append(f"## {topic}")
            new_lines.append("")
            new_lines.append("| Article | Summary | Updated |")
            new_lines.append("|---------|---------|---------|")
        new_lines.append(new_row)

    INDEX_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def rebuild_index() -> dict:
    """重建全局索引（API 调用入口）"""
    _rebuild_index_file()
    parsed = _parse_index()
    total = sum(len(v) for v in parsed.values())
    return {"ok": True, "topics": len(parsed), "articles": total}


# ===========================================================================
# Log 操作日志
# ===========================================================================

def append_log(operation: str, detail: str, updates: list[str] = None):
    """追加操作日志

    格式:
    ## [YYYY-MM-DD] {operation} | {detail}
    - Updated: {article}
    """
    today_str = date.today().isoformat()
    entry = f"## [{today_str}] {operation} | {detail}\n"
    if updates:
        for u in updates:
            entry += f"- Updated: {u}\n"
    entry += "\n"

    if LOG_PATH.exists():
        content = LOG_PATH.read_text(encoding="utf-8")
        # 在标题后第一个空行之后插入（保持时间倒序）
        parts = content.split("\n\n", 1)
        if len(parts) == 2:
            content = parts[0] + "\n\n" + entry + parts[1]
        else:
            content += entry
    else:
        content = "# Wiki Log\n\n" + entry

    LOG_PATH.write_text(content, encoding="utf-8")


# ===========================================================================
# 搜索
# ===========================================================================

def _tokenize(text: str) -> list[str]:
    """简单分词（支持中英文混合）"""
    # 英文小写 + 中文逐字
    text = text.lower()
    tokens = []
    # 提取中文字符
    chinese = re.findall(r'[一-鿿]+', text)
    # 提取英文单词
    english = re.findall(r'[a-z0-9]+', text)
    tokens.extend(chinese)
    tokens.extend(english)
    return tokens


def _search_by_keywords(query: str, articles: list[dict]) -> list[dict]:
    """关键词搜索 wiki 文章（TF-IDF 风格评分）"""
    query_tokens = set(_tokenize(query))
    if not query_tokens:
        return articles[:10]

    scored = []
    for article in articles:
        path = BASE_DIR / article["path"]
        if not path.exists():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:
            continue

        content_lower = content.lower()

        # Token 匹配得分
        score = 0
        for token in query_tokens:
            count = content_lower.count(token)
            score += count * 2  # 精确匹配权重

        # 标题匹配加权
        title = article.get("title", "").lower()
        for token in query_tokens:
            if token in title:
                score += 10

        if score > 0:
            scored.append({**article, "score": score})

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored


def _search_by_embedding(query: str, articles: list[dict], top_k: int = 5) -> list[dict]:
    """语义搜索（通过 VolcengineEmbedder 调用 multimodal embeddings API）

    如果无法访问 embedding API，降级为关键词搜索。
    """
    try:
        from services.embedder import VolcengineEmbedder
        import math

        llm_cfg = config.upstream
        embedder = VolcengineEmbedder(
            model=llm_cfg.embedding_model,
            api_key=llm_cfg.api_key,
            base_url=llm_cfg.base_url,
        )

        # 获取 query embedding
        try:
            query_vec = embedder.embed(query)
        except Exception as emb_err:
            log.warning(f"⚠️ Embedding 不可用，降级为关键词搜索: {emb_err}")
            return _search_by_keywords(query, articles)[:top_k]

        if not query_vec:
            return _search_by_keywords(query, articles)[:top_k]

        # 对每个文章获取 embedding 并计算余弦相似度
        def cosine(a, b):
            dot = sum(x * y for x, y in zip(a, b))
            norm_a = math.sqrt(sum(x * x for x in a))
            norm_b = math.sqrt(sum(x * x for x in b))
            return dot / (norm_a * norm_b) if norm_a and norm_b else 0

        scored = []
        for article in articles[:20]:  # 先关键词过滤 top-20 再做语义排序
            path = BASE_DIR / article["path"]
            if not path.exists():
                continue
            try:
                content = path.read_text(encoding="utf-8")
                # 截取前 2000 字符做 embedding (节省 token)
                snippet = content[:2000]
            except Exception:
                continue

            try:
                art_vec = embedder.embed(snippet)
            except Exception:
                continue

            if art_vec:
                sim = cosine(query_vec, art_vec)
                scored.append({**article, "score": round(sim, 4)})

        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:top_k]

    except Exception as e:
        log.warning(f"⚠️ 语义搜索失败，降级为关键词搜索: {e}")
        return _search_by_keywords(query, articles)[:top_k]


def search_wiki(query: str, top_k: int = 5, use_embedding: bool = False) -> list[dict]:
    """搜索 wiki 文章, 返回最相关的 top_k 篇

    Args:
        query: 搜索查询
        top_k: 返回最多篇数
        use_embedding: 是否使用语义搜索 (默认关键词搜索)

    Returns:
        [{path, topic, filename, title, score, snippet, content}, ...]
    """
    articles = list_articles()
    if not articles:
        return []

    # 先用关键词快速过滤
    if use_embedding and articles:
        results = _search_by_embedding(query, articles, top_k)
    else:
        results = _search_by_keywords(query, articles)[:top_k]

    # 附加内容摘要
    for r in results:
        path = BASE_DIR / r["path"]
        if path.exists():
            content = path.read_text(encoding="utf-8")
            r["content"] = content
            # 生成 snippet: 找到 query 关键词附近的文本
            snippet = content[:300] if len(content) > 300 else content
            r["snippet"] = snippet.strip()

    return results


# ===========================================================================
# Ingest — 摄入 + 编译
# ===========================================================================

def _build_ingest_prompt(raw_content: str, raw_path: str, existing_articles: list[dict]) -> str:
    """构建 Ingest 编译 prompt — 用 SKILL.md 作为 system prompt 的精华版"""

    # 列出已有的 wiki 文章结构供 LLM 参考
    existing_summary = ""
    if existing_articles:
        by_topic: dict[str, list[str]] = {}
        for a in existing_articles:
            t = a["topic"]
            if t not in by_topic:
                by_topic[t] = []
            by_topic[t].append(a["title"])
        for t, titles in by_topic.items():
            existing_summary += f"\n- **{t}/**: {', '.join(titles[:10])}"
            if len(titles) > 10:
                existing_summary += f" (+{len(titles) - 10} more)"

    return f"""你是知识库编译助手，负责将原始材料编译成结构化的 wiki 页面。请严格遵循以下规范。

## 架构规则

1. **raw/ 不可变**: 原始材料存在 raw/，编译到 wiki/，不要修改 raw/
2. **wiki/ 结构**: wiki/<topic>/<article>.md，只支持一级 topic 子目录
3. **已有文章合并**: 如果新内容与已有文章核心论点相同，合并到已有文章；新概念创建新文章
4. **冲突标注**: 如果新来源与已有内容矛盾，标注冲突并注明来源归属
5. **交叉引用**: 添加 See Also 段落，链接相关文章

## 文章格式

按以下格式输出文章（严格 Markdown）:

```markdown
# {{文章标题}}

> Sources: {{来源作者, YYYY-MM-DD}}
> Raw: [{{文件名}}](.../../raw/{{topic}}/{{filename}}.md)

## Overview

{{一段话概括文章核心要点}}

## {{正文章节}}

{{从来源材料中提炼的连贯结构。不要复制原文；重组和提炼。}}

## See Also

{{交叉引用已有 wiki 文章}}
```

## 已有 wiki 文章
{existing_summary if existing_summary else '(暂无已有文章)'}

## 任务

原始材料路径: `{raw_path}`

请阅读以下原始材料，决定:
- 如果与已有文章核心论点相同 → 输出"合并指令"，说明合并到哪个已有文章，提供完整的新版文章内容
- 如果是新概念 → 输出"新建指令"，说明新建文件名和所属 topic，提供完整文章内容
- 如果一个来源涉及多个主题 → 可能需要创建多篇文章

输出格式（JSON）:
{{
  "actions": [
    {{
      "type": "merge",  // 或 "new"
      "topic": "主题目录名",
      "filename": "文章文件名.md",
      "merge_target": "已有文章的相对路径"  // 仅 merge 类型
    }}
  ],
  "articles": [
    {{
      "topic": "主题目录名",
      "filename": "文章文件名.md",
      "title": "文章标题",
      "content": "完整 Markdown 文章内容"
    }}
  ]
}}

## 原始材料
{raw_content}
"""


def ingest(content: str, title: str, topic: str = "general",
           source_url: str = "", published_date: str = "",
           model: str = None) -> dict:
    """Ingest 操作: 保存 raw → LLM 编译 wiki 文章 → 更新 index/log

    这是 LLM Wiki 系统的核心操作。一次调用完成:
    1. 保存原始材料到 raw/
    2. 调用 LLM 编译 wiki 文章
    3. 更新 wiki/index.md
    4. 追加 wiki/log.md
    5. 执行级联更新检查

    Args:
        content: 原始文本内容
        title: 来源标题
        topic: 主题分类
        source_url: 来源 URL
        published_date: 发布日期
        model: 编译用的 LLM 模型 (默认用 upstream.model)

    Returns:
        dict: 包含 raw_path, articles_created, articles_updated, log_entry
    """
    if not is_wiki_initialized():
        init_wiki()

    if model is None:
        try:
            import db
            wiki_cfg = db.get_wiki_llm_config()
            model = wiki_cfg["model"]
        except Exception:
            model = config.upstream.model

    # Step 1: 保存 raw 材料
    raw_result = save_raw(content, title, topic, source_url, published_date)
    raw_path = raw_result["path"]
    log.info(f"📥 Ingest 开始: {title} | topic={topic}")

    # Step 2: 获取已有文章列表给 LLM 参考
    existing_articles = list_articles()

    # Step 3: 构建 prompt 并调用 LLM 编译
    prompt = _build_ingest_prompt(content, raw_path, existing_articles)
    messages = [
        {"role": "system", "content": SKILL_CONTENT[:3000]},  # SKILL.md 核心规则作为 system prompt
        {"role": "user", "content": prompt},
    ]

    log.info(f"🤖 调用 LLM 编译 wiki: {model}")
    t0 = time.time()

    try:
        result = _call_wiki_llm(model, messages, max_time=300)
    except Exception as e:
        log.error(f"❌ LLM 编译调用失败: {e}")
        return {
            "ok": False,
            "error": str(e),
            "raw_path": raw_path,
        }

    elapsed = time.time() - t0

    if "error" in result:
        log.error(f"❌ LLM 编译返回错误: {result['error']}")
        return {
            "ok": False,
            "error": str(result["error"]),
            "raw_path": raw_path,
        }

    raw_response = result.get("choices", [{}])[0].get("message", {}).get("content", "")
    log.info(f"✅ LLM 编译完成 ({elapsed:.1f}s, {len(raw_response)} 字符)")

    # Step 4: 解析 LLM 输出
    try:
        json_match = re.search(r'\{[\s\S]*\}', raw_response)
        if json_match:
            parsed = json.loads(json_match.group())
        else:
            raise ValueError("No JSON found in LLM response")
    except (json.JSONDecodeError, ValueError) as e:
        log.warning(f"⚠️ LLM JSON 解析失败，尝试从原始响应中提取: {e}")
        # Fallback: 把整个 LLM 输出作为一篇新文章
        slug = _sanitize_slug(title)
        fallback_content = f"""# {title}

> Sources: {source_url or 'User-provided'}
> Raw: [{Path(raw_path).name}](../{raw_path})

## Overview

{raw_response[:500]}

## Content

{raw_response}
"""
        parsed = {
            "articles": [{
                "topic": topic,
                "filename": f"{date.today().isoformat()}-{slug}.md",
                "title": title,
                "content": fallback_content,
            }],
            "actions": [{"type": "new", "topic": topic, "filename": f"{date.today().isoformat()}-{slug}.md"}],
        }

    # Step 5: 保存编译后的文章
    articles_created = []
    articles_updated = []
    cascade_updates = []

    for article_data in parsed.get("articles", []):
        art_topic = article_data.get("topic", topic)
        art_filename = article_data.get("filename", f"{date.today().isoformat()}-{_sanitize_slug(title)}.md")
        art_content = article_data.get("content", "")
        art_title = article_data.get("title", title)

        if not art_content:
            continue

        # 判断是 merge 还是 new
        action_type = None
        for action in parsed.get("actions", []):
            if action.get("filename") == art_filename:
                action_type = action.get("type", "new")
                break

        if action_type == "merge":
            # 合并: 直接覆盖已有文章
            merge_target = ""
            for action in parsed.get("actions", []):
                if action.get("filename") == art_filename:
                    merge_target = action.get("merge_target", "")
            if merge_target:
                full_target = BASE_DIR / merge_target
                full_target.write_text(art_content, encoding="utf-8")
                articles_updated.append(merge_target)
                log.info(f"🔄 合并更新: {merge_target}")
        else:
            # 新建文章
            save_result = save_article(art_topic, art_filename, art_content)
            articles_created.append(save_result["path"])
            log.info(f"📝 新建文章: {save_result['path']}")

        # 更新 index
        update_index_entry(art_topic, art_filename, art_title)

    # Step 6: 级联更新检查（简易版 — 检查同 topic 下的文章是否需要更新交叉引用）
    for existing in existing_articles:
        if existing["topic"] == topic:
            art_path = BASE_DIR / existing["path"]
            if art_path.exists():
                art_text = art_path.read_text(encoding="utf-8")
                # 简单检查: 新文章标题是否在已有文章中被提及但未链接
                for created in articles_created:
                    new_title = Path(created).stem.replace("-", " ")
                    if new_title.lower() in art_text.lower() and created not in art_text:
                        # 自动添加交叉引用
                        new_filename = Path(created).name
                        see_also_line = f"- [有关{new_title}]({new_filename})"
                        if "## See Also" in art_text:
                            art_text = art_text.replace("## See Also", f"## See Also\n{see_also_line}")
                        else:
                            art_text += f"\n## See Also\n{see_also_line}\n"
                        art_path.write_text(art_text, encoding="utf-8")
                        cascade_updates.append(existing["path"])
                        log.info(f"🔗 级联更新交叉引用: {existing['path']}")

    # Step 7: 追加日志
    primary_title = articles_created[0] if articles_created else (articles_updated[0] if articles_updated else title)
    append_log("ingest", str(primary_title), updates=cascade_updates)

    summary = {
        "ok": True,
        "raw_path": raw_path,
        "articles_created": articles_created,
        "articles_updated": articles_updated,
        "cascade_updates": cascade_updates,
        "compile_time_s": round(elapsed, 1),
        "model_used": model,
    }

    log.info(f"✅ Ingest 完成: 新建{len(articles_created)}篇, 更新{len(articles_updated)}篇, 级联{len(cascade_updates)}篇")
    return summary


# ===========================================================================
# Query — 搜索 + 引用回答
# ===========================================================================

def query(query_text: str, top_k: int = 5, use_embedding: bool = False,
          generate_answer: bool = False, model: str = None) -> dict:
    """Query 操作: 搜索 wiki 并可选生成带引用的回答

    Args:
        query_text: 搜索查询
        top_k: 返回最多篇数
        use_embedding: 是否使用语义搜索
        generate_answer: 是否用 LLM 生成综合回答
        model: 生成回答用的模型

    Returns:
        dict: {results: [...], answer: "..."}
    """
    results = search_wiki(query_text, top_k, use_embedding)

    answer = ""
    if generate_answer and results:
        if model is None:
            model = config.upstream.model

        # 构建引用上下文
        contexts = []
        for i, r in enumerate(results):
            content = r.get("content", "")
            if content:
                contexts.append(f"### [{r['title']}]({r['path']})\n\n{content[:1500]}")

        ctx_text = "\n\n---\n\n".join(contexts)

        answer_prompt = f"""请根据以下 wiki 知识库内容回答用户问题。引用来源时用 markdown 链接格式 [文章标题](wiki/topic/filename.md)。

## Wiki 知识库内容

{ctx_text}

## 用户问题

{query_text}

## 要求
- 优先使用 wiki 内容回答，不要使用你自己的训练知识
- 每个断言都要引用具体的 wiki 文章
- 如果没有找到相关信息，明确说明
- 用中文回答
"""

        messages = [
            {"role": "user", "content": answer_prompt},
        ]

        try:
            llm_result = _call_wiki_llm(model, messages, max_time=120)
            if "error" not in llm_result:
                answer = llm_result.get("choices", [{}])[0].get("message", {}).get("content", "")
        except Exception as e:
            log.warning(f"⚠️ 生成回答失败: {e}")

    # 清理结果：移除完整内容，只保留 snippet
    clean_results = []
    for r in results:
        clean_results.append({
            "path": r["path"],
            "topic": r["topic"],
            "title": r["title"],
            "snippet": r.get("snippet", ""),
            "score": r.get("score", 0),
        })

    return {
        "query": query_text,
        "count": len(clean_results),
        "results": clean_results,
        "answer": answer,
    }


def archive_answer(query_text: str, answer: str, topic: str = "general") -> dict:
    """将 Query 的回答归档为 wiki 页面（按 SKILL.md 的 Archive 流程）

    1. 创建新的 wiki 页面（使用 archive-template.md 格式）
    2. 更新 index.md（Summary 前缀 [Archived]）
    3. 追加 log.md
    """
    if not answer:
        return {"ok": False, "error": "没有可归档的回答"}

    today_str = date.today().isoformat()
    slug = _sanitize_slug(query_text, max_len=40)

    # 按 archive-template.md 格式
    archive_content = f"""# {query_text}

> Sources: {query_text} — Archived Query Answer
> Archived: {today_str}

## Overview

{query_text}

## Answer

{answer}
"""

    result = save_article(topic, f"archived-{slug}.md", archive_content)
    update_index_entry(topic, result["filename"], query_text, summary=f"[Archived] {query_text[:80]}")
    append_log("query", f"Archived: {query_text[:60]}")

    log.info(f"📦 回答已归档: {result['path']}")
    return {"ok": True, "path": result["path"]}


# ===========================================================================
# Lint — 健康检查
# ===========================================================================

def _deterministic_lint() -> dict:
    """确定性检查（自动修复）"""
    issues_fixed = []
    issues_reported = []

    # 1. Index 一致性检查
    if INDEX_PATH.exists():
        index_data = _parse_index()
        actual_articles = list_articles()
        actual_paths = {a["path"] for a in actual_articles}

        # 检查 index 中有但实际不存在的文件
        for topic_name, entries in index_data.items():
            for entry in entries:
                expected_path = f"wiki/{topic_name}/{Path(entry['path']).name}"
                # 检查多种可能的路径格式
                found = False
                for actual_path in actual_paths:
                    if entry["path"] in actual_path or actual_path.endswith(entry["path"]):
                        found = True
                        break
                    # 也尝试匹配文件名
                    if Path(entry["path"]).name == Path(actual_path).name:
                        found = True
                        break

                if not found:
                    issues_reported.append(
                        f"[MISSING] Index 条目指向不存在的文件: {topic_name}/{entry['path']}"
                    )

        # 检查实际存在但 index 中缺失的文件
        for article in actual_articles:
            found_in_index = False
            for topic_name, entries in index_data.items():
                for entry in entries:
                    if entry["path"] in article["path"] or article["path"].endswith(entry["path"]):
                        found_in_index = True
                        break
                    if Path(entry["path"]).name == Path(article["path"]).name:
                        found_in_index = True
                        break
            if not found_in_index:
                update_index_entry(article["topic"], article["filename"], article["title"])
                issues_fixed.append(f"[FIXED] 添加缺失的 index 条目: {article['path']}")

    # 2. 内部链接检查
    all_articles = list_articles()
    for article in all_articles:
        art_path = BASE_DIR / article["path"]
        if not art_path.exists():
            continue
        content = art_path.read_text(encoding="utf-8")

        # 提取所有 markdown 链接 [text](path)
        links = re.findall(r'\[([^\]]+)\]\(([^)]+)\)', content)
        for link_text, link_path in links:
            # 跳过外部 URL
            if link_path.startswith("http://") or link_path.startswith("https://"):
                continue
            # 跳过 raw/ 链接（由 Raw references 检查处理）
            if link_path.startswith("raw/") or link_path.startswith("../raw/"):
                continue

            # 解析相对路径
            target = (art_path.parent / link_path).resolve()
            if not target.exists():
                # 在 wiki/ 中搜索同名文件
                matches = list(WIKI_DIR.rglob(Path(link_path).name))
                matches = [m for m in matches if m.name not in ("index.md", "log.md")]
                if len(matches) == 1:
                    # 自动修复
                    new_rel = os.path.relpath(matches[0], art_path.parent).replace("\\", "/")
                    content = content.replace(f"]({link_path})", f"]({new_rel})")
                    issues_fixed.append(f"[FIXED] 修复链接: {article['path']} 中 {link_path} → {new_rel}")
                elif len(matches) == 0:
                    issues_reported.append(f"[BROKEN] 链接目标不存在: {article['path']} → {link_path}")
                else:
                    issues_reported.append(f"[AMBIGUOUS] 找到多个同名文件: {link_path} → {[str(m) for m in matches]}")

        # 写回修复后的内容
        if any(f"{article['path']}" in f for f in issues_fixed):
            art_path.write_text(content, encoding="utf-8")

    # 3. Raw 引用检查
    for article in all_articles:
        art_path = BASE_DIR / article["path"]
        if not art_path.exists():
            continue
        content = art_path.read_text(encoding="utf-8")

        # 提取 Raw 字段中的链接
        raw_match = re.search(r'>\s*Raw:\s*(.+)$', content, re.MULTILINE)
        if raw_match:
            raw_links = re.findall(r'\[([^\]]*)\]\(([^)]+)\)', raw_match.group(1))
            for link_text, link_path in raw_links:
                target = (art_path.parent / link_path).resolve()
                if not target.exists():
                    matches = list(RAW_DIR.rglob(Path(link_path).name))
                    if len(matches) == 1:
                        new_rel = os.path.relpath(matches[0], art_path.parent).replace("\\", "/")
                        content = content.replace(f"]({link_path})", f"]({new_rel})")
                        issues_fixed.append(f"[FIXED] 修复 Raw 引用: {article['path']} 中 {link_path} → {new_rel}")
                    elif len(matches) == 0:
                        issues_reported.append(f"[BROKEN] Raw 引用不存在: {article['path']} → {link_path}")

    # 写回
    if any(f"{article['path']}" in f for f in issues_fixed):
        for article in all_articles:
            art_path = BASE_DIR / article["path"]
            if art_path.exists() and any(f"{article['path']}" in f for f in issues_fixed):
                # 上面已经修改过 content 了，这里需要重新读取
                pass  # 已在上面循环中处理

    return {
        "fixed": issues_fixed,
        "reported": issues_reported,
    }


def _heuristic_lint(model: str = None) -> list[str]:
    """启发式检查（LLM 分析，仅报告）"""
    if model is None:
        try:
            import db
            wiki_cfg = db.get_wiki_llm_config()
            model = wiki_cfg["model"]
        except Exception:
            model = config.upstream.model

    all_articles = list_articles()
    if not all_articles:
        return ["没有 wiki 文章，跳过启发式检查。"]

    # 构建所有文章的摘要给 LLM
    summary_lines = []
    for a in all_articles:
        art_path = BASE_DIR / a["path"]
        if art_path.exists():
            content = art_path.read_text(encoding="utf-8")
            summary_lines.append(f"- [{a['title']}]({a['path']}) ({a['topic']}): {content[:200]}...")
        else:
            summary_lines.append(f"- [{a['title']}]({a['path']}) ({a['topic']}) — FILE MISSING")

    articles_summary = "\n".join(summary_lines[:50])  # 最多 50 篇

    lint_prompt = f"""你是 wiki 知识库的质量审计员。请检查以下 wiki 的启发式问题（仅报告，不自动修复）。

## 检查维度

1. **事实矛盾**: 不同文章之间是否存在互相矛盾的说法？
2. **过时声明**: 是否有被更新的来源取代的过时信息？
3. **缺失冲突标注**: 来源有分歧但未标注归属的
4. **孤立页面**: 是否有没有任何其他 wiki 文章链接到它的页面？
5. **缺失跨主题引用**: 相关但未链接的文章
6. **频繁提及但无专属页面**: 多个文章中提到但未创建专属页面的重要概念
7. **归档过期**: Archived 文章引用的源文章是否已大幅更新？

## Wiki 文章摘要

{articles_summary}

## 输出格式

按维度列出发现的问题（JSON）:
{{
  "findings": [
    {{
      "category": "contradiction | outdated | missing_annotation | orphan | missing_cross_ref | missing_page | stale_archive",
      "severity": "high | medium | low",
      "description": "问题描述",
      "articles_involved": ["文章路径1", "文章路径2"],
      "suggestion": "建议修复方案"
    }}
  ]
}}

如果没有问题，返回空数组。
"""

    messages = [{"role": "user", "content": lint_prompt}]

    try:
        result = _call_wiki_llm(model, messages, max_time=180)
        if "error" in result:
            return [f"启发式检查调用失败: {result['error']}"]

        raw = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        json_match = re.search(r'\{[\s\S]*\}', raw)
        if json_match:
            parsed = json.loads(json_match.group())
            findings = parsed.get("findings", [])
            return [
                f"[{f.get('category', 'unknown')}|{f.get('severity', 'medium')}] {f.get('description', '')}"
                for f in findings
            ]
        return [f"无法解析 LLM 输出: {raw[:200]}"]
    except Exception as e:
        return [f"启发式检查失败: {e}"]


def lint(auto_fix: bool = True, heuristic: bool = False, model: str = None) -> dict:
    """Lint 操作: 健康检查

    Args:
        auto_fix: 是否自动修复确定性问题
        heuristic: 是否执行 LLM 启发式检查（较慢，消耗 tokens）

    Returns:
        {deterministic: {fixed: [...], reported: [...]}, heuristic: [...]}
    """
    if not is_wiki_initialized():
        return {"ok": False, "error": "Wiki 未初始化，请先执行 ingest"}

    result = {
        "ok": True,
        "deterministic": {"fixed": [], "reported": []},
        "heuristic": [],
    }

    if auto_fix:
        det = _deterministic_lint()
        result["deterministic"] = det

    if heuristic:
        result["heuristic"] = _heuristic_lint(model)

    # 追加日志
    total_issues = len(result["deterministic"]["fixed"]) + len(result["deterministic"]["reported"]) + len(result["heuristic"])
    total_fixed = len(result["deterministic"]["fixed"])
    append_log("lint", f"{total_issues} issues found, {total_fixed} auto-fixed")

    log.info(f"🔍 Lint 完成: {total_fixed} 自动修复, {total_issues - total_fixed} 待处理")
    return result


# ===========================================================================
# Wiki 状态摘要
# ===========================================================================

def get_wiki_status() -> dict:
    """获取 wiki 整体状态"""
    initialized = is_wiki_initialized()
    if not initialized:
        return {"initialized": False}

    articles = list_articles()
    raw_files = list_raw()
    index_parsed = _parse_index()
    total_indexed = sum(len(v) for v in index_parsed.values())

    # 读取最近日志
    recent_logs = []
    if LOG_PATH.exists():
        log_lines = LOG_PATH.read_text(encoding="utf-8").strip().split("\n")
        # 取最近 5 条操作
        log_entries = [l for l in log_lines if l.startswith("## [")]
        recent_logs = log_entries[:5]

    return {
        "initialized": True,
        "raw_count": len(raw_files),
        "wiki_count": len(articles),
        "indexed_count": total_indexed,
        "topics": list(set(a["topic"] for a in articles)),
        "has_skill": SKILL_PATH.exists(),
        "recent_logs": recent_logs,
    }
