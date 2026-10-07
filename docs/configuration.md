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
timeout = 90                    # 单次请求超时（秒）；Claude 适配使用流式，按两次数据之间的间隔计
max_retries = 2                 # 限流、服务端错误、连接失败与超时的自动重试次数

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
template = ""                   # 默认公文模板（见第六节）；空为国标默认参数
font_substitution = true        # 渲染预览时未安装的公文字库以开源字体替代（只影响预览与核验，不改变 DOCX）

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

模型名称与地址会随服务商更新，以服务商当前文档为准。使用任何外部模型都要：把主机加入 `[egress] allowed_hosts`；公共云模型的 `max_clearance` 保持“公开”；本地部署模型（Ollama、vLLM）在单位批准环境中可按制度提高 `max_clearance`。只有本机回环地址（`localhost`、`127.0.0.1`、`::1`）免列白名单；局域网主机（包括 `*.local`）同样要列入。

适配层只向网关核准的地址发送请求：不跟随 HTTP 重定向（重定向按调用失败处理）；本机地址不经环境变量中的 `HTTP(S)_PROXY` 代理。

调用失败的处理：

| 情形 | 处理 |
|---|---|
| 未配置模型、密钥变量未设置、网关不允许出网 | 显示“未就绪”或拒绝原因，技能走确定性路径 |
| 模型拒答 | 审计记录 `model.response`（refused），技能记录 `skill.model_skipped` 后回退确定性路径 |
| 输出不是 JSON，或结构与约定不符（如段落不是对象） | 技能记录 `skill.model_skipped` 后回退确定性路径 |
| 限流、服务端错误、连接失败、超时（已按 `max_retries` 重试）、重定向、响应报文异常 | 审计记录 `model.error`；办文流程中的模型步骤改用确定性路径，任务提示写明“模型接口调用失败”；按修改意见修订转人工处理；对话模式报告后可继续输入 |

Anthropic 适配默认启用服务端拒答回退（beta `server-side-fallback-2026-07-01`，`fallbacks="default"`），并在读取内容前检查 `stop_reason == "refusal"`；拒答会显式记录，不当作空结果成功。请求使用流式并取最终消息，长输出不会因非流式请求在生成完成前超时。对话模式的系统提示与工具集在会话内保持不变、历史只追加（回传的思考块绑定此前的会话前缀）；`api_key_env` 显式指定的变量未设置时视为配置错误，使用默认的 `ANTHROPIC_API_KEY` 时也可由 SDK 的其他凭据来源提供。

## 四、单位配置档

`src/gongwen/profiles/`：`party_gov`（党政机关，依照条例）、`hospital`、`university`、`research_institute`（参照条例）。每个配置档包含：适用主体类型、可用文种、办公室名称、内设机构对外行文规则、单位程序（触发关键词 → 程序名称）、自称模式（我委/我院/我校）、上级与平级机关（用于越级、平行行文判断）。

## 五、说明文件

工作区根目录的 `GONGWEN.md`（或 `AGENTS.md`）会从仓库根到当前目录逐级合并，子目录可用 `GONGWEN.override.md` 覆盖。只写本单位制度与惯例，不写具体事实数据；它补充提示词，但不改变权限。

## 六、公文字体

GB/T 9704—2012 规定的是字体类别（仿宋体、楷体、黑体、小标宋体、宋体）；**方正小标宋简体、仿宋_GB2312、楷体_GB2312 等字库名属实务，而且都是授权字库**（方正字库、中易）。本系统不下载、不附带这些字库，须使用本单位合法取得的文件，例如单位购买的方正字库，或 Windows 自带的“仿宋”“楷体”“黑体”“宋体”（`C:\Windows\Fonts` 下的 simfang.ttf、simkai.ttf、simhei.ttf、simsun.ttc）。

```bash
gongwen fonts check                         # 逐类核对：指定字库或备选名是否已安装（族名逐字核对，不把 fc-match 的回退当作已安装），渲染时实际用哪个字体
gongwen fonts install --from 字库目录/       # 安装本单位提供的字库（默认 ~/.local/share/fonts/gongwen；--system 为 /usr/local/share/fonts/gongwen，需管理员权限），刷新 fc-cache 后复核
gongwen fonts install --open                # Debian/Ubuntu：用 apt 安装开源替代字体（fonts-noto-cjk、fonts-noto-cjk-extra、fonts-lxgw-wenkai、fonts-arphic-ukai）；权限不足时打印应执行的命令
gongwen fonts map [--conf]                  # 渲染预览的替代映射（--conf 输出生成的 fontconfig 配置）
```

安装时从文件中读出字体族名（有 fontTools 时用 fontTools，否则用 `fc-scan`），与模板中的字库名、备选名逐一核对：

- 族名就是指定字库名：直接满足；
- 族名是备选名（如 Windows 的“仿宋 / FangSong”之于“仿宋_GB2312”）：渲染预览会用它代替，但 Word 中仍找“仿宋_GB2312”，因此提示在模板中直接改用该字库名，如 `gongwen template set 本单位 fonts.fangsong.name=仿宋`（字库名属实务，改用后不算偏离国标）；内置模板 `windows-fonts` 已按 Windows 自带字库名设置；
- 族名与任何字库名都不一致：警告，并按族名猜测字体类别给出 `template set` 建议。

**开源替代字体只用于预览。** 指定字库及其备选名都未安装时，LibreOffice 渲染子进程使用一份临时生成的 fontconfig 配置（环境变量 `FONTCONFIG_FILE`，包含系统配置，不改动系统字体设置，也不改变 DOCX 中的字体名），把字库名映射到最接近的开源字体：

| 类别 | 依次尝试（已安装的真实字库总是优先） | 说明 |
|---|---|---|
| 小标宋体 | 方正小标宋简体、方正小标宋_GBK、华文中宋 … → Noto Serif CJK SC Black → Noto Serif CJK SC Bold | Black 即思源宋体 Heavy，是笔画最粗的开源宋体，2 号标题的黑度最接近小标宋；Bold 接近中宋 |
| 宋体 | 宋体、SimSun … → Noto Serif CJK SC | |
| 黑体 | 黑体、SimHei … → Noto Sans CJK SC → 文泉驿正黑 | |
| 楷体 | 楷体_GB2312、楷体、KaiTi … → 霞鹜文楷（LXGW WenKai）→ 文鼎楷体（AR PL UKai CN） | |
| 仿宋体 | 仿宋_GB2312、仿宋、FangSong … → 朱雀仿宋（如自行安装）→ Noto Serif CJK SC Light | 软件源中没有开源仿宋体，以细宋体近似，字形不是仿宋，报告中写明 |

映射以 `<accept>` 追加在所请求的字库名之后，真实字库装了就用真实字库。渲染核验以 PDF 实际嵌入的字体为准，如实报告替代，例如“方正小标宋简体：本机未安装。已替代：Noto Serif CJK SC Black（开源替代字体，仅供预览；定稿须安装方正小标宋简体）”；存在替代时不声称已满足指定字体版式。`gongwen doctor` 也按当前模板逐类显示字体情况。

## 七、公文模板（可自行调整）

模板是叠加在基础配置档（`gbt9704-2012`）之上的具名改动，外加本单位信息。存放位置：内置（只读：`gbt9704-2012`、`windows-fonts`）、工作区 `templates/`（只读，便于随仓库共享）、数据目录 `<data_dir>/templates/`（用户模板，可写）；同名时数据目录优先。

```bash
gongwen template list
gongwen template new 本单位 --from windows-fonts
gongwen template set 本单位 elements.title.size=小二 "elements.organ_mark.color=#C00000" \
    unit.organ_mark=示例市卫生健康委员会文件 unit.doc_number_prefix=示卫发 unit.printer=示例市卫生健康委员会办公室
gongwen template set 本单位 --unset elements.title.size      # 恢复为国标默认值
gongwen template show 本单位 [--effective]                     # 模板内容、单位信息、偏离国标的各项（--effective 显示合并后的生效参数）
gongwen template validate 本单位
gongwen template export-dotx 本单位 -o 本单位公文.dotx          # Word 模板
gongwen template delete 本单位 --yes
gongwen format 稿件.docx --template 本单位                       # 也可用于 exec、task new；默认取配置 layout.template
```

模板文件示例（只写与基础配置档不同的设置）：

```yaml
name: 本单位
base: gbt9704-2012
description: 按本委公文处理实施细则
fonts:
  fangsong: {name: 仿宋, alternates: [FangSong, 仿宋_GB2312]}
elements:
  title: {size: 小二}
  organ_mark: {color: C00000, top_from_type_area_mm: 35, max_size: 小初}
  levels: {2: heiti}
  page_number: {style: dash}        # dash：“— 1 —”；plain：只有数字（偏离 7.5）
unit:
  organ_mark: 示例市卫生健康委员会文件   # 发文机关标志
  letter_organ_mark: 示例市卫生健康委员会 # 信函格式机关名称
  doc_number_prefix: 示卫发             # 发文字号代字（年份、序号仍由办理流程确定）
  printer: 示例市卫生健康委员会办公室     # 印发机关
  cc: [示例市人民政府办公室]             # 默认抄送机关
  brief_name: 卫生健康工作简报
  brief_issuer: 示例市卫生健康委员会办公室
```

可设置：`fonts.<类别>.name/alternates`；`page`、`margins`、`type_area`（毫米）；`grid.lines_per_page/chars_per_line/line_pt/char_spacing_twips`；`elements.*` 的字体（类别：fangsong/kaiti/heiti/xiaobiaosong/songti，也可写“仿宋”“黑体”等）、字号（字号表中的名称，如二号、小二）、颜色（十六进制）、缩进与空行、发文机关标志位置与字号上限、标题每行最多字数、页码样式等；`letter`、`command`、`jiyao`、`brief` 各节。未显式设置时相关数值随之推算：下白边、切口随天头、订口与版心推算（版心保持不变）；行距随每面行数、字距随每行字数与正文字号推算（向下取整到缇，不超出版心）；标题每行字数随标题字号推算。

校验：未知设置项（给出相近的正确写法）、字号名不在字号表、颜色不是十六进制、毫米数超出合理范围、版心与页边距之和不等于纸张、行数 × 行距或字数 × 字宽超出版心、修改条款与强度等标准注记，一律拒绝并逐项说明。

**偏离必须可见。** 各单位可依本单位公文处理细则调整格式，但每一项改动都对照基础配置档列出条款与强度：改动“规定”“一般”“推荐”“可以”类参数记为“偏离国标”；字库名、线宽、标题每行字数等国标未规定的数值记为“实务调整”；天头、订口在 ±1mm 允许误差内的记为“允许误差内”。偏离在 `template show/validate` 中列出，排版报告中另有 `TEMPLATE`（按哪个模板、偏离几项）与逐项的 `TPL-DEV` 核验项（“规定”类偏离注明须有制度依据）。生成参数核验与渲染核验都以模板的生效参数为准——有意设置的小二标题不会被判为不符合，但仍作为偏离列出。

单位信息只补全文稿中空缺（或仍为【待……】占位）的要素：发文机关标志、信函机关名称、发文字号代字、印发机关、默认抄送、简报名称与编印单位；排版报告注明“按模板的单位信息填入……，请核对”。暂不支持以图片作为红头（发文机关标志仍为文字）。

**Word 模板（.dotx）。** `export-dotx` 按模板生效参数设置纸张、页边距、行网格、奇偶页页码（7.5），排好版头（发文机关标志、发文字号与红色分隔线），并定义具名段落样式：公文标题、主送机关、正文、一级标题（黑体）、二级标题（楷体）、三级标题、四级标题、附件说明、署名、成文日期、附注、版记，另有发文机关标志、发文字号。主部件的内容类型为 Word 模板（`wordprocessingml.template.main+xml`），已用 LibreOffice 验证可打开；Word 中未实测。模板中的版记是普通段落，定稿时须放在最后一面版心底部；加盖印章时署名应以成文日期为准居中。

## 八、排版预览

```bash
gongwen preview 稿件.txt --template 本单位 -o 预览 --open   # 文稿：排版 → LibreOffice 渲染 → 页面图像 → preview.html
gongwen preview <任务编号>                                 # 任务当前排版稿（默认输出到数据目录 previews/<任务>）
```

`preview.html` 自包含（页面图像内嵌），并排显示各页，附核验结果（通过／提示／不符合／未核验）、模板偏离与字体替代。本机没有 LibreOffice 或 poppler-utils 时退回 HTML 近似预览，并写明“未实际渲染，不代表实际版面”。

本地工作台（`gongwen serve`）另有：`/templates` 模板列表与常用设置的编辑表单（各类字库名、各要素字号与字体、发文机关标志颜色与位置、天头订口、单位信息），“预览”按示例公文渲染页面图像，“保存”经服务端校验后写入用户模板；`/preview/<任务>` 显示任务当前排版稿的页面图像与核验结果。写操作沿用工作台的安全约束（仅本机、会话令牌、请求大小限制），预览图像只从数据目录 `previews/` 下读取。
