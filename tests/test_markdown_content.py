from app.config import DEFAULT_SUMMARY_PROMPT
from services.markdown_content import (
    parse_memory_insight_markdown,
    parse_recording_insight_markdown,
    parse_summary_markdown,
    sanitize_summary_markdown,
)


def test_default_summary_prompt_is_markdown_only_and_omits_empty_sections():
    assert "只输出纯 Markdown" in DEFAULT_SUMMARY_PROMPT
    assert "不要输出 JSON" in DEFAULT_SUMMARY_PROMPT
    assert "省略整个“关键决定”章节" in DEFAULT_SUMMARY_PROMPT
    assert "不推测时间、时长、负责人" in DEFAULT_SUMMARY_PROMPT


def test_summary_markdown_builds_legacy_fields_without_json():
    result = parse_summary_markdown("""# 录音纪要
## 核心目的
确认新版上线安排
## 关键决定
- 周五发布
## 行动项
- 完成回归测试
## 会议主要内容
讨论了上线范围。
## 标签
- 产品发布
""")

    assert result["current_intent"] == "确认新版上线安排"
    assert result["key_decisions"] == ["周五发布"]
    assert result["action_items"] == ["完成回归测试"]
    assert result["tags"] == ["产品发布"]
    assert result["detailed_summary"].startswith("# 录音纪要")


def test_summary_markdown_reads_compact_metadata_fields():
    result = parse_summary_markdown("""# 录音纪要

**核心主题**：两天培训复盘
**关键词**：培训、销售、行动计划

## 主要内容

### 第一天
梳理销售流程。

## 明确决策
- 下周开始试运行
""")

    assert result["current_intent"] == "两天培训复盘"
    assert result["tags"] == ["培训", "销售", "行动计划"]
    assert result["key_decisions"] == ["下周开始试运行"]


def test_recording_insight_accepts_unstructured_markdown():
    result = parse_recording_insight_markdown("这是一段没有标准标题但仍应保存的洞察。")

    assert result["core_insight"] == "这是一段没有标准标题但仍应保存的洞察。"
    assert result["markdown"] == result["core_insight"]


def test_memory_insight_filters_evidence_ids_without_json():
    result = parse_memory_insight_markdown(
        """# 记忆洞察
## 总体判断
目标保持一致。
## 关联记忆
- 延续上周计划
## 证据
- clm_valid
- clm_unknown
""",
        {"clm_valid"},
    )

    assert result["summary"] == "目标保持一致。"
    assert result["connections"] == ["延续上周计划"]
    assert result["evidence_ids"] == ["clm_valid"]


def test_summary_sanitizer_drops_unknown_rows_and_empty_optional_sections():
    markdown = sanitize_summary_markdown("""# 录音纪要
## 基本信息
- 录音时间：无法确认
- 核心主题：产品讨论
## 关键决定
暂无
## 行动项
- 未提供
## 会议主要内容
讨论了产品方向。
""")

    assert "无法确认" not in markdown
    assert "关键决定" not in markdown
    assert "行动项" not in markdown
    assert "核心主题：产品讨论" in markdown
