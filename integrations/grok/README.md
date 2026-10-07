# grok 系列命令行接入

## Grok Build（xAI 官方）
- MCP：见同目录 `config.toml`，或运行 `grok mcp add gongwen -- gongwen -C /path/to/workspace mcp`；
  `grok mcp list`、`grok mcp doctor gongwen` 可检查连接。工具名形如 `gongwen__task_status`。
- 依据：xAI 文档 <https://docs.x.ai/build/features/mcp-servers>（2026-10 查阅）。

## grok-cli（superagent-ai 社区版）
- MCP：在 TUI 中输入 `/mcps`，添加 stdio 服务，命令填 `gongwen`，参数填 `-C /path/to/workspace mcp`；
  也可写入项目 `.grok/settings.json` 的 `mcpServers`。该文件的字段结构以 grok-cli 当前版本为准
  （其 README 未给出完整示例），建议先用 `/mcps` 添加，再查看生成的配置。
- 技能：grok-cli 从项目 `.agents/skills/<name>/SKILL.md` 加载技能，运行
  `gongwen skills install --dest .agents/skills` 即可；TUI 中 `/skills` 查看。
- 指令：grok-cli 按 Codex 方式从仓库根到当前目录合并 `AGENTS.md`，可参考 `../codex/AGENTS.md`。
- 钩子：grok-cli 的 `hooks.PreToolUse` 与 gongwen 自身的钩子约定一致（退出码 2 阻断），
  可复用 `../hooks/` 中的脚本思路。
- 依据：<https://github.com/superagent-ai/grok-cli>（2026-10 查阅）。

## 无头模式对接
grok 的无头模式（`--prompt`）适合把 gongwen 当作 MCP 工具调用；若在流水线中直接调用 gongwen，
用 `gongwen exec … --json` 获取 NDJSON 事件流（见根目录 README）。
