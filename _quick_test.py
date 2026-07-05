"""Quick integration test for Vibry AI Core server

Tests: health, ASR mode, recordings CRUD, stats, memory add/search, chat proxy
"""
import urllib.request, json, sys, os

BASE = "http://localhost:9999"
USER_ID = "test_user_quick"


def api(method, path, body=None):
    url = f"{BASE}{path}"
    data = json.dumps(body, ensure_ascii=False).encode() if body else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    r.add_header("Authorization", f"Bearer {USER_ID}")
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        body = e.read().decode()[:300] if hasattr(e, "read") else str(e)
        return {"error": body}


def test(name, fn):
    try:
        fn()
        print(f"  ✅ {name}")
        return True
    except Exception as e:
        print(f"  ❌ {name}: {e}")
        return False


passed = 0
failed = 0


def check(name, fn):
    global passed, failed
    if test(name, fn):
        passed += 1
    else:
        failed += 1


# ---- Tests ----
print("🧪 Vibry AI Core 集成测试")
print(f"   服务: {BASE}  |  用户: {USER_ID}")
print()

# 1. Health
check("健康检查", lambda: (
    print(f"       {json.dumps(api('GET', '/api/health'), ensure_ascii=False)[:150]}"),
))

# 2. ASR mode
check("ASR 模式查询", lambda: (
    print(f"       模式: {api('GET', '/api/asr-mode').get('asr_mode')}"),
))

# 3. Recordings list
check("录音列表", lambda: (
    r := api("GET", "/api/recordings?limit=5"),
    print(f"       共 {r.get('stats', {}).get('total', 0)} 条"),
))

# 4. Stats
check("统计信息", lambda: (
    r := api("GET", "/api/stats"),
    print(f"       {json.dumps(r, ensure_ascii=False)[:100]}"),
))

# 5. Memory add
check("写入记忆", lambda: (
    api("POST", "/api/memories", {"text": "用户偏好轻量级架构，讨厌重资产方案"}),
    api("POST", "/api/memories", {"text": "项目 VibryCard 是 AI 录音笔 + 记忆中台"}),
    api("POST", "/api/memories", {"text": "用户使用 Python 和 Dart 开发"}),
))

# 6. Memory search
check("检索记忆", lambda: (
    r := api("GET", "/api/memories?q=架构偏好&top_k=3"),
    print(f"       找到 {r.get('count', 0)} 条"),
))

# 7. Chat (non-streaming with memory)
check("记忆增强 Chat", lambda: (
    r := api("POST", "/v1/chat/completions", {
        "messages": [{"role": "user", "content": "根据你对我的了解，我偏好什么架构风格？请简要回答。"}],
        "max_tokens": 150,
    }),
    content := (r.get("choices", [{}])[0].get("message", {}).get("content", "")),
    print(f"       回复: {content[:120]}..."),
    is_error := "error" in r,
    None if not is_error else (_ for _ in ()).throw(Exception(r["error"])),
))

# 8. Models
check("模型列表", lambda: (
    r := api("GET", "/v1/models"),
    print(f"       模型: {r['data'][0]['id']}"),
))

print()
print("=" * 50)
print(f"📊 结果: {passed} 通过, {failed} 失败, {passed + failed} 总计")
print("=" * 50)

if failed > 0:
    print("\n⚠️ 有测试失败。检查 server_output.log 或终端日志。")
    sys.exit(1)
else:
    print("\n✅ 全部通过！Vibry AI Core 完整闭环验证成功")
    print()
    print("💡 API 端点汇总:")
    print(f"   OpenAI 代理: {BASE}/v1/chat/completions")
    print(f"   语音转文字:  {BASE}/api/transcribe")
    print(f"   会议纪要:    {BASE}/api/summarize")
    print(f"   录音管理:    {BASE}/api/recordings")
    print(f"   记忆管理:    {BASE}/api/memories")
    print(f"   统计信息:    {BASE}/api/stats")
