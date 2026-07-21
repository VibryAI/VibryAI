# VibryAI Server API 参考

> 对应 VibryAI Server `1.0.0` 当前实现。代码路由是最终事实源；开发环境还可访问 `/docs` 和 `/openapi.json`。

## 1. 基础约定

- 默认地址：`http://127.0.0.1:9999`。
- 当前为单用户部署，业务数据所有者固定为 `admin`；客户端提交的 `user_id` 不创建新租户。
- JSON 请求使用 `Content-Type: application/json`；音频上传使用 `multipart/form-data`。
- 时间字段使用 ISO 8601 字符串；SQLite 兼容接口中的旧时间字段可能是本地时间字符串。
- `POST /api/v2/sources` 和 `POST /api/v2/insights/run` 返回持久任务，调用方应查询任务状态。
- `/api/audio/{recording_id}` 必须携带该录音返回的独立 `token` 查询参数。

### 认证边界

- `/admin/api/*` 配置、Token、迁移和重建接口要求管理员 Bearer Token。
- 管理员先调用 `POST /admin/api/login`，再发送 `Authorization: Bearer <token>`。
- 当前 `/api/*`、`/api/v2/*` 和 `/v1/*` 是单用户兼容接口，应用层尚未强制 API Token 校验。生产环境必须通过 Nginx、防火墙或私网限制访问。
- Dashboard 中创建的 API Token 已可管理，但目前不应被描述为业务接口的强制鉴权机制。

## 2. 健康与 OpenAI 网关

| 方法 | 路径 | 作用 |
|---|---|---|
| `GET` | `/api/health` | 服务、Chat、Embedding、ASR 和数据库状态 |
| `GET` | `/v1/models` | 当前 Chat 与 Embedding 模型 |
| `POST` | `/v1/chat/completions` | OpenAI 兼容对话；注入 Persona 和认知上下文 |
| `POST` | `/v1/embeddings` | 代理远程多模态 Embedding Provider |

`/v1/chat/completions` 支持标准非流式和 SSE 流式请求，并额外支持：

| Header | 默认值 | 说明 |
|---|---|---|
| `X-Vibry-Context-Mode` | `auto` | `auto` 自动注入；`none` 禁用认知上下文 |
| `X-Vibry-Project` | 空 | 逗号分隔的项目 ID |
| `X-Vibry-Context-Budget` | `1200` | 上下文预算，范围 200-8000 |

```bash
curl http://127.0.0.1:9999/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'X-Vibry-Project: prj_xxx' \
  -d '{"messages":[{"role":"user","content":"项目下一步做什么？"}],"stream":false}'
```

注意：Cognition 内部的本地 FastEmbed 不通过 `/v1/embeddings` 暴露；其状态通过 `/api/v2/operations` 查看。

## 3. 录音、转写与纪要

| 方法 | 路径 | 作用 |
|---|---|---|
| `GET/POST` | `/api/asr-mode` | 查看或切换 ASR Provider |
| `POST` | `/api/transcribe` | VibryCard 完整转写并保存录音 |
| `POST` | `/api/transcribe/voice` | 低延迟语音对话转写，不创建录音记录 |
| `POST` | `/api/summarize` | 根据转写生成结构化纪要 |
| `POST` | `/api/insight` | 根据转写生成即时分析 |
| `GET` | `/api/recording-status/{id}` | 查询录音处理状态 |
| `GET` | `/api/recordings` | 录音列表；支持 `status/category/limit/offset` |
| `GET/DELETE` | `/api/recordings/{id}` | 录音详情或删除 |
| `PATCH` | `/api/recordings/{id}/tags` | 更新标签和旧分类字段 |
| `GET` | `/api/audio/{id}?token=...` | 播放受 Token 保护的 WAV |
| `GET` | `/api/stats` | 当前单用户录音统计 |
| `GET` | `/api/categories` | VibryCard 兼容分类列表 |
| `GET` | `/api/v2/recordings/{id}/content` | 转写、纪要、Source、关联洞察和后台深链 |
| `PUT` | `/api/v2/recordings/{id}/projects` | 设置录音的多项目归属 |

音频上传：

```bash
curl http://127.0.0.1:9999/api/transcribe \
  -F 'audio=@meeting.wav' \
  -F 'title=产品周会' \
  -F 'category=会议'
```

也支持 JSON：

```json
{
  "audio_base64": "<base64>",
  "title": "产品周会",
  "category": "会议"
}
```

成功响应包含 `text`、`recording_id`、`audio_url`、`audio_token`、`source_id` 和 `cognition_job_id`。转写成功会创建 L0 Source；认知 Worker 随后提取 L1 Claim 和项目建议。

纪要请求：

```json
{
  "transcript": "完整转写文本",
  "record_title": "产品周会",
  "context": "可选背景"
}
```

## 4. 声纹

| 方法 | 路径 | 作用 |
|---|---|---|
| `POST` | `/api/voiceprint/enroll` | `name + audio` 注册声纹 |
| `GET` | `/api/voiceprint/list` | 声纹名称列表 |
| `DELETE` | `/api/voiceprint/{name}` | 删除声纹 |
| `POST` | `/api/voiceprint/discover` | 从带说话人分离的录音发现发言人 |
| `POST` | `/api/voiceprint/discover/enroll` | 将发言人编号绑定姓名并回写转写 |

发现请求为 `{"recording_id":"rec_xxx"}`；绑定请求为
`{"recording_id":"rec_xxx","names":{"1":"张三","2":"李四"}}`。

## 5. L0 Source 与持久任务

| 方法 | 路径 | 作用 |
|---|---|---|
| `POST` | `/api/v2/sources` | 写入 L0 Source，幂等创建处理任务 |
| `GET` | `/api/v2/sources?limit=&status=` | Source 列表 |
| `GET` | `/api/v2/sources/{id}` | Source 及其 Claims |
| `PUT` | `/api/v2/sources/{id}/projects` | 覆盖人工确认的多项目标签 |
| `GET` | `/api/v2/jobs/{id}` | 任务状态 |
| `POST` | `/api/v2/jobs/{id}/retry` | 将失败任务重新入队 |

```json
{
  "content": "已确认本季度先完成 VibryCard 1.0 发布。",
  "source_type": "manual",
  "origin": "api",
  "title": "发布决策",
  "external_id": "note:release-1",
  "occurred_at": "2026-07-17T09:00:00+08:00",
  "metadata": {},
  "project_ids": ["prj_xxx"]
}
```

同一用户下重复的 `origin + external_id` 或内容哈希会返回已有 Source，并把 `duplicate` 设为 `true`。

## 6. L2 项目工作区

| 方法 | 路径 | 作用 |
|---|---|---|
| `GET/POST` | `/api/v2/projects` | 列表或创建项目 |
| `GET/PATCH/DELETE` | `/api/v2/projects/{id}` | 详情、更新或删除项目 |
| `GET` | `/api/v2/projects/{id}/workspace` | 项目、对话、Source、任务、洞察和日志 |
| `GET` | `/api/v2/projects/{id}/brief` | Goal 与证据化 Claim/Insight 简报 |
| `POST` | `/api/v2/projects/{id}/chat` | 项目记忆对话并写入反馈 Source |
| `POST` | `/api/v2/projects/{id}/memberships` | 添加 Source/Claim/Scenario 关系 |
| `POST` | `/api/v2/projects/{id}/tasks` | 创建任务 |
| `PATCH` | `/api/v2/tasks/{task_id}` | 更新任务 |

创建项目主要字段：

```json
{
  "name": "VibryCard 1.0",
  "description": "项目背景文本",
  "goal": "完成可发布的录音与认知闭环",
  "stage": "active",
  "tags": ["产品", "录音"],
  "constraints": {},
  "metrics": {},
  "background_html": "<p>富文本背景</p>",
  "start_at": null,
  "target_at": null
}
```

删除项目会删除项目关系、项目洞察和相关任务/日志，不删除原始录音与 Source。

## 7. L3 洞察与证据

| 方法 | 路径 | 作用 |
|---|---|---|
| `GET` | `/api/v2/insights?project_id=&limit=` | 洞察列表 |
| `POST` | `/api/v2/insights/run` | 为指定项目或全部脏项目创建洞察任务 |
| `POST` | `/api/v2/feedback` | 保存修正、确认或否定事件 |

```json
{"project_id":"prj_xxx","trigger":"manual"}
```

省略 `project_id` 时，只处理状态为 dirty 的项目。洞察包含 Claim 级证据，Dashboard 可进一步打开对应会议纪要。

## 8. L4 Memory Matrix

| 方法 | 路径 | 作用 |
|---|---|---|
| `GET` | `/api/v2/memory-matrix` | 全局线程、消息和 L4 候选 |
| `POST` | `/api/v2/memory-matrix/chat` | 全局认知校准对话 |
| `POST` | `/api/v2/memory-matrix/items` | 创建 L4 候选 |
| `PATCH` | `/api/v2/memory-matrix/items/{id}` | `suggested/confirmed/rejected/superseded` 状态反馈 |

L4 内容使用 `content_html`，前端可直接富文本渲染；确认或否定会写入反馈和全局对话日志。

## 9. 上下文、Dashboard 与插件

| 方法 | 路径 | 作用 |
|---|---|---|
| `POST` | `/api/v2/context/build` | 按查询、项目和 Token 预算编译上下文 |
| `GET` | `/api/v2/dashboard` | 第二大脑统计和最新数据 |
| `GET` | `/api/v2/operations` | Job、插件、FastEmbed 和 MCP 状态 |
| `GET` | `/api/v2/plugins` | 已发现插件 Manifest |

上下文请求：

```json
{
  "query": "VibryCard 发布前还缺什么？",
  "project_ids": ["prj_xxx"],
  "limit": 12,
  "token_budget": 1200
}
```

`semantic.mode` 可能是 `fastembed_pending`、`fastembed`、`fastembed_error` 或 `remote`。

## 10. 管理后台接口

登录：

```bash
TOKEN=$(curl -s http://127.0.0.1:9999/admin/api/login \
  -H 'Content-Type: application/json' \
  -d '{"password":"<admin-password>"}' | python -c 'import json,sys; print(json.load(sys.stdin)["token"])')
```

管理页面为 `GET /admin`。当前管理 API：

| 方法 | 路径 | 作用 |
|---|---|---|
| `POST` | `/admin/api/login` | 密码或验证码登录 |
| `GET` | `/admin/api/verify` | 验证管理员 Token |
| `GET` | `/admin/api/stats` | 录音、调用、Token 与费用汇总 |
| `GET/POST` | `/admin/api/config` | 读取或更新 Chat、Embedding、ASR 和 Prompt 配置 |
| `GET` | `/admin/api/billing` | 计费汇总和最近明细 |
| `GET` | `/admin/api/logs?lines=` | 读取 `data/logs/server.log` 尾部 |
| `GET` | `/admin/api/admin-profile` | 管理员邮箱状态 |
| `POST` | `/admin/api/change-password` | 修改密码并刷新签名密钥 |
| `POST` | `/admin/api/set-email` | 设置找回密码邮箱 |
| `GET` | `/admin/api/email-config` | 邮件服务状态 |
| `POST` | `/admin/api/forgot-password` | 发送一次性验证码 |
| `POST` | `/admin/api/reset-password` | 使用验证码重置密码 |
| `GET/POST` | `/admin/api/personality` | 读取或更新全局 Persona Prompt |
| `GET` | `/admin/api/chat-history` | 查询网关对话和会话列表 |
| `GET/POST` | `/admin/api/tokens` | 列出或创建 API Token |
| `DELETE` | `/admin/api/tokens/{id}` | 删除 API Token |
| `POST` | `/admin/api/categories` | 创建 VibryCard 兼容分类 |
| `PUT/DELETE` | `/admin/api/categories/{id}` | 更新或删除兼容分类 |
| `POST` | `/admin/api/transcribe-upload` | Dashboard 上传音频并进入认知链路 |
| `POST` | `/admin/api/v2/migrations/recordings` | 导入历史录音 Source |
| `POST` | `/admin/api/v2/migrations/recording-summaries` | 导入历史结构化纪要 Source |
| `POST` | `/admin/api/v2/semantic/reindex` | 重建 Claim 向量索引 |

除登录和找回密码流程外，受保护接口使用：

```text
Authorization: Bearer <admin-token>
```

## 11. MCP

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

当前 MCP 是与服务器数据库同机运行的 stdio 适配器，不是远程 HTTP MCP。它提供上下文检索、项目列表、项目 Brief 和用户批准的 Source 写入。
