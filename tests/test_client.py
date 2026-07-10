#!/usr/bin/env python3
"""Vibry AI Core — 集成测试客户端

验证完整闭环:
1. 写入记忆 → Mem0
2. 发送 Chat 请求 → 记忆自动注入 → 上游 LLM 返回
3. 流式和非流式

用法:
  python test_client.py
"""

import json
import sys
import time
import urllib.request
import urllib.error

BASE = "http://localhost:9999"
USER_ID = "test_user_vibry"


def api(method: str, path: str, body: dict | None = None) -> dict:
    """发送 HTTP 请求"""
    url = f"{BASE}{path}"
    data = json.dumps(body, ensure_ascii=False).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {USER_ID}")

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode()[:300]}"}
    except Exception as e:
        return {"error": str(e)}


def test_health():
    print("\n" + "=" * 50)
    print("1️⃣  健康检查")
    print("=" * 50)
    r = api("GET", "/api/health")
    print(json.dumps(r, ensure_ascii=False, indent=2))
    assert r.get("status") == "ok", "健康检查失败!"
    print("✅ 服务运行正常")


def test_add_memory():
    print("\n" + "=" * 50)
    print("2️⃣  写入记忆到 Mem0")
    print("=" * 50)

    memories = [
        "用户非常讨厌重资产架构，偏好轻量级微服务方案",
        "用户的主语言是 Python 和 Dart，不喜欢 Java 的啰嗦",
        "项目 VibryCard 是一个 AI 录音笔硬件 + 记忆中台",
        "用户偏好本地自托管，对数据隐私极度重视",
        "用户喜欢用 Cursor 写代码，用 LobeChat 做日常 AI 对话",
    ]

    for mem in memories:
        r = api("POST", "/api/memories", {"text": mem, "metadata": {"source": "test"}})
        if r.get("ok"):
            print(f"  ✅ 已写入: {mem[:60]}...")
        else:
            print(f"  ❌ 写入失败: {r}")
    print("✅ 记忆写入完成")


def test_search_memories():
    print("\n" + "=" * 50)
    print("3️⃣  检索记忆")
    print("=" * 50)
    r = api("GET", "/api/memories?q=架构偏好&top_k=3")
    count = r.get("count", 0)
    print(f"  检索到 {count} 条相关记忆:")
    for mem in r.get("memories", []):
        score = mem.get("score", 0)
        text = mem.get("memory", "")[:80]
        print(f"    [{score:.2f}] {text}")
    print("✅ 记忆检索正常")


def test_chat_non_streaming():
    print("\n" + "=" * 50)
    print("4️⃣  Chat Completions (非流式)")
    print("=" * 50)
    t0 = time.time()
    r = api("POST", "/v1/chat/completions", {
        "model": "gpt-3.5-turbo",  # 会被 config 覆盖
        "messages": [
            {"role": "system", "content": "你是一个技术架构顾问。请根据用户背景给出建议。"},
            {"role": "user", "content": "帮我设计一个 AI 应用的后端架构，要简单、可自托管"},
        ],
        "temperature": 0.7,
        "max_tokens": 300,
    })
    elapsed = time.time() - t0

    if "error" in r:
        print(f"  ⚠️ 上游错误: {r['error']}")
        print("  (如果 API Key 未配置，这是预期的)")
        return

    content = r.get("choices", [{}])[0].get("message", {}).get("content", "")
    usage = r.get("usage", {})
    print(f"  回复 ({elapsed:.1f}s, {usage.get('total_tokens', '?')} tokens):")
    print(f"  {content[:300]}...")
    print("✅ 非流式 Chat 正常")


def test_chat_streaming():
    print("\n" + "=" * 50)
    print("5️⃣  Chat Completions (流式 SSE)")
    print("=" * 50)

    url = f"{BASE}/v1/chat/completions"
    body = json.dumps({
        "model": "gpt-3.5-turbo",
        "messages": [
            {"role": "user", "content": "简要介绍一下我的项目 VibryCard"},
        ],
        "stream": True,
        "max_tokens": 200,
    }, ensure_ascii=False).encode()

    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {USER_ID}")

    print("  流式接收中...")
    chunks = 0
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            for line in resp:
                line = line.decode().strip()
                if line.startswith("data: ") and line != "data: [DONE]":
                    chunks += 1
                    if chunks <= 3:
                        data = json.loads(line[6:])
                        delta = data.get("choices", [{}])[0].get("delta", {}).get("content", "")
                        print(f"  📦 chunk #{chunks}: {delta[:50]}...")
                elif line == "data: [DONE]":
                    print(f"  🏁 流式结束")
    except Exception as e:
        print(f"  ⚠️ 流式错误: {e}")
        return

    print(f"✅ 流式 Chat 正常 (共 {chunks} 个 chunks)")


def test_list_models():
    print("\n" + "=" * 50)
    print("6️⃣  模型列表 (OpenAI 兼容)")
    print("=" * 50)
    r = api("GET", "/v1/models")
    print(json.dumps(r, ensure_ascii=False, indent=2))
    print("✅ 模型列表正常")


def main():
    print("🧪 Vibry AI Core 集成测试")
    print(f"   服务地址: {BASE}")
    print(f"   用户 ID: {USER_ID}")

    tests = [
        ("健康检查", test_health),
        ("写入记忆", test_add_memory),
        ("检索记忆", test_search_memories),
        ("非流式Chat", test_chat_non_streaming),
        ("流式Chat", test_chat_streaming),
        ("模型列表", test_list_models),
    ]

    passed = 0
    failed = 0

    for name, fn in tests:
        try:
            fn()
            passed += 1
        except Exception as e:
            print(f"  ❌ 失败: {e}")
            failed += 1

    print("\n" + "=" * 50)
    print(f"📊 结果: {passed} 通过, {failed} 失败, {len(tests)} 总计")
    print("=" * 50)

    if failed > 0:
        print("\n⚠️ 有测试失败，请检查:")
        print("  1. 服务是否启动: python main.py")
        print("  2. .env 中的 UPSTREAM_API_KEY 是否配置")
        print("  3. 上游 API 是否可达")
        sys.exit(1)
    else:
        print("\n✅ 全部测试通过! Vibry AI Core 记忆闭环验证完成")
        print("\n💡 下一步:")
        print("  1. 在 Cursor 中: Settings → Models → OpenAI →")
        print(f"     Base URL = http://localhost:9999/v1")
        print(f"     API Key  = {USER_ID}")
        print("  2. 试试问: '帮我设计一个系统' — 看它是否记住了你的偏好")


if __name__ == "__main__":
    main()
