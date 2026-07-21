# VibryAI Server Agent 接入

## 1. stdio MCP

MCP 适配器必须运行在能够直接访问 VibryAI Server SQLite 数据库的同一台机器，
当前不是远程 HTTP MCP。

Windows 开发环境：

```json
{
  "mcpServers": {
    "vibry-ai": {
      "command": "D:/VibryAI/VibryServer/venv/Scripts/python.exe",
      "args": ["D:/VibryAI/VibryServer/mcp_server.py", "--user-id", "admin"]
    }
  }
}
```

Linux 服务器：

```json
{
  "mcpServers": {
    "vibry-ai": {
      "command": "/opt/http/vibryai/server/venv/bin/python",
      "args": ["/opt/http/vibryai/server/mcp_server.py", "--user-id", "admin"]
    }
  }
}
```

当前提供四个工具：

- `vibry_search_context`：按查询、项目和 Token 预算检索证据化上下文；
- `vibry_list_projects`：读取项目列表；
- `vibry_project_brief`：读取 Goal、Claims、证据和洞察；
- `vibry_capture_source`：把用户批准的 Agent 结果写成 L0 Source 和持久任务。

MCP 不暴露 SQLite 接口。写入仍经过 Source、去重、Claim 提取、FastEmbed 和
项目推荐主链路。

## 2. Codex/Claude Code 历史同步

历史读取必须在本机执行。连接器只读取用户明确传入的一个 JSONL 文件，不扫描
主目录或其他会话目录。

```powershell
python D:/VibryAI/VibryServer/connectors/local_history_sync.py `
  D:/exports/codex-session.jsonl `
  --kind codex `
  --server https://api.vibry.ai `
  --token YOUR_VIBRY_TOKEN `
  --project-id prj_example
```

Claude Code 使用 `--kind claude_code`。可重复执行同一导出文件：连接器根据文件
SHA-256 生成稳定 `external_id`，服务器会返回已有 Source 而不重复写入。

当前服务器是单用户 `admin` 模型；`--user-id` 仅为兼容参数。业务 API Token
尚未在应用层强制校验，生产环境应同时依靠 HTTPS、Nginx/防火墙和可信客户端。

## 3. OpenAI 兼容网关

Agent 也可以直接调用 `/v1/chat/completions`。网关会保存对话并自动编译认知上下文。

```bash
curl https://api.vibry.ai/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'X-Vibry-Project: prj_example' \
  -H 'X-Vibry-Context-Budget: 1200' \
  -d '{"messages":[{"role":"user","content":"总结项目风险"}],"stream":false}'
```

设置 `X-Vibry-Context-Mode: none` 可关闭记忆注入，适合做对照测试。

## 4. 写入原则

- Agent 只同步用户选择的会话、文件或结果；
- Source 可同时关联多个项目；
- Agent 输出先作为证据或候选，不自动升级为用户确认事实；
- 写入后在 Dashboard 检查 Source、项目归属、Claim 和失败任务；
- 插件清单和 MCP 配置可从 `GET /api/v2/operations` 获取。
