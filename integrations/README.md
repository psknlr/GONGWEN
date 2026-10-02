# 与开源智能体框架的集成

公文智能体以三种方式接入其他智能体框架，都**不改变**其治理边界：

| 方式 | 适用 | 入口 |
|---|---|---|
| MCP 服务（stdio） | Codex、Grok Build、grok-cli、Claude Code、ZCode 等支持 MCP 的客户端 | `gongwen -C <工作区> mcp` |
| Agent Skills | 支持 `SKILL.md` 的客户端（Codex/grok-cli：`.agents/skills`；Claude Code：`.claude/skills`） | `gongwen skills install --dest <目录>` |
| 机器可读命令行 | 流水线、其他 harness 的工具插件 | `gongwen exec … --json`（NDJSON）、`gongwen check … --json` |

## 治理边界（所有接入方式相同）
- 外部客户端以 `channel:mcp` 身份调用，只能推进、查询、检索、检查、提交修改建议；
- 任务确认、提纲确认、材料准入、权限确认、人工送审、审批导入、采纳修改建议只能由人在终端
  （`gongwen task …`、`gongwen chat` 中的斜杠命令）或本地工作台（`gongwen serve`）完成；
- 工具结果会进入客户端自己的模型，默认只返回由“公开”材料形成的内容（`[mcp] max_clearance`）；
- 宿主工具对工作区文件的直接访问不受 gongwen 控制，请把数据目录放在宿主工作区之外（见 `zcode/README.md`）。

## 目录
- `codex/`：`config.toml` 片段、`AGENTS.md` 示例
- `grok/`：Grok Build 的 `config.toml` 片段；grok-cli 接入说明
- `claude-code/`：项目级 `.mcp.json`
- `deepseek-harness/`：设计对照与接入方式
- `zcode/`：安全教训与接入方式
- `hooks/`：PreToolUse 钩子示例（退出码 2 阻断）
