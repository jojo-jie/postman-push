# postman-push

将项目 API 自动同步到 Postman Collection 的 Agent Skill。适用于 Cursor、Codex、Claude 等 AI 编程工具，在代码变更后一键更新 Postman 文档，减少手工维护成本。

## 功能

- **自动检测 Postman MCP** — 识别当前 AI 宿主工具的 Postman MCP 配置
- **多源 API 发现** — 优先解析 OpenAPI/Swagger，回退到框架路由与 Controller
- **增量 / 全量同步** — 默认基于 Git diff 保守增量更新，也可显式全量扫描
- **多 Collection 映射** — 按 internal / open 等规则自动选择目标 Collection
- **双通道写入** — Agent 可优先走当前会话的 Postman MCP；本地脚本提供 Postman HTTP API 回退

## 适用场景

- 将路由、Controller 或 OpenAPI 变更同步到 Postman
- 只更新最近改动的 API 端点
- 区分内部 API 与对外 API，写入不同 Collection
- 在 AI 对话中说「更新 Postman 文档」「同步 collection」即可触发

## 前置要求

- Python 3.10+
- 目标项目已配置 Postman Workspace 与 Collection
- （可选）Postman MCP 或 `POSTMAN_API_KEY`

## 环境变量

在目标项目的 `.env`、`.env.local` 或 `.env.development` 中配置：

```env
POSTMAN_WORKSPACE_ID=your-workspace-id

# 可配置多个 Collection，后缀为逻辑名称
POSTMAN_COLLECTION_ID_OPEN=your-open-collection-id
POSTMAN_COLLECTION_ID_OPEN_HOST=https://api.example.com

POSTMAN_COLLECTION_ID_INTERNAL=your-internal-collection-id
POSTMAN_COLLECTION_ID_INTERNAL_HOST=https://internal.example.com

# MCP 不可用时作为 HTTP API 回退
POSTMAN_API_KEY=your-postman-api-key
```

Collection 选择规则：

1. 用户显式指定 Collection key 时优先使用
2. 对外 API 优先匹配含 `open`、`public`、`external` 的 key
3. 内部 API 优先匹配含 `internal`、`admin`、`private` 的 key
4. 无明确匹配时，启发式选择第一个 open/public Collection

## 作为 Agent Skill 使用

将本仓库放入 Agent 的 skills 目录，例如：

```text
~/.cursor/skills/postman-push/
~/.claude/skills/postman-push/
~/.codex/skills/postman-push/
```

在对话中直接说明需求即可，例如：

- 「把最近改动的 API 同步到 Postman」
- 「全量更新 Postman collection」
- 「把对外接口推到 open collection」

Agent 会按 `SKILL.md` 中的工作流自动执行检测、发现与推送。有 Postman MCP 写入工具时由 Agent 直接调用 MCP；`scripts/postman_push.py` 只负责 HTTP API 回退。

## 命令行脚本

也可在目标项目根目录手动运行：

### 1. 检测 Postman MCP 配置

```bash
python3 path/to/postman-push/scripts/detect_postman_config.py --json
```

### 2. 发现 API

增量模式（默认）：

```bash
python3 path/to/postman-push/scripts/discover_apis.py \
  --repo "$PWD" \
  --mode incremental \
  --json
```

默认增量模式不会在 Git diff 证据不足时自动扫描整个项目。若确认可以扩大扫描范围，可显式开启全量兜底：

```bash
python3 path/to/postman-push/scripts/discover_apis.py \
  --repo "$PWD" \
  --mode incremental \
  --allow-full-fallback \
  --json
```

全量模式：

```bash
python3 path/to/postman-push/scripts/discover_apis.py \
  --repo "$PWD" \
  --mode full \
  --json
```

限定模块或路径：

```bash
python3 path/to/postman-push/scripts/discover_apis.py \
  --repo "$PWD" \
  --mode incremental \
  --include routes/users.py \
  --json
```

### 3. 推送到 Postman

```bash
python3 path/to/postman-push/scripts/postman_push.py \
  --repo "$PWD" \
  --mode incremental \
  --host-tool auto
```

默认不联网、不写远端，只输出本地 dry-run 摘要。若要拉取已有 Collection 并查看新增/更新数量：

```bash
python3 path/to/postman-push/scripts/postman_push.py \
  --repo "$PWD" \
  --mode incremental \
  --preview
```

确认后写入 Postman：

```bash
python3 path/to/postman-push/scripts/postman_push.py \
  --repo "$PWD" \
  --mode incremental \
  --apply
```

如果增量模式没有发现 API，推送脚本会返回 `result: "no-op"`，不会写远端。若需要在推送流程中允许全量兜底，同样传入 `--allow-full-fallback`。

## 同步行为

- 增量模式默认只处理显式 include 或 Git diff 相关接口，避免静默扩大同步范围
- `--preview` 会输出 created / updated / preserved 计数；`--apply` 才会执行远端写入
- 按 `METHOD + path` 匹配已有请求，存在则更新，不存在则追加
- 路径参数 `:id`、`{id}`、`{{id}}` 视为等价
- 保留 Collection 中无关请求、已保存响应及元数据
- 每个端点尽量包含：方法、路径、Host、Query/Path 参数、Headers、Body 字段与描述
- 描述优先来自 OpenAPI、路由注释、Validator/DTO；推断内容会标注为 inferred

## 项目结构

```text
postman-push/
├── SKILL.md                      # Agent Skill 主指令
├── README.md
├── scripts/
│   ├── detect_postman_config.py  # 检测 Postman MCP 与凭证
│   ├── discover_apis.py          # API 发现，输出标准化 JSON
│   ├── env_utils.py              # 共享 .env 解析逻辑
│   └── postman_push.py           # 合并写入 Postman Collection
├── references/
│   ├── mcp-detection.md          # 各宿主 MCP 检测策略
│   └── api-discovery.md          # API 发现规则与限制
└── evals/
    └── evals.json                # Skill 评估用例
```

## 无本地脚本时的 Cursor MCP 直连模式

若目标仓库未包含上述 `scripts/`，Agent 会改用 Postman MCP 工具直接操作：

1. 读取项目 `.env*` 解析 Workspace / Collection / Host
2. 以 OpenAPI 为优先数据源
3. 调用 `getCollections`、`createCollectionRequest`、`updateCollectionRequest` 等 MCP 工具 upsert 端点

## License

MIT
