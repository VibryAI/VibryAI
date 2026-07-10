# Vibry AI SOLO

*Digital Prefrontal Cortex — Your Personal AI Memory & Knowledge Hub*

A lightweight, self-hosted AI middleware server that sits between you and your LLMs. It injects long-term memory (Mem0) and professional knowledge (Wiki RAG) into every conversation — so every AI you use remembers you and understands your domain.

**mem0 makes the system know YOU. wiki-rag makes the system know your FIELD.**

---

## Architecture

```
┌─────────────────────────────────────────────┐
│              VIBRY AI SOLO                   │
│                                              │
│  ┌─────────┐  ┌─────────┐  ┌──────────────┐ │
│  │  Mem0    │  │  Wiki   │  │  Voiceprint  │ │
│  │ (Who)    │  │ (What)  │  │  (Who Said)  │ │
│  └────┬─────┘  └────┬─────┘  └──────┬───────┘ │
│       └──────────────┼──────────────┘         │
│                      ▼                        │
│            ┌─────────────────┐                │
│            │  Context Injector│                │
│            └────────┬────────┘                │
│                     ▼                         │
│         ┌───────────────────────┐             │
│         │  LLM Proxy (OpenAI)   │             │
│         │  Doubao / DeepSeek    │             │
│         └───────────────────────┘             │
└─────────────────────────────────────────────┘
```

## Features

| Module | Description | Status |
|--------|-------------|--------|
| 🧠 **Mem0 Memory** | Semantic long-term memory via Qdrant vector store | ✅ |
| 📚 **Wiki RAG** | Karpathy-style compiler-mode knowledge base | ✅ |
| 🎤 **ASR** | Speech-to-text: FunASR local + Doubao cloud | ✅ |
| 🗣️ **Voiceprint** | Speaker identification via MFCC embeddings | ✅ |
| 📝 **Summarization** | Meeting minutes + business insight generation | ✅ |
| 🔄 **Streaming Proxy** | OpenAI-compatible `/v1/chat/completions` | ✅ |
| 📊 **Admin Panel** | Web UI for config, memory, wiki, billing | ✅ |

## Quick Start

```bash
# Clone & deploy
git clone https://github.com/VibryAI/VibryAI.git
cd VibryAI

# Install
bash deploy.sh

# Or manually
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # Edit with your API keys
python run.py
```

Open `http://localhost:9999/admin` and log in with your admin password.

## Usage

### As an OpenAI-compatible proxy

Point any OpenAI-compatible client to `http://localhost:9999/v1`:

```python
import openai
client = openai.OpenAI(base_url="http://localhost:9999/v1", api_key="your-user-id")
response = client.chat.completions.create(
    model="gpt-4",  # Model is overridden by server config
    messages=[{"role": "user", "content": "Design a backend architecture for me"}]
)
```

Works with Cursor, LobeChat, ChatGPT Desktop, Claude Code, and any OpenAI-compatible client.

### API Endpoints

```
/v1/chat/completions     OpenAI-compatible chat proxy
/v1/embeddings           Embedding proxy
/api/transcribe          Audio file transcription
/api/transcribe/voice    Real-time voice chat ASR
/api/summarize           Meeting summary generation
/api/insight             Business insight analysis
/api/memories            Memory CRUD
/api/wiki/*              Wiki knowledge base management
/api/voiceprint/*        Voiceprint enrollment & identification
/api/recordings/*        Recording CRUD
/admin                   Admin web panel
```

## Project Structure

```
VibryAI/
├── run.py                    # Entry point
├── app/                      # FastAPI app + config
├── db/                       # SQLite connection + models
├── services/                 # Core logic
│   ├── proxy.py              # LLM proxy
│   ├── memory.py             # Mem0 engine
│   ├── wiki.py               # Wiki RAG
│   ├── asr.py                # ASR engine
│   └── voiceprint.py         # Voiceprint recognition
├── routers/                  # API route handlers
├── utils/                    # Audio, encoding utilities
├── static/                   # Admin panel HTML
├── tests/                    # Test suites
├── wiki-rag/                 # karpathy-llm-wiki SKILL.md
├── raw/                      # Wiki source materials
├── wiki/                     # Compiled wiki articles
└── voiceprints/              # Voiceprint embeddings
```

## Configuration

All settings are in `.env`. Key settings can also be changed at runtime via the admin panel.

| Variable | Description | Default |
|----------|-------------|---------|
| `UPSTREAM_BASE_URL` | LLM API base URL | Volcengine Ark |
| `UPSTREAM_API_KEY` | LLM API key | — |
| `UPSTREAM_MODEL` | Default chat model | doubao-seed-2-1-turbo |
| `WIKI_MODEL` | Wiki compilation model | deepseek-chat |
| `WIKI_API_KEY` | Wiki LLM API key | (reuses upstream) |
| `ASR_MODE` | Speech recognition mode | local |
| `ADMIN_PASSWORD` | Admin panel password | vibry2024 |
| `SERVER_PORT` | Server port | 9999 |

## Requirements

- Python 3.10+
- ffmpeg (for audio processing)
- 4GB+ RAM (for local ASR model)

## License

MIT — see [LICENSE](LICENSE)

---

# Vibry AI SOLO · 中文说明

**数字前额叶 — 你的个人 AI 记忆与知识中枢**

一个轻量级、可自托管的 AI 中间件服务器。它在你和大模型之间注入长期记忆和专业领域知识——让你用的每个 AI 都记得你、懂你的专业。

**mem0 让系统更懂你。wiki-rag 让系统更专业。**

## 核心功能

| 模块 | 说明 |
|------|------|
| 🧠 **Mem0 记忆** | 基于 Qdrant 向量库的语义长期记忆 |
| 📚 **Wiki 知识库** | Karpathy 式编译器模式 RAG，原始材料→LLM 编译→结构化 wiki |
| 🎤 **语音转写** | FunASR 本地模型 + 豆包云端 ASR（极速版/标准版·说话人分离） |
| 🗣️ **声纹识别** | MFCC 声纹提取 + 余弦相似度匹配，转写自动标注说话人 |
| 📝 **会议纪要** | 结构化摘要 + 商业洞察生成 |
| 🔄 **流式代理** | OpenAI 兼容接口，支持 SSE 流式 |
| 📊 **管理后台** | Web 界面配置模型、记忆、wiki、计费 |

## 快速开始

```bash
git clone https://github.com/VibryAI/VibryAI.git
cd VibryAI
bash deploy.sh          # 一键部署（Linux/macOS）
# 或手动: pip install -r requirements.txt && python run.py
```

打开 `http://localhost:9999/admin`，用管理员密码登录。

## 使用方式

将任意 OpenAI 兼容客户端指向 `http://localhost:9999/v1`：

- **Cursor**: Settings → Models → OpenAI → Base URL = `http://localhost:9999/v1`
- **LobeChat**: 添加 OpenAI 兼容提供商
- **ChatGPT Desktop**: 设置中配置自定义 API

每次对话会自动注入你的长期记忆和专业知识库内容。

## 项目结构

```
VibryAI/
├── run.py              # 入口
├── app/                # FastAPI + 配置
├── db/                 # 数据库连接 + 模型
├── services/           # 核心服务（代理/记忆/wiki/ASR/声纹）
├── routers/            # API 路由
├── utils/              # 工具（音频处理/编码）
├── static/             # 管理面板
├── tests/              # 测试
├── wiki-rag/           # karpathy-llm-wiki 规范
├── raw/                # Wiki 原始材料
├── wiki/               # 编译后的 wiki 页面
└── voiceprints/        # 声纹向量
```

## 配置

编辑 `.env` 文件，或在管理后台运行时修改。必填项：

- `UPSTREAM_API_KEY` — 豆包/DeepSeek API Key
- `ADMIN_PASSWORD` — 管理后台密码
- `WIKI_API_KEY` — DeepSeek API Key（Wiki 编译用，长上下文+便宜）

## 环境要求

- Python 3.10+
- ffmpeg（音频处理）
- 4GB+ 内存（本地 ASR 模型需要）
