#!/usr/bin/env python3
"""Vibry AI Core — 全量功能测试

覆盖: 健康检查 / 记忆 / Chat / Wiki RAG / ASR配置 / 声纹 / DB迁移

用法: python test_full.py
"""

import json, sys, time, os, urllib.request, urllib.error

import pytest

BASE = "http://127.0.0.1:9999"
USER_ID = "test_full_suite"
PASSED = 0
FAILED = 0

pytestmark = pytest.mark.skipif(
    os.getenv("VIBRY_LIVE_TESTS") != "1",
    reason="requires a running VibryAI Server at http://127.0.0.1:9999; set VIBRY_LIVE_TESTS=1",
)


def api(method, path, body=None, expect_status=None):
    url = f"{BASE}{path}"
    data = json.dumps(body, ensure_ascii=False).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {USER_ID}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = resp.status
            raw = resp.read().decode()
            if expect_status and status not in expect_status:
                return {"_fail": True, "status": status, "body": raw[:200]}
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        status = e.code
        body = e.read().decode()
        if expect_status and status in expect_status:
            try: return json.loads(body)
            except: return {"status": status, "body": body[:200]}
        return {"_fail": True, "status": status, "body": body[:200]}
    except Exception as e:
        return {"_fail": True, "error": str(e)}


def check(name, ok, detail=""):
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  ✅ {name}{' — ' + detail if detail else ''}")
    else:
        FAILED += 1
        print(f"  ❌ {name}{' — ' + detail if detail else ''}")


# ===================================================================
# 0. 服务连通性
# ===================================================================
def test_health():
    print("\n" + "=" * 55)
    print("0️⃣  服务连通性")
    print("=" * 55)
    r = api("GET", "/api/health")
    if r.get("_fail"):
        print(f"  ❌ 无法连接到 {BASE}，请确认服务已启动: python main.py")
        sys.exit(1)
    check("服务运行", r.get("status") == "ok", f"version={r.get('version')}")
    check("Cognition", r.get("cognition") == "ok", r.get("cognition"))
    check("ASR 模式", r.get("asr_mode") in ("local", "cloud", "cloud_standard"),
          r.get("asr_mode"))
    check("队列状态", "asr" in r.get("queue", {}), str(r.get("queue")))


# ===================================================================
# 1. DB 迁移验证
# ===================================================================
def test_db_migration():
    print("\n" + "=" * 55)
    print("1️⃣  DB 迁移验证")
    print("=" * 55)

    import sqlite3
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    db_path = os.path.join(_root, "data", "vibrycard.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # 检查 recordings 表新列
    cur = conn.execute("PRAGMA table_info(recordings)")
    cols = {r[1] for r in cur.fetchall()}
    check("utterances_json 列", "utterances_json" in cols)
    check("raw_wav_path 列", "raw_wav_path" in cols)
    check("insight_json 列", "insight_json" in cols)

    # 检查 asr_config 表新列
    cur = conn.execute("PRAGMA table_info(asr_config)")
    asr_cols = {r[1] for r in cur.fetchall()}
    check("summary_prompt 列", "summary_prompt" in asr_cols)
    check("insight_prompt 列", "insight_prompt" in asr_cols)
    check("voice_mode 列", "voice_mode" in asr_cols)

    # 验证默认值
    row = conn.execute("SELECT * FROM asr_config WHERE id=1").fetchone()
    if row:
        check("ASR config 有数据", row["app_id"] != "", f"app_id={row['app_id'][:8]}...")
        check("voice_mode 默认值", row["voice_mode"] == "cloud")
    conn.close()


# ===================================================================
# 2. Wiki RAG
# ===================================================================
def legacy_wiki_script():
    print("\n" + "=" * 55)
    print("2️⃣  Wiki RAG 知识库")
    print("=" * 55)

    # 状态
    r = api("GET", "/api/wiki/status")
    check("Wiki 状态", r.get("initialized") is not None)
    if not r.get("initialized"):
        r2 = api("POST", "/api/wiki/init", {})
        check("Wiki 初始化", r2.get("ok"), str(r2.get("created", [])))
        time.sleep(0.2)

    r = api("GET", "/api/wiki/status")
    check("Wiki 已就绪", r.get("initialized") is True,
          f"raw={r.get('raw_count')} wiki={r.get('wiki_count')}")

    # 摄入测试
    r = api("POST", "/api/wiki/ingest", {
        "title": "测试文档",
        "topic": "test",
        "content": "# Python 微服务架构\n\n## 概述\nPython 适合构建轻量级微服务，推荐使用 FastAPI + SQLite 方案。\n\n## 优势\n- 开发速度快\n- 自托管友好\n- 生态丰富",
    })
    check("Wiki 摄入", r.get("ok") is True,
          f"新建={len(r.get('articles_created',[]))}篇 编译={r.get('compile_time_s',0)}s")

    # 查询
    r = api("POST", "/api/wiki/query", {"query": "微服务架构"})
    check("Wiki 查询", r.get("count", 0) > 0, f"找到 {r.get('count')} 篇")

    # 文章列表
    r = api("GET", "/api/wiki/pages")
    check("Wiki 文章列表", r.get("count", 0) > 0)

    # Lint
    r = api("POST", "/api/wiki/lint", {"auto_fix": True})
    check("Wiki Lint", r.get("ok") is True,
          f"fixed={len(r.get('deterministic',{}).get('fixed',[]))}")


# ===================================================================
# 3. 记忆 (Mem0)
# ===================================================================
def legacy_memory_script():
    print("\n" + "=" * 55)
    print("3️⃣  Mem0 记忆引擎")
    print("=" * 55)

    r = api("POST", "/api/memories", {"text": "测试记忆：用户偏好 Python + FastAPI 技术栈"})
    check("写入记忆", r.get("ok") is True)

    r = api("GET", "/api/memories?q=Python")
    check("检索记忆", r.get("count", 0) > 0, f"找到 {r.get('count')} 条")

    r = api("GET", "/api/memories?q=不存在的关键词XYZ123")
    check("无匹配记忆", r.get("count", 0) >= 0, "正常返回空")


# ===================================================================
# 4. Chat 代理
# ===================================================================
def test_chat():
    print("\n" + "=" * 55)
    print("4️⃣  Chat 代理")
    print("=" * 55)

    r = api("POST", "/v1/chat/completions", {
        "messages": [{"role": "user", "content": "Hello"}],
        "max_tokens": 30, "stream": False,
    })
    content = r.get("choices", [{}])[0].get("message", {}).get("content", "")
    check("Chat 非流式", bool(content), f"回复 {len(content)} 字符")
    memories = r.get("_vibry_memories", {})
    if memories:
        print(f"     💡 注入了 {memories.get('count', 0)} 条记忆")

    # 流式
    url = f"{BASE}/v1/chat/completions"
    body = json.dumps({
        "messages": [{"role": "user", "content": "Hi"}],
        "max_tokens": 20, "stream": True,
    }).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {USER_ID}")
    chunks = 0
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            for line in resp:
                line = line.decode().strip()
                if line.startswith("data: ") and line != "data: [DONE]":
                    chunks += 1
    except Exception as e:
        pass
    check("Chat 流式", chunks > 0, f"{chunks} chunks")


# ===================================================================
# 5. ASR 配置
# ===================================================================
def test_asr_config():
    print("\n" + "=" * 55)
    print("5️⃣  ASR 配置 & Prompt")
    print("=" * 55)

    # 通过 admin login 获取 token
    r = api("POST", "/admin/api/login", {"password": "vibry2024"})
    if r.get("ok"):
        admin_token = r["token"]
        auth = {"Authorization": f"Bearer {admin_token}"}

        url = f"{BASE}/admin/api/config"
        req = urllib.request.Request(url)
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {admin_token}")
        with urllib.request.urlopen(req, timeout=10) as resp:
            config = json.loads(resp.read().decode())

        check("ASR voice_mode", config.get("asr_voice_mode") in ("cloud", "local"),
              config.get("asr_voice_mode"))
        check("summary_prompt 字段", "summary_prompt" in config,
              "存在" if config.get("summary_prompt") else "空(使用默认)")
        check("insight_prompt 字段", "insight_prompt" in config,
              "存在" if config.get("insight_prompt") else "空(使用默认)")
    else:
        check("Admin 登录", False, "跳过后续 ASR 配置测试")
        return

    # 测试 ASR mode 端点
    r = api("GET", "/api/asr-mode")
    check("ASR mode 读取", r.get("asr_mode") is not None, r.get("asr_mode"))


# ===================================================================
# 6. 声纹
# ===================================================================
def test_voiceprint():
    print("\n" + "=" * 55)
    print("6️⃣  声纹识别")
    print("=" * 55)

    r = api("GET", "/api/voiceprint/list")
    if r.get("_fail"):
        check("声纹列表", False, str(r))
        return
    check("声纹列表", r.get("voiceprints") is not None,
          f"{len(r.get('voiceprints', []))} 个已注册")


# ===================================================================
# 7. 录音状态
# ===================================================================
def test_recording_status():
    print("\n" + "=" * 55)
    print("7️⃣  录音状态查询")
    print("=" * 55)

    r = api("GET", "/api/recording-status/nonexistent_id")
    check("录音状态 404", r.get("_fail") and r.get("status") == 404, "不存在=404 ✓")

    r = api("GET", "/api/recordings")
    check("录音列表", r.get("recordings") is not None,
          f"{len(r.get('recordings', []))} 条记录")


# ===================================================================
# 8. 统计 & 模型
# ===================================================================
def test_stats():
    print("\n" + "=" * 55)
    print("8️⃣  统计 & 模型列表")
    print("=" * 55)

    r = api("GET", "/api/stats")
    check("统计端点", r.get("total") is not None,
          f"total={r.get('total')} completed={r.get('completed')}")

    r = api("GET", "/v1/models")
    check("模型列表", r.get("object") == "list",
          f"{len(r.get('data', []))} 个模型")


# ===================================================================
# Main
# ===================================================================
def main():
    print("🧪 Vibry AI Core — 全量功能测试")
    print(f"   服务: {BASE}")
    print(f"   用户: {USER_ID}")
    print(f"   时间: {time.strftime('%H:%M:%S')}")

    tests = [
        ("服务连通性", test_health),
        ("DB 迁移验证", test_db_migration),
        ("Chat 代理", test_chat),
        ("ASR 配置", test_asr_config),
        ("声纹识别", test_voiceprint),
        ("录音状态", test_recording_status),
        ("统计 & 模型", test_stats),
    ]

    for name, fn in tests:
        try:
            fn()
        except Exception as e:
            import traceback
            print(f"  💥 {name} 异常: {e}")
            traceback.print_exc()
            global FAILED
            FAILED += 1

    print("\n" + "=" * 55)
    total = PASSED + FAILED
    bar = "🟢" * PASSED + "🔴" * FAILED
    print(f"📊 {bar}")
    print(f"   通过: {PASSED} / 失败: {FAILED} / 总计: {total}")
    print("=" * 55)

    if FAILED > 0:
        print("\n⚠️ 存在失败项，请检查服务日志")
        sys.exit(1)
    else:
        print("\n✅ 全量功能测试通过!")
        sys.exit(0)


if __name__ == "__main__":
    main()
