# VibryAI Server

VibryAI Server 是 Vibry.AI 个人认知内核的服务器实现，同时提供
VibryCard 语音处理、项目工作区、证据化记忆、夜间洞察、Dashboard、
OpenAI 兼容网关和 MCP 接入。

## 当前边界

- **VibryAI Server**：唯一认知数据源、AI 网关和管理后台。
- **VibryCard**：录音采集客户端，通过兼容接口上传、转写和读取结果。
- **外部 Agent**：通过 REST、OpenAI API、MCP 或显式文件同步插件接入。
- **单用户**：所有业务调用固定归属认知所有者 `admin`。

运行时不再依赖 Mem0、Qdrant、Wiki 或独立 RAG 服务。专业资料、会议、
Agent 历史和用户输入统一进入 Source/Claim/Project/Insight 认知链路。

## 认知链路

1. **L0 Source**：录音转写、文本、文档文本、聊天和 Agent 历史。
2. **L1 Claim**：四网络主张、实体、时间、置信度和证据片段。
3. **L2 Project**：Goal、背景、多项目归属、对话、任务和操作日志。
4. **L3 Insight**：按项目手动或夜间生成的证据化洞察。
5. **L4 Memory Matrix**：跨项目认知、确认/否定和全局校准对话。

语义检索支持本地 FastEmbed `BAAI/bge-small-zh-v1.5` 或兼容的远程
Embedding Provider。Provider 不可用时任务明确失败并可重试，不使用静默
哈希降级。

## 启动

```powershell
cd D:\VibryAI\VibryServer
python run.py
```

- Dashboard：<http://127.0.0.1:9999/admin>
- OpenAPI：<http://127.0.0.1:9999/docs>
- 健康检查：<http://127.0.0.1:9999/api/health>

Linux 首次安装或从发布包更新：

```bash
sudo VIBRY_HOME=/opt/http/vibryai/server bash deploy.sh
sudo VIBRY_HOME=/opt/http/vibryai/server bash deploy.sh --update
```

`--update` 必须在新版本解压目录执行。它会保留 `.env`、`data/`、虚拟环境
和模型缓存，并在健康检查失败时回滚代码、配置和数据库。

## 核心接口

- `POST /api/transcribe`：上传音频，返回转写、录音 ID、Source 和认知任务。
- `POST /api/summarize`、`POST /api/insight`：生成单次录音纪要和即时分析。
- `POST /api/v2/sources`：写入文本、文档文本或 Agent 历史等 L0 Source。
- `GET/POST /api/v2/projects`：创建和维护项目。
- `GET /api/v2/projects/{id}/workspace`：项目完整工作区。
- `POST /api/v2/projects/{id}/chat`：带项目记忆的对话。
- `GET /api/v2/memory-matrix`、`POST /api/v2/memory-matrix/chat`：L4 全局认知与校准对话。
- `POST /api/v2/insights/run`：按项目或脏项目触发 L3 洞察。
- `POST /api/v2/context/build`：编译证据化上下文。
- `POST /v1/chat/completions`：自动注入认知上下文的 OpenAI 兼容网关。
- `GET /api/v2/operations`：任务、插件、FastEmbed 和 MCP 运行状态。

完整请求格式、认证边界和全部路由见
[API_REFERENCE.md](docs/API_REFERENCE.md)。

## Agent 接入

本地 stdio MCP：

```powershell
python mcp_server.py --user-id admin
```

当前提供：

- `vibry_search_context`
- `vibry_list_projects`
- `vibry_project_brief`
- `vibry_capture_source`

Codex/Claude Code 历史同步见
[AGENT_INTEGRATION.md](docs/AGENT_INTEGRATION.md)。

## 验证

```powershell
python -m pytest -q
```

架构与产品边界：

- [VibryAI_Product_Requirements.md](docs/VibryAI_Product_Requirements.md)
- [VibryServer_Refactor_Plan.md](docs/VibryServer_Refactor_Plan.md)
- [asr_provider_contract.md](docs/asr_provider_contract.md)
