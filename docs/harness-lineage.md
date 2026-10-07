# 与开源智能体框架的关系

本项目是独立实现的 Python 包，**没有复制或依赖** grok-cli、Codex、DeepSeek Harness、ZCode 的源代码；它借鉴这些框架的工程做法，并把它们放进公文场景的约束之下。下表说明借鉴了什么、改成了什么、为什么。

## 一、借鉴对照

| 来源 | 做法 | 本项目中的对应 | 公文场景的改动 |
|---|---|---|---|
| **DeepSeek Harness**（Cordis） | 一切皆插件：模型、工具、技能、记忆、执行循环、权限都可替换；依赖就绪才激活；副作用可回收 | `kernel/context.py`（服务注入、事件、LIFO 回收）、`runtime.py` 插件装配、`[[plugins]]` 配置 | 插件可以替换检索器、追加规则，但**不能改变状态机与人工审核节点**——这些不是插件 |
| DeepSeek Harness | 追加式会话日志，可恢复、分叉、回放 | `harness/session.py`；`gongwen session verify/replay/fork` | 加哈希链防篡改；长字段只记哈希与摘要（设计 §7.3 最小化） |
| **Codex** | `AGENTS.md` 分层合并、`AGENTS.override.md` 覆盖 | `harness/instructions.py`：`GONGWEN.md` / `AGENTS.md` / `GONGWEN.override.md` 从仓库根合并到当前目录 | 说明文件只能补充单位惯例，权限仍由执行层决定 |
| Codex | 审批策略 untrusted / on-request / never | `harness/approval.py` | 区分“工具动作审批”和“办文审核节点”：任何审批策略都不能跳过人工送审 |
| Codex | `codex exec` 无头执行、`-c key=value` 覆盖、配置档 profile、`config.toml` 分层 | `gongwen exec --json`、`-c`、`-p`、`~/.gongwen/config.toml` < 工作区 < 配置档 < 环境变量 < 命令行 | 无头模式只能自动接受四类可接受节点，退出码区分“停在人工节点” |
| Codex | MCP 服务模式；`.agents/skills` 技能目录 | `gongwen mcp`；`gongwen skills install --dest .agents/skills` | MCP 客户端是模型通道，不获得人工权限 |
| **grok-cli** | 交互式代理 + 无头模式、子代理、钩子（PreToolUse 退出码 2 阻断）、设置分层 | `gongwen chat`（模型通道 + 人工斜杠命令）、`harness/hooks.py`（同样的退出码约定）、`integrations/hooks/` | 斜杠命令是人的通道：模型输出不会被解析成命令 |
| grok 系列 | MCP 工具以 `<server>__<tool>` 命名、`grok mcp add` | `integrations/grok/` 接入说明 | — |
| **ZCode** | （教训）代码库索引功能把本地工作区快照直接上传云存储、绕过服务端审计 | `harness/egress.py` 出网网关作为唯一出口、默认拒绝、每次判定留痕；准入先于模型；没有任何后台上传功能 | 数据目录建议放在宿主工具工作区之外（宿主对文件的直接访问不受本系统控制） |
| ZCode / 多数编码代理 | yolo / 自动执行模式 | 不提供“跳过全部确认”的模式 | 公文场景的人工审核节点是程序强制的，不是可关闭的提示 |

## 二、为什么不直接“套壳”一个编码智能体

编码智能体的默认假设是：模型可以自由读写工作区、运行命令，人只在关键动作上批准。公文场景的假设不同：

1. **材料先准入再处理**：编码代理默认把工作区文件发给模型；公文材料要先判断能否进入当前环境（设计 §5 阶段 0、§7.1）；
2. **人工节点是流程的一部分**：任务契约、提纲与措施、人工送审都要人决定，不是“可选的批准”；
3. **事实与依据有状态**：拟议不能写成已完成，记载不能写成已核实，依据要在适用时点有效——这些需要结构化对象和确定性检查，而不是提示词；
4. **交付物是证据包**：不是一份文件，而是文稿、证据、问题、待确认事项与版本记录。

因此本项目把编码智能体的长处（插件化、日志、钩子、无头模式、MCP、技能）用在“外壳”，把办文的约束放在“内核”的状态机、权限与规则里；外部智能体通过 MCP 或 Agent Skills 使用这些能力时，也受同样的约束。

## 三、接入方式

见 [`integrations/README.md`](../integrations/README.md)。

## 参考

- DeepSeek Harness：<https://github.com/deepseek-ai/deepseek-harness>
- Codex 技能目录与 MCP 配置：<https://learn.chatgpt.com/docs/build-skills>、<https://learn.chatgpt.com/docs/extend/mcp?surface=cli>
- Grok Build MCP：<https://docs.x.ai/build/features/mcp-servers>
- grok-cli：<https://github.com/superagent-ai/grok-cli>
- ZCode 事件与开源报道：<https://www.infoq.cn/article/qEHi6k5ycwXUiasvfNKH>、<https://www.huxiu.com/article/4892970.html>
- Model Context Protocol 生命周期：<https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle>

（以上链接于 2026 年 10 月查阅；各工具配置格式以其当前版本文档为准。）
