# Claude Code 接入

1. MCP：把同目录 `.mcp.json` 放到办文工作区根目录（项目级），或运行
   `claude mcp add gongwen -- gongwen -C /path/to/workspace mcp`。
2. 技能：`gongwen skills install --dest .claude/skills`，Claude Code 会按需加载 12 项技能说明。
3. 说明文件：可把 `../codex/AGENTS.md` 的内容放进工作区的 `CLAUDE.md`。

注意：Claude Code 中的模型同样只是 MCP 客户端（channel:mcp），不能处理审核节点、导入审批、
确认材料准入或采纳修改建议；工具结果只返回“公开”材料形成的内容（可在 `[mcp] max_clearance` 调整，
须符合本单位制度）。
