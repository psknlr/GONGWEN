# DeepSeek Harness 对照与接入

## 借鉴了什么
DeepSeek Harness（`dsh`，基于 Cordis 插件框架，“一切皆插件”）把模型、工具、技能、记忆、执行循环与
权限都做成可替换插件。本项目的内核按同一思路实现（Python 版，见 `src/gongwen/kernel/context.py`）：

| DeepSeek Harness / Cordis 思路 | 本项目实现 |
|---|---|
| 服务注册与依赖注入（插件声明依赖，依赖就绪才激活） | `Context.provide/get` + `Plugin.inject`，依赖缺失时挂起 |
| 事件总线 | `Context.on/emit/bail`，钩子以 `hook/<事件>` 进程内注册 |
| 副作用可回收（卸载插件时按后进先出撤销） | `Context.effect` + `Scope.dispose`（LIFO） |
| 模型、工具、技能皆为插件 | `runtime.py` 中的 Stores/Knowledge/Governance/Skills 插件；`config.toml` 的 `plugins` 追加单位插件 |
| 追加式会话日志（恢复、分叉、回放） | `harness/session.py`：哈希链 JSONL，`gongwen session verify/replay/fork` |

不同之处：公文场景中，状态转换、人工审核节点、预算与失败退出由**程序化状态机**决定，
插件和模型都不能改变这一点（见 `orchestrator/engine.py`）。

## 如何接入
本仓库**没有**针对 dsh 插件接口的代码：其接口处于开发者预览阶段，应以官方文档为准，避免写出过时或虚构的 API。
两种稳妥方式：
1. 若所用版本支持 MCP 客户端，按其文档把 `gongwen -C <工作区> mcp` 配置为 stdio MCP 服务；
2. 在 dsh 的工具插件中以子进程调用 gongwen 的机器可读接口：
   - `gongwen check <文件> --json`：检查已有文稿；
   - `gongwen exec "<需求>" --material … --clearance 公开 --json`：无头推进，输出 NDJSON 事件流，
     最后一行 `{"type": "result", "exit_code": …}`；退出码 3 表示停在须人工处理的审核节点。

模型配置可直接使用 DeepSeek（`[model] provider = "deepseek"`，密钥取 `DEEPSEEK_API_KEY`），
并把 `api.deepseek.com` 加入 `[egress] allowed_hosts`；公共云模型的 `max_clearance` 保持“公开”。

参考：<https://github.com/deepseek-ai/deepseek-harness>（2026-10 查阅）。
