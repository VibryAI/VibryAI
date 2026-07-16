"""Evidence-bound project insight generation with a deterministic fallback."""

from __future__ import annotations

import json
import logging
import re
from html import escape

from cognition import store

log = logging.getLogger("vibry.cognition.insights")


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
    prompt = f"""你是 Vibry.AI 的项目战略分析器。根据项目事实生成洞察。

项目：{project['name']}
目标：{project['goal']}
阶段：{project['stage']}
事实（每条都带 claim_id）：
{facts}

只输出 JSON：
{{"insights":[{{"type":"fact|risk|opportunity|gap|recommendation","title":"不超过20字","content":"简洁具体的洞察","confidence":0.0,"claim_ids":["clm_..."]}}]}}

要求：事实、推断、建议严格区分；不得引用不存在的 claim_id；不确定时降低 confidence；最多 5 条。"""
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
        match = re.search(r"\{[\s\S]*\}", raw)
        data = json.loads(match.group() if match else raw)
        valid_ids = {claim["id"] for claim in brief["claims"]}
        items = []
        for item in data.get("insights", [])[:5]:
            claim_ids = [claim_id for claim_id in item.get("claim_ids", []) if claim_id in valid_ids]
            if not claim_ids or not item.get("content"):
                continue
            items.append({
                "type": item.get("type", "inference"),
                "title": item.get("title", "项目洞察")[:80],
                "content": item["content"][:4000],
                "confidence": float(item.get("confidence", 0.5)),
                "claim_ids": claim_ids,
            })
        return items
    except Exception as exc:
        log.warning("LLM project insight unavailable: %s", exc)
        return []


def generate_project_insights(project_id: str, user_id: str) -> list[dict]:
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
            title=item["title"], content=item["content"], confidence=item["confidence"], evidence=evidence,
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
            content=f"<h3>{escape(project['name'])} · L3 项目洞察</h3><ul>{rows}</ul>",
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
