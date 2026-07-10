# VibryAI Pro 版 — 架构蓝图

> 未来参考方案 | 多租户 · 多 Provider · 多智能体 · 插件体系

---

## 定位

Pro 版是 **中央记忆路由 API 网关**，面向团队/企业场景。上游客户端（Claude Code、ChatGPT、Doubao 等）统一经由此网关接入，网关负责记忆增强、知识注入、智能路由，然后转发到下游 LLM。

```
┌─────────────────────────────────────────────────────┐
│                   VIBRYAI PRO HUB                    │
│                                                      │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐           │
│  │ CODING   │  │ MEETING  │  │ GENERAL  │  Agent 类型 │
│  │ AGENT    │  │ AGENT    │  │ AGENT    │           │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘           │
│       │              │              │                 │
│  ┌────┴──────────────┴──────────────┴────┐           │
│  │          PLUGIN PIPELINE              │           │
│  │  Classifier → Memory → Wiki → Persona │           │
│  └────────────────┬──────────────────────┘           │
│                   │                                  │
│  ┌────────────────┴──────────────────────┐           │
│  │          PROVIDER REGISTRY            │           │
│  │  Doubao │ OpenAI │ Anthropic │ DeepSeek│          │
│  └───────────────────────────────────────┘           │
│                                                      │
│  上游 ← Claude Code / ChatGPT / Doubao / VibryCard    │
└─────────────────────────────────────────────────────┘
```

---

## 一、多 Provider 路由

### 核心概念

不再是单一上游 LLM，而是一个 **Provider 注册表**，运行时动态选择。

```python
ProviderRegistry:
  - "doubao"    → Volcengine Ark (base_url + api_key + default_model)
  - "openai"    → api.openai.com
  - "anthropic" → api.anthropic.com
  - "deepseek"  → api.deepseek.com
```

### 适配器模式

| Provider 类型 | 适配器 | 说明 |
|-------------|--------|------|
| `openai_compatible` | `OpenAICompatibleAdapter` | 覆盖 Doubao、DeepSeek、vLLM、Ollama 等 |
| `anthropic` | `AnthropicAdapter` | 独立适配：Messages API + x-api-key + 不同 SSE 格式 |

适配器负责：URL 拼接、Header 构建、Payload 翻译、SSE 归一化。

### 路由机制

- **客户端主动选择**: `X-Vibry-Provider: openai` header
- **Agent 类型默认**: coding agent 默认走 DeepSeek，meeting 走 Doubao
- **故障转移**: provider A 不可用时自动切到 provider B

---

## 二、多智能体 (Multi-Agent)

### Agent 类型

每种 Agent 有独立的**插件清单 (Plugin Manifest)**：

| Agent 类型 | 端点 | 激活插件 |
|-----------|------|---------|
| 🖥️ **Coding** | `/v1/agent/coding` | Classifier + Mem0(coding scope) + Code Wiki + Technical Persona |
| 🎙️ **Meeting** | `/v1/agent/meeting` | Classifier + Mem0(global+people scope) + Voiceprint + Meeting Persona |
| 💬 **General** | `/v1/agent/general` | Classifier + Mem0(global scope) + Wiki(all topics) + Default Persona |

### Header 动态控制

```
X-Vibry-Agent-Type: coding          # 覆盖端点默认
X-Vibry-Plugins: mem0,wiki          # 覆盖插件链
X-Vibry-Provider: deepseek          # 覆盖 provider
X-Vibry-Wiki-TopK: 5                # 调整检索数量
```

---

## 三、插件体系 (Plugin System)

### 设计原则

每个功能模块封装为独立插件，可按 Agent 类型自由组合：

```python
class BasePlugin:
    name: str           # 插件名
    priority: int       # 执行优先级 (越低越先执行)
    should_run(ctx)     # 判断是否需要执行
    execute(ctx)        # 执行：检索/注入上下文
```

### 插件清单

| 插件 | 优先级 | 职责 |
|------|-------|------|
| **ClassifierPlugin** | 0 | 查询分类 (personal/professional/coding/meeting)，指导下游插件 |
| **PersonalityPlugin** | 10 | 注入人格设定 (按 agent 类型不同) |
| **MemoryPlugin** | 20 | Mem0 检索 (按 scope 过滤) |
| **WikiPlugin** | 30 | Wiki RAG 检索 (按 topic 过滤) |
| **VoiceprintPlugin** | 25 | 说话人识别 (meeting agent 专用) |
| **MeetingContextPlugin** | 35 | 会议上下文增强 (agenda, 历史纪要) |

### 插件链执行

插件**并发执行**（asyncio.gather），互不阻塞。任一插件失败不影响其他插件（graceful degradation）。

---

## 四、多知识库 (Multi-Knowledge-Base)

### 按域隔离

不混用一个向量库，不同领域的知识存在独立的 Collection/Index：

| 知识库 | 存储 | 用途 |
|--------|------|------|
| `wiki_general` | wiki/ 文件 + 关键词索引 | 通用专业知识 |
| `wiki_coding` | wiki/ 文件 + 关键词索引 | 代码/技术栈/框架 |
| `wiki_business` | wiki/ 文件 + 关键词索引 | 商业/产品/战略 |
| `mem0_global` | Qdrant collection | 用户画像、长期偏好 |
| `mem0_coding` | Qdrant collection | 项目结构、编码习惯 |
| `mem0_people` | Qdrant collection | 同事、角色、互动历史 |
| `mem0_meeting` | Qdrant collection | 决策记录、行动项 |

### 查询分类路由

```
用户查询
  → ClassifierPlugin 分类
    → personal   → 只查 mem0_global + mem0_people
    → coding     → 只查 mem0_coding + wiki_coding
    → meeting    → 只查 mem0_meeting + mem0_people
    → professional → 只查 wiki_general + mem0_global
```

防止"语义污染"：个人闲聊不触发专业技术文档检索，反之亦然。

---

## 五、多记忆体 (Multi-Memory)

### Scope 隔离

每个 Scope 是独立的 Mem0 collection，写入时自动打标签：

```python
add_memory("用户偏好 React + TypeScript", user_id, scope="coding")
add_memory("下周和 Alice 开会讨论预算", user_id, scope="meeting")
add_memory("喜欢简洁直接的沟通风格", user_id, scope="global")
```

检索时按 Agent 类型的 Plugin Manifest 决定查哪些 Scope：

```python
# Coding Agent: 只查 coding scope
search_memories(query, user_id, scopes=["coding"])

# Meeting Agent: 查 global + people
search_memories(query, user_id, scopes=["global", "people"])
```

---

## 六、声纹识别 (Voiceprint)

### Meeting Agent 增强

```
音频输入
  → VoiceprintPlugin
    → 提取 speaker embedding (ECAPA-TDNN)
    → 匹配已注册 speaker 库
    → 输出: [{speaker: "Alice", segments: [...]}, {speaker: "Bob", ...}]
  → MemoryPlugin
    → 用 speaker 身份增强记忆检索
    → "上次 Alice 提到..." 可精确定位到 Alice 的会议记录
```

### 数据库

| 表 | 字段 |
|---|------|
| `speakers` | id, user_id, name, voiceprint(blob), samples_count |
| `speaker_segments` | recording_id, speaker_id, start_sec, end_sec, text |

---

## 七、技术栈

| 层 | 技术 |
|---|------|
| Server | Python 3.14 + FastAPI + uvicorn |
| 向量库 | Qdrant (本地) |
| 记忆引擎 | Mem0 v2 |
| 知识编译 | karpathy-llm-wiki (SKILL.md + raw/ + wiki/) |
| ASR | FunASR 本地 + 豆包云端 (极速版 + 标准版) |
| 声纹 | SpeechBrain ECAPA-TDNN (可选) |
| 数据 | SQLite (WAL 模式) |
| 管理 | Admin Panel (纯 HTML/JS SPA) |

---

## 八、与 SOLO 版的关系

| 维度 | SOLO 版 (当下开发) | Pro 版 (未来) |
|------|-------------------|--------------|
| 用户 | 单人 | 多租户/团队 |
| Provider | 1 个 (Doubao) | 多个，可动态切换 |
| Agent 类型 | 1 种 (general) | 3+ 种 (coding/meeting/general) |
| 插件 | 无插件体系，硬编码逻辑 | 可组合插件链 |
| 记忆隔离 | 无 (一个 collection) | 按 scope 隔离，多 collection |
| 知识库 | 一个 wiki | 多个 wiki，按域隔离 |
| 声纹 | 无 | 有 |
| 开源 | 是 | 否 |

SOLO 版是 Pro 版的**单用户、单 Provider、最小化**子集，验证核心模式。Pro 版在 SOLO 验证成功后，增加多租户、插件体系、多 Provider 路由等高级能力。
