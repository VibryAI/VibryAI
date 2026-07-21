"""Evidence-bound project insight generation with a deterministic fallback."""

from __future__ import annotations

import json
import logging
import re
from html import escape

from cognition import store

log = logging.getLogger("vibry.cognition.insights")


def _parse_json_object(raw: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.I)
    try:
        value = json.loads(cleaned)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for index, char in enumerate(cleaned):
            if char != "{":
                continue
            try:
                value, _ = decoder.raw_decode(cleaned[index:])
                if isinstance(value, dict):
                    return value
            except json.JSONDecodeError:
                continue
    return {}


def _evidence(brief: dict) -> list[dict]:
    result = []
    for claim in brief["claims"][:20]:
        result.append({"claim_id": claim["id"], "source_ids": [item["source_id"] for item in claim["evidence"]]})
    return result


def _fallback(brief: dict) -> list[dict]:
    claims = brief["claims"]
    decisions = [claim["content"] for claim in claims if "[决策]" in claim["content"]]
    actions = [claim["content"] for claim in claims if "[行动项]" in claim["content"]]
    content = "近期新增证据已归入项目。"
    if decisions:
        content += " 已记录决策：" + "；".join(decisions[:3])
    if actions:
        content += " 待跟进行动：" + "；".join(actions[:3])
    return [{"type": "fact", "title": "项目状态更新", "content": content, "confidence": 0.65}]


def _llm_insights(brief: dict) -> list[dict]:
    facts = "\n".join(f"- ({claim['id']}) {claim['content']}" for claim in brief["claims"][:40])
    if not facts:
        return []
    project = brief["project"]
    previous = "\n".join(
        f"- [{item['insight_type']}] {item['title']}: {item['content']}"
        for item in brief.get("insights", [])[:10]
    ) or "- 暂无上一版洞察"
    prompt = f"""你是 Vibry.AI 的项目战略分析器。根据项目事实生成洞察。

项目：{project['name']}
目标：{project['goal']}
阶段：{project['stage']}
事实（每条都带 claim_id）：
{facts}

上一版洞察：
{previous}

只输出 JSON：
{{"insights":[{{"type":"fact|risk|opportunity|gap|recommendation","title":"不超过20字","content":"简洁具体的洞察","confidence":0.0,"claim_ids":["clm_..."]}}]}}

要求：只输出相对上一版真正新增或发生变化的内容；事实、推断、建议严格区分；不得引用不存在的 claim_id；不确定时降低 confidence；每种 type 最多一条，总计最多 5 条。"""
    try:
        from app.config import config
        from services.asr import call_llm
        result = call_llm(config.summary.effective_model, [
            {"role": "system", "content": "你只输出有效 JSON，不编造事实。"},
            {"role": "user", "content": prompt},
        ], 120)
        if result.get("error"):
            return []
        raw = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        data = _parse_json_object(raw)
        valid_ids = {claim["id"] for claim in brief["claims"]}
        items = []
        seen_types = set()
        for item in data.get("insights", [])[:5]:
            insight_type = item.get("type", "inference")
            if insight_type in seen_types:
                continue
            claim_ids = [claim_id for claim_id in item.get("claim_ids", []) if claim_id in valid_ids]
            if not claim_ids or not item.get("content"):
                continue
            items.append({
                "type": insight_type,
                "title": item.get("title", "项目洞察")[:80],
                "content": item["content"][:4000],
                "confidence": float(item.get("confidence", 0.5)),
                "claim_ids": claim_ids,
            })
            seen_types.add(insight_type)
        return items
    except Exception as exc:
        log.warning("LLM project insight unavailable: %s", exc)
        return []


def generate_project_insights(
    project_id: str, user_id: str, trigger_type: str = "scheduled",
) -> list[dict]:
    brief = store.project_brief(project_id, user_id)
    if not brief:
        raise ValueError(f"project not found: {project_id}")
    items = _llm_insights(brief) or _fallback(brief)
    evidence_by_claim = {claim["id"]: claim["evidence"] for claim in brief["claims"]}
    created = []
    for item in items:
        claim_ids = item.get("claim_ids") or [claim["id"] for claim in brief["claims"][:3]]
        evidence = [{"claim_id": claim_id, "sources": evidence_by_claim.get(claim_id, [])} for claim_id in claim_ids]
        created.append(store.create_insight(
            user_id=user_id, project_id=project_id, insight_type=item["type"],
            title=item["title"], content=item["content"], confidence=item["confidence"],
            evidence=evidence, trigger_type=trigger_type,
        ))
    if created:
        project = brief["project"]
        thread = store.ensure_thread(
            user_id=user_id, scope_type="global", scope_id="", title="Memory Matrix",
        )
        rows = "".join(
            f"<li><strong>{escape(item['title'])}</strong><p>{escape(item['content'])}</p></li>"
            for item in created
        )
        store.add_message(
            thread_id=thread["id"], user_id=user_id, role="assistant",
            content=f"<h3>{escape(project['name'])} · 新的关联与提醒</h3><ul>{rows}</ul>",
            content_format="html", message_type="insight",
            metadata={"level": "L3", "project_id": project_id, "insight_ids": [item["id"] for item in created]},
        )
        store.add_event(
            user_id=user_id, project_id=project_id, event_type="l3_published",
            actor="system", object_type="thread", object_id=thread["id"],
            payload={"insight_ids": [item["id"] for item in created]},
        )
    store.mark_project_insighted(project_id)
    return created
