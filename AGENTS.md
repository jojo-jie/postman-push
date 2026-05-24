# AGENTS.md

<!-- AGENTS-MD-SYNC:START -->
## Agent 工作约定（自动维护）

- 最近生成时间（UTC）：`2026-05-24 03:47:10Z`
- 生成脚本：`agents-md-sync/scripts/sync_agents_md.py`
- 官方参考：`https://agents.md/`，`https://github.com/agentsmd/agents.md`

### 项目概览
- 项目名称：`postman-push`
- 检测到的技术栈：`Python`
- 检测到的工作区包数量：`0`
- 仓库结构提示：`evals, references, scripts`

### 指令优先级
- 用户在对话中的明确指令优先于本文件。
- 处理局部目录时，优先遵循目录树中最近的 `AGENTS.md`。
- README 和贡献文档作为补充上下文；如有冲突，以更高优先级指令为准。

### 开发环境提示
- 除非下方命令另有说明，默认在当前适用范围根目录执行命令。
- 这是 Python Agent Skill 仓库，无 package manager；脚本位于 `scripts/`，用 `python3` 直接运行。
- 核心工作流见 `SKILL.md`：`detect_postman_config.py` → `discover_apis.py` → `postman_push.py`。
- push 脚本应在目标 API 项目根目录执行（传入 `--repo`），而非在本 skill 仓库内。
- 修改行为后，参考 `evals/evals.json` 中的用例做回归验证。

### 常用命令
- 除非特别说明，以下命令都在当前适用范围根目录执行。
- 未自动检测到标准的 install/dev/build/typecheck/test/lint/format 命令。

### 测试说明
- 针对本次修改的文件运行最相关的检查。
- 修复由本次修改引入的测试、类型、lint 和格式化失败。
- 行为发生变化时，补充或更新测试。

### 代码风格
- 遵循所编辑文件的既有代码风格。
- 修改范围保持聚焦，避免无关重写或大范围格式化噪音。
- 保持 Python 3.10+ 语法（dataclass、type hints、`from __future__ import annotations`）。
- 脚本应支持 `--json` 输出，便于 Agent 自动化解析。
- 变更聚焦 skill 行为，避免无关 refactor 或大范围格式化。

### 安全注意事项
- 不提交密钥、令牌、凭据或本地环境文件。
- 在信任边界校验输入，并保留既有鉴权检查。
- 不得提交 `POSTMAN_API_KEY` 或目标项目 `.env` 中的凭证。
- 不要将 MCP bearer token 与 Postman API key 混用。

### 约束规则
- 优先遵循用户指令，其次遵循离当前目录最近的 AGENTS.md 规则。
- 修改范围保持聚焦，避免顺手做无关重构。
- 避免直接编辑生成物或依赖目录：`node_modules, dist, build, .git, __pycache__`
- 行为发生变化时，补充或更新测试。
- 当子目录工作流明显分化时，再补充嵌套的 AGENTS.md。

### PR / 交付说明
- 交付前说明行为变化、已执行验证和已知缺口。
- 说明对 `SKILL.md` 工作流或 `scripts/` 行为的改动。
- 若变更 API 发现或推送逻辑，同步更新 `references/` 中对应文档。
- 提交前手动运行相关脚本的 `--json` 输出做 smoke test。

### 验证清单
- 交付前运行最相关的 build/typecheck/test/lint/format 命令；如果没有标准命令，就执行最接近的可用验证步骤。
- 明确说明已验证和未验证的内容。
- 确认 `scripts/*.py` 在 `python3` 下可正常执行 `--help`。
- 若修改同步规则，检查 `evals/evals.json` 用例是否仍匹配预期。
<!-- AGENTS-MD-SYNC:END -->

## 项目说明（手动维护）
- 本仓库 `README.md` 面向人类用户；`SKILL.md` 是 Agent 执行主指令。
- 与用户沟通时优先使用简体中文。
