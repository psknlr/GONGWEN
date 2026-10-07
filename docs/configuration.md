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
provider = "offline"            # 预设名或别名，见下文“三、模型接入”（gongwen model presets）
name = ""
base_url = ""
api_key_env = ""                # 从哪个环境变量读取密钥（密钥不写入配置文件）
max_clearance = "公开"          # 允许发送给该模型的最高材料属性
timeout = 90                    # 单次请求超时（秒）；Claude 适配使用流式，按两次数据之间的间隔计
max_retries = 2                 # 限流、服务端错误、连接失败与超时的自动重试次数

[models.reviewer]               # 具名模型，供路由使用（推荐用 gongwen model add 生成）
provider = "zhipu"
name = "glm-4.6"
api_key_env = "ZHIPUAI_API_KEY"
# thinking = false              # 思考开关（智谱 GLM）；不写则按服务商默认
# json_mode = ""                # 留空按预设；json_schema / json_object / none（只靠提示约束）
# extra_body = {}               # 附加请求体字段（OpenAI 兼容接口），用于服务商新增参数

[routing]                       # 设计 §8.2：按任务复杂度路由；是否分模型应经评测证明收益
light = ""                      # 分类、抽取
heavy = ""                      # 起草、修订
reviewer = "reviewer"           # 独立审校（建议不同模型或至少独立上下文）
agent = ""                      # 对话代理（gongwen chat）；不写则使用 [model]

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

## 三、模型接入

不配置模型时系统走完整的确定性路径（零出网）。接入外部模型后，起草、审校、事实抽取与对话代理可调用模型，但所有输出仍须经确定性校验，越过证据的内容一律拒绝。

### 1. 三步接入

```bash
export DEEPSEEK_API_KEY=…                     # 密钥只放在环境变量里，不写入任何配置文件
gongwen model add ds --preset deepseek --role heavy --allow-egress
gongwen model test ds                         # 一次最小的受控调用：密钥、出网、型号、JSON 模式
```

`gongwen model add` 把 `[models.ds]` 写入工作区 `.gongwen/config.toml`（保留原有注释与其他设置）、在 `[routing]` 中引用它；只有加 `--allow-egress` 才把服务主机加入 `[egress] allowed_hosts`，否则打印应加入的那一行并说明调用会被出网网关拒绝。配置中只记录**环境变量名**（`api_key_env`），从不记录密钥本身；把密钥误当作变量名传入时会被拒绝。

更多示例：

```bash
# Claude：强模型起草、轻量模型抽取（须 pip install "gongwen[anthropic]"）
export ANTHROPIC_API_KEY=…
gongwen model add claude --preset claude --role heavy,reviewer --allow-egress     # claude-opus-5-5
gongwen model add haiku --preset claude --role light --allow-egress               # 只用于 light 时取 claude-haiku-4-5

# GPT（推理模型）作为对话代理
export OPENAI_API_KEY=…
gongwen model add gpt --preset gpt --role agent --allow-egress                    # gpt-6.1-sol

# 智谱 GLM 国际站（Z.ai），关闭思考模式
export ZHIPUAI_API_KEY=…
gongwen model add glm --preset glm --region intl --thinking off --role reviewer --allow-egress

# MiniMax 中国大陆站 / 国际站
export MINIMAX_API_KEY=…
gongwen model add mm --preset minimax --role heavy --allow-egress                 # api.minimaxi.com
gongwen model add mmi --preset minimax_intl --role heavy --allow-egress           # api.minimax.io

# 本机 Ollama（本机回环地址免列白名单）
gongwen model add local --preset ollama --model qwen3:32b --role all

gongwen model list                  # 已配置的模型、角色、密钥是否已设置（只显示是/否）、出网是否允许、材料上限
gongwen model remote ds             # 查询服务商当前可用的型号（GET /models），* 标出配置中使用的型号
gongwen model presets               # 全部预设、区域地址、已知型号与接口差异
```

角色：`heavy` 起草与修订，`light` 分类与抽取，`reviewer` 独立审校，`agent` 对话代理（`gongwen chat`），`all` 表示全部；未路由的角色使用 `[model]`。`--model` 覆盖默认型号，`--base-url` 或 `--region cn|intl` 覆盖地址，`--api-key-env` 覆盖密钥变量名，`--max-clearance` 设置材料上限（默认“公开”），`--force` 覆盖同名条目。`gongwen doctor` 也会列出每个已配置模型的密钥与出网状态。

### 2. 预设

| 服务商 | 预设（别名） | 默认地址 | 密钥环境变量 | 默认型号（强 / 轻） | 说明 |
|---|---|---|---|---|---|
| Claude | `anthropic`（`claude`） | `https://api.anthropic.com` | `ANTHROPIC_API_KEY` | `claude-opus-5-5` / `claude-haiku-4-5` | 官方 SDK；结构化输出（json_schema）与严格工具；不发送采样参数；`claude-sonnet-5-5` 可用 `--model` 指定 |
| GPT | `openai`（`gpt`） | `https://api.openai.com/v1` | `OPENAI_API_KEY` | `gpt-6.1-sol` / `gpt-6-luna` | 推理模型：发送 `max_completion_tokens`、不发送 `temperature`；JSON 用 json_schema；另有 `gpt-6-astra` |
| DeepSeek | `deepseek` | `https://api.deepseek.com` | `DEEPSEEK_API_KEY` | `deepseek-chat` | `deepseek-reasoner`：忽略 `reasoning_content`，不发送 `response_format` 与 `temperature` |
| 智谱 GLM | `zhipu`（`glm`） | `https://open.bigmodel.cn/api/paas/v4`；国际站 `--region intl`：`https://api.z.ai/api/paas/v4` | `ZHIPUAI_API_KEY` | `glm-4.6` / `glm-4.5-air` | `--thinking on` / `off` 设置请求体 `thinking`；temperature 限定 0.01–1；GLM-5.x、GLM-4.7 系列的型号编码以 `gongwen model remote` 为准 |
| MiniMax | `minimax`（`minimax-cn`）、`minimax_intl` | `https://api.minimaxi.com/v1`（中国大陆站）、`https://api.minimax.io/v1`（国际站） | `MINIMAX_API_KEY` | `MiniMax-M2.7` / `MiniMax-M2.7-highspeed` | temperature 限定 (0, 1]；JSON 只靠提示约束；去除思考标签；另有 `MiniMax-M3` 等 |
| 通义千问 | `qwen` | `https://dashscope.aliyuncs.com/compatible-mode/v1`；国际站 `--region intl` | `DASHSCOPE_API_KEY` | `qwen-plus` | 兼容模式接口 |
| Kimi | `moonshot`（`kimi`） | `https://api.moonshot.cn/v1`；国际站 `--region intl`：`https://api.moonshot.ai/v1` | `MOONSHOT_API_KEY` | `moonshot-v1-32k` | Kimi K2 等新型号以 `gongwen model remote` 为准 |
| xAI Grok | `xai`（`grok`） | `https://api.x.ai/v1` | `XAI_API_KEY` | `grok-4` | |
| 本地 | `ollama`、`vllm` | `http://localhost:11434/v1`、`http://localhost:8000/v1` | — | 须 `--model` 指定 | 本机回环地址免列白名单；局域网主机须列入 |
| 其他兼容接口 | `openai_compat` | 须 `--base-url` | `OPENAI_COMPAT_API_KEY`（可改） | 须 `--model` 指定 | 接口差异用 `json_mode`、`extra_body` 调整 |

**型号名称会随服务商更新**，表中默认值只是起点：添加后用 `gongwen model remote <名称>` 查询服务商当前可用的型号，再用 `gongwen model add <名称> --preset … --model <型号> --force` 修改。没有提供型号列表接口的服务会如实说明，并列出预设中记录的已知型号供参考。地址、型号、密钥变量都可在配置中覆盖，服务商调整接口时不必等待本项目更新。旧配置中的 `provider = "zhipu"`、`"deepseek"` 等写法照常可用。

### 3. 接口差异的处理

- **输出上限与采样参数**：推理模型（如当前 GPT）使用 `max_completion_tokens`，不发送 `temperature`；有取值范围限制的服务（MiniMax、智谱、Kimi、通义）发送前把 `temperature` 夹到允许区间。
- **JSON 模式**：按预设使用 `json_schema`、`json_object`，或不发送 `response_format`（只靠提示约束与宽容解析）；无论哪种，提示中都给出 JSON Schema，输出还要经本地的结构核对。服务端以 HTTP 400 明确拒绝 `response_format`（或 `temperature`、`max_tokens`）时，去掉（或改名为 `max_completion_tokens`）后重试一次，并在本次运行内记住，不再发送。
- **思考内容**：推理模型返回的 `reasoning_content` 一律忽略——不作为答案、不参与 JSON 解析、不写入审计日志；正文中夹带的 `<think>…</think>`、`<thinking>…</thinking>`（如早期 MiniMax M2、本地推理模型）在解析前去除。推理模型输出较慢、思考也占用输出额度，`model test` 的耗时可作参考。
- **Claude**：结构化输出与工具使用严格模式；Haiku 4.5 等不接受 `effort` 的旧型号不发送 `effort`，也不请求服务端拒答回退。

### 4. 材料属性与出网

公共云模型的 `max_clearance` 保持“公开”（`model add` 的默认值）：公开材料研发版路线下出网网关只放行公开材料；提高上限须有单位制度依据，并只用于本地部署或经单位批准的服务。使用任何外部模型都要把主机加入 `[egress] allowed_hosts`（`--allow-egress` 或手工加入）；只有本机回环地址（`localhost`、`127.0.0.1`、`::1`）免列白名单，局域网主机（包括 `*.local`）同样要列入。`gongwen model test` 与 `gongwen model remote` 也经过出网网关、预算与审计（审计写入数据目录 `audit/model-tests.jsonl`，只记哈希），未获准的主机在发出任何请求之前即被拒绝。

适配层只向网关核准的地址发送请求：不跟随 HTTP 重定向（重定向按调用失败处理）；本机地址不经环境变量中的 `HTTP(S)_PROXY` 代理。错误信息中出现的密钥一律打码。

### 5. 调用失败的处理

| 情形 | 处理 |
|---|---|
| 未配置模型、密钥变量未设置、网关不允许出网 | 显示“未就绪”或拒绝原因，技能走确定性路径；`model test` 给出 `export` 或白名单写法 |
| 模型拒答 | 审计记录 `model.response`（refused），技能记录 `skill.model_skipped` 后回退确定性路径 |
| 输出不是 JSON，或结构与约定不符（如段落不是对象） | 技能记录 `skill.model_skipped` 后回退确定性路径 |
| 限流、服务端错误、连接失败、超时（已按 `max_retries` 重试）、重定向、响应报文异常 | 审计记录 `model.error`；办文流程中的模型步骤改用确定性路径，任务提示写明“模型接口调用失败”；按修改意见修订转人工处理；对话模式报告后可继续输入 |
| `model test` 遇到 HTTP 401 / 403 / 404 / 超时 | 分别提示密钥无效、无权访问（未开通型号或额度不足）、型号或地址不存在（用 `model remote` 核对）、网络或服务过慢（可调大 `timeout`） |

Anthropic 适配默认启用服务端拒答回退（beta `server-side-fallback-2026-07-01`，`fallbacks="default"`），并在读取内容前检查 `stop_reason == "refusal"`；拒答会显式记录，不当作空结果成功。请求使用流式并取最终消息，长输出不会因非流式请求在生成完成前超时。对话模式的系统提示与工具集在会话内保持不变、历史只追加（回传的思考块绑定此前的会话前缀）；`api_key_env` 显式指定的变量未设置时视为配置错误，使用默认的 `ANTHROPIC_API_KEY` 时也可由 SDK 的其他凭据来源提供。

## 四、单位配置档

`src/gongwen/profiles/`：`party_gov`（党政机关，依照条例）、`hospital`、`university`、`research_institute`（参照条例）。每个配置档包含：适用主体类型、可用文种、办公室名称、内设机构对外行文规则、单位程序（触发关键词 → 程序名称）、自称模式（我委/我院/我校）、上级与平级机关（用于越级、平行行文判断）。

## 五、说明文件

工作区根目录的 `GONGWEN.md`（或 `AGENTS.md`）会从仓库根到当前目录逐级合并，子目录可用 `GONGWEN.override.md` 覆盖。只写本单位制度与惯例，不写具体事实数据；它补充提示词，但不改变权限。
