"""Markdown-first content helpers for recording summaries and insights."""

from __future__ import annotations

import re


_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*$")
_BULLET_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)(.+?)\s*$")
_EMPTY_MARKERS = {"暂无", "无", "未提及", "暂无明确内容", "未识别", "未提供", "无法确认", "未知"}


def clean_markdown(raw: str) -> str:
    text = (raw or "").strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _key(title: str) -> str:
    return re.sub(r"[\s:：_-]+", "", title).lower()


def _sections(markdown: str) -> tuple[str, dict[str, str]]:
    preamble: list[str] = []
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in clean_markdown(markdown).splitlines():
        match = _HEADING_RE.match(line)
        if match:
            current = _key(match.group(1))
            sections.setdefault(current, [])
        elif current is None:
            preamble.append(line)
        else:
            sections[current].append(line)
    return "\n".join(preamble).strip(), {
        key: "\n".join(lines).strip() for key, lines in sections.items()
    }


def _find(sections: dict[str, str], *aliases: str) -> str:
    keys = {_key(alias) for alias in aliases}
    for key, value in sections.items():
        if key in keys and value.strip():
            return value.strip()
    for key, value in sections.items():
        if value.strip() and any(alias in key for alias in keys):
            return value.strip()
    return ""


def _items(text: str) -> list[str]:
    result: list[str] = []
    for line in (text or "").splitlines():
        match = _BULLET_RE.match(line)
        value = match.group(1) if match else line.strip()
        if value and value not in _EMPTY_MARKERS and not value.startswith("#"):
            result.append(value)
    return result


def _first_text(text: str) -> str:
    for line in (text or "").splitlines():
        value = re.sub(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)", "", line).strip()
        if value:
            return value
    return ""


def sanitize_summary_markdown(raw: str) -> str:
    """Remove unknown-value rows and empty optional sections deterministically."""
    lines = clean_markdown(raw).splitlines()
    filtered: list[str] = []
    for line in lines:
        match = re.match(r"^\s*[-*+]\s+[^:：]+[:：]\s*(.+?)\s*$", line)
        if match and match.group(1).strip() in _EMPTY_MARKERS:
            continue
        filtered.append(line)

    chunks: list[tuple[str | None, list[str]]] = []
    current_title: str | None = None
    current_lines: list[str] = []
    for line in filtered:
        match = _HEADING_RE.match(line)
        if match:
            chunks.append((current_title, current_lines))
            current_title = match.group(1)
            current_lines = [line]
        else:
            current_lines.append(line)
    chunks.append((current_title, current_lines))

    optional = {_key(value) for value in ("关键决定", "关键决策", "行动项", "后续行动", "标签", "Tags")}
    kept: list[str] = []
    for title, chunk in chunks:
        if title is not None and _key(title) in optional:
            body = "\n".join(chunk[1:]).strip()
            if not _items(body):
                continue
        kept.extend(chunk)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def parse_summary_markdown(raw: str) -> dict:
    markdown = sanitize_summary_markdown(raw)
    preamble, sections = _sections(markdown)
    intent_text = _find(sections, "核心目的", "核心意图", "核心主题", "主题", "概要")
    decisions_text = _find(sections, "明确决策", "关键决定", "关键决策", "决定", "结论")
    actions_text = _find(sections, "行动项", "后续行动", "下一步")
    tags_text = _find(sections, "标签", "Tags")
    if not intent_text:
        match = re.search(
            r"^\s*\*\*核心(?:主题|目的|意图)\*\*\s*[：:]\s*(.+?)\s*$",
            markdown,
            re.M,
        )
        intent_text = match.group(1).strip() if match else ""
    if not tags_text:
        match = re.search(
            r"^\s*\*\*关键词\*\*\s*[：:]\s*(.+?)\s*$",
            markdown,
            re.M,
        )
        if match:
            tags_text = "\n".join(
                f"- {item.strip()}"
                for item in re.split(r"[、,，]", match.group(1))
                if item.strip()
            )
    return {
        "markdown": markdown,
        "current_intent": _first_text(intent_text or preamble),
        "key_decisions": _items(decisions_text),
        "action_items": _items(actions_text),
        "memory_conflict": "",
        "proactive_next": "",
        "tags": [item.lstrip("#").strip() for item in _items(tags_text)],
        "detailed_summary": markdown,
        "full_summary": markdown,
    }


def summary_to_markdown(summary: dict) -> str:
    if not isinstance(summary, dict):
        return ""
    existing = summary.get("markdown") or summary.get("full_summary")
    if existing and "#" in str(existing):
        return clean_markdown(str(existing))
    detailed = str(summary.get("detailed_summary") or "").strip()
    intent = str(summary.get("current_intent") or "").strip()
    decisions = [str(item).strip() for item in summary.get("key_decisions") or [] if str(item).strip()]
    actions = [str(item).strip() for item in summary.get("action_items") or [] if str(item).strip()]
    tags = [str(item).strip() for item in summary.get("tags") or [] if str(item).strip()]
    parts = ["# 录音纪要"]
    if intent:
        parts.extend(["## 核心目的", intent])
    if decisions:
        parts.extend(["## 关键决定", *[f"- {item}" for item in decisions]])
    if actions:
        parts.extend(["## 行动项", *[f"- {item}" for item in actions]])
    if detailed:
        parts.extend(["## 详细纪要", detailed])
    if tags:
        parts.extend(["## 标签", *[f"- {item}" for item in tags]])
    return "\n\n".join(parts).strip()


def parse_recording_insight_markdown(raw: str) -> dict:
    markdown = clean_markdown(raw)
    preamble, sections = _sections(markdown)
    core = _find(sections, "核心洞察", "洞察", "核心判断") or preamble
    opportunity = _find(sections, "机会分析", "机会")
    risk = _find(sections, "风险提示", "风险", "盲点")
    actions = _find(sections, "行动建议", "建议", "下一步")
    if not any((core, opportunity, risk, actions)):
        core = markdown
    return {
        "markdown": markdown,
        "core_insight": core.strip(),
        "analysis": {"opportunity": opportunity.strip(), "risk": risk.strip()},
        "action_suggestions": _items(actions),
    }


def recording_insight_to_markdown(insight: dict) -> str:
    if not isinstance(insight, dict):
        return ""
    if insight.get("markdown"):
        return clean_markdown(str(insight["markdown"]))
    analysis = insight.get("analysis") if isinstance(insight.get("analysis"), dict) else {}
    parts = ["# 录音洞察"]
    values = (
        ("核心洞察", insight.get("core_insight")),
        ("机会分析", analysis.get("opportunity")),
        ("风险提示", analysis.get("risk")),
    )
    for title, value in values:
        if value:
            parts.extend([f"## {title}", str(value).strip()])
    actions = [str(item).strip() for item in insight.get("action_suggestions") or [] if str(item).strip()]
    if actions:
        parts.extend(["## 行动建议", *[f"- {item}" for item in actions]])
    return "\n\n".join(parts).strip()


def parse_memory_insight_markdown(raw: str, valid_evidence_ids: set[str] | None = None) -> dict:
    markdown = clean_markdown(raw)
    preamble, sections = _sections(markdown)
    evidence_ids = re.findall(r"\bclm_[A-Za-z0-9_-]+\b", markdown)
    if valid_evidence_ids is not None:
        evidence_ids = [item for item in evidence_ids if item in valid_evidence_ids]
    return {
        "markdown": markdown,
        "summary": (_find(sections, "总体判断", "关联判断", "摘要") or preamble).strip(),
        "connections": _items(_find(sections, "关联记忆", "关联")),
        "patterns": _items(_find(sections, "持续模式", "模式")),
        "conflicts": _items(_find(sections, "矛盾与变化", "矛盾", "变化")),
        "suggestions": _items(_find(sections, "个性化提示", "提示", "建议")),
        "evidence_ids": list(dict.fromkeys(evidence_ids)),
    }


def memory_insight_to_markdown(insight: dict) -> str:
    if not isinstance(insight, dict):
        return ""
    if insight.get("markdown"):
        return clean_markdown(str(insight["markdown"]))
    parts = ["# 记忆洞察"]
    summary = str(insight.get("summary") or "").strip()
    if summary:
        parts.extend(["## 总体判断", summary])
    for title, key in (
        ("关联记忆", "connections"),
        ("持续模式", "patterns"),
        ("矛盾与变化", "conflicts"),
        ("个性化提示", "suggestions"),
        ("证据", "evidence_ids"),
    ):
        items = [str(item).strip() for item in insight.get(key) or [] if str(item).strip()]
        if items:
            parts.extend([f"## {title}", *[f"- {item}" for item in items]])
    return "\n\n".join(parts).strip()
