# 配置

## 一、加载顺序

默认值 < 用户级 `~/.gongwen/config.toml`（可用 `GONGWEN_HOME` 改位置）< 工作区 `.gongwen/config.toml` < 配置档 `-p <名称>`（`[profiles.<名称>]`）< 环境变量 < 命令行 `-c key=value`。

```bash
gongwen init --unit-profile hospital --unit-name 示例市第一人民医院 --region 示例省
gongwen config                          # 查看生效配置
gongwen -c model.provider="deepseek" -c model.name="deepseek-chat" doctor
```

环境变量：`GONGWEN_ROUTE`、`GONGWEN_DATA_DIR`、`GONGWEN_UNIT_PROFILE`、`GONGWEN_MODEL_PROVIDER`、`GONGWEN_MODEL_NAME`、`GONGWEN_MODEL_BASE_URL`、`GONGWEN_APPROVAL_POLICY`、`GONGWEN_PROFILE`、`GONGWEN_USER`。

## 二、主要配置项

```toml
[environment]
route = "public_dev"            # public_dev / unit_approved（涉密不提供）
data_dir = ".gongwen"           # 任务数据、材料、审计日志；接入宿主智能体时建议放在其工作区之外
unit_profile = "party_gov"      # party_gov / hospital / university / research_institute
unit_name = ""
region = ""
accept_internal_materials = false  # 仅单位批准环境、经制度明确后开启

[model]                         # 默认模型；offline 表示不调用任何模型（确定性路径）
provider = "offline"            # deepseek / xai / zhipu / qwen / moonshot / ollama / vllm / openai_compat / anthropic
name = ""
base_url = ""
api_key_env = ""                # 从哪个环境变量读取密钥（密钥不写入配置文件）
max_clearance = "公开"          # 允许发送给该模型的最高材料属性

[models.reviewer]               # 具名模型，供路由使用
provider = "zhipu"
name = "glm-4.6"
api_key_env = "ZHIPUAI_API_KEY"

[routing]                       # 设计 §8.2：按任务复杂度路由；是否分模型应经评测证明收益
light = ""                      # 分类、抽取
heavy = ""                      # 起草、修订
reviewer = "reviewer"           # 独立审校（建议不同模型或至少独立上下文）

[approval]
policy = "on-request"           # untrusted / on-request / never（工具动作审批，与办文审核节点不同）
auto_accept_checkpoints = []

[egress]
allowed_hosts = []              # 例如 ["api.deepseek.com"]；本机地址之外未列出的一律拒绝
block_all = false

[budget]
max_model_calls = 60
max_revision_rounds = 2         # 设计 §5 阶段 7：自动修订先限定两轮

[layout]
profile = "gbt9704-2012"
margin_mode = "standard"        # standard / compensated（软件页边距补偿口径，实务，待核）
render_check = true             # LibreOffice 实际渲染核验

[features]                      # 消融开关（评测用）
fact_ledger = true
temporal_check = true
independent_review = true
targeted_revision = true
consistency_check = true
burden_check = true

[mcp]
max_clearance = "公开"          # 经 MCP 返回给外部客户端的内容上限

[agent]
max_turns = 16                  # 对话模式单次输入内的模型—工具往返上限

[[hooks.PreToolUse]]            # 钩子：标准输入 JSON，退出码 2 阻断
matcher = "material_add"
command = "python3 integrations/hooks/block_sensitive_names.py"

[[plugins]]                     # 追加插件：module:attr
module = "my_unit.plugins:UnitPlugin"
```

## 三、模型预设

| provider | 默认地址 | 密钥环境变量 | 默认模型 |
|---|---|---|---|
| `deepseek` | `https://api.deepseek.com` | `DEEPSEEK_API_KEY` | `deepseek-chat` |
| `xai` | `https://api.x.ai/v1` | `XAI_API_KEY` | `grok-4` |
| `zhipu` | `https://open.bigmodel.cn/api/paas/v4` | `ZHIPUAI_API_KEY` | `glm-4.6` |
| `qwen` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `DASHSCOPE_API_KEY` | `qwen-plus` |
| `moonshot` | `https://api.moonshot.cn/v1` | `MOONSHOT_API_KEY` | `moonshot-v1-32k` |
| `ollama` | `http://localhost:11434/v1` | — | 按本地模型填写 |
| `vllm` | `http://localhost:8000/v1` | — | 按部署填写 |
| `openai_compat` | 自行填写 `base_url` | `OPENAI_COMPAT_API_KEY`（可改） | 自行填写 |
| `anthropic` | `https://api.anthropic.com` | `ANTHROPIC_API_KEY` | `claude-opus-5-5`（须 `pip install "gongwen[anthropic]"`） |

模型名称与地址会随服务商更新，以服务商当前文档为准。使用任何外部模型都要：把主机加入 `[egress] allowed_hosts`；公共云模型的 `max_clearance` 保持“公开”；本地部署模型（Ollama、vLLM）在单位批准环境中可按制度提高 `max_clearance`。

Anthropic 适配默认启用服务端拒答回退（beta `server-side-fallback-2026-07-01`，`fallbacks="default"`），并在读取内容前检查 `stop_reason == "refusal"`；拒答会显式记录，不当作空结果成功。

## 四、单位配置档

`src/gongwen/profiles/`：`party_gov`（党政机关，依照条例）、`hospital`、`university`、`research_institute`（参照条例）。每个配置档包含：适用主体类型、可用文种、办公室名称、内设机构对外行文规则、单位程序（触发关键词 → 程序名称）、自称模式（我委/我院/我校）、上级与平级机关（用于越级、平行行文判断）。

## 五、说明文件

工作区根目录的 `GONGWEN.md`（或 `AGENTS.md`）会从仓库根到当前目录逐级合并，子目录可用 `GONGWEN.override.md` 覆盖。只写本单位制度与惯例，不写具体事实数据；它补充提示词，但不改变权限。
