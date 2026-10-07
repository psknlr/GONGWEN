# 架构说明

> 受控主编排器（程序化状态机）+ 12 项可组合技能 + 独立审校通道 + 确定性规则与计算工具 + 人工审核节点（设计 §3.1）

“主编排器”不是一个拥有所有权限、自由决定一切的大模型。任务状态、工具权限、预算、审批和失败退出都由程序决定；模型只在允许的范围内提出计划、组织文稿或提交修改建议，并且每一次输出都要经过确定性校验。

## 一、六层结构（设计 §3.2）

| 层次 | 职责 | 本项目实现 |
|---|---|---|
| 交互工作台 | 收集需求、展示材料、定位问题、人工确认与修订 | `cli/main.py`（命令行、无头 NDJSON）、`agent/`（对话代理与人工斜杠命令）、`workbench/`（审阅工作台页面与本地服务） |
| 治理与编排层 | 状态转换、权限、预算、暂停恢复、审批绑定 | `orchestrator/engine.py`（状态机）、`orchestrator/checkpoints.py`（审核节点）、`harness/`（权限、审批策略、钩子、预算、出网网关、会话日志、工具管线） |
| 专业技能层 | 解析、检索、事实、起草、审校、修订等 | `skills/` 12 项技能；`agent_skills/` 对应的 Agent Skills 说明 |
| 知识与证据层 | 规则库、依据库、事项材料库、案例与模板库 | `knowledge/`（依据库、文种库、词表、存储）、`rules/registry.py`（规则库）、`profiles/`（单位配置档） |
| 工具执行层 | 检索、计算、表格校验、格式编译、渲染 | `knowledge/retrieval.py`（BM25 与精确命中）、`rules/textutil.py`（数字抽取与复算）、`layout/`（DOCX 编译、回读核验、LibreOffice 渲染测量） |
| 评价与运维层 | 回归测试、安全测试、版本管理 | `eval/`（回归用例、消融、基线、盲评导出）、`tests/`、规则库与版式配置版本号写入送审包 |

## 二、状态机（设计 §8.1）

```
材料准入 → 任务确认 → 材料解析 → 依据与事实准备 → 提纲确认 → 起草 → 审校 ⇄ 定向修订 → 排版检查 → 人工送审
                                     │                                                        │
                                     ├→ 发现冲突（材料数据冲突 / 依据冲突）                       ├→ 已形成送审材料（人工确认）
                                     ├→ 超出权限（内设机构对外行文、部门向下级政府发指令等）       └→ 已绑定审批记录（导入真实审批，绑定内容哈希）
                                     ├→ 待补材料
任何阶段 → 处理失败（预算超限、权限拒绝、异常）；材料准入 → 禁止进入
```

- 每个阶段由 `Engine._h_<阶段>` 处理器实现，返回下一阶段或 `None`（停在审核节点）；
- 审核节点（`Checkpoint`）只能由人工主体处理：`resolve_checkpoint` 检查 `by.is_human` 与权限 `checkpoint.resolve`；
- 无头模式只能按启动者显式给出的 `--accept` 自动接受四类节点（任务确认、提纲确认、冲突暂不采信、审校问题保留），**材料准入确认、权限确认、人工送审永远需要人**；
- 失败必须可见：预算超限、权限拒绝和异常都会进入“处理失败”并写入审计日志；模型拒答或接口故障时，可选的模型步骤改用确定性路径（不是猜测），并记入审计与任务提示，不会悄悄降级；
- 追加材料会使派生产物失效并回到准入阶段；已审批版本发生实质修改时审批失效，并提示“须报原签批人复审（条例第二十五条（一））”。

## 三、12 项技能（设计 §3.3）

| # | 技能 | 阶段 | 通道 | 输出 Schema | 代码 |
|---|---|---|---|---|---|
| 1 | 任务建模 | 任务确认 | planner | `TaskSpec` | `skills/task_modeling.py` |
| 2 | 材料解析 | 材料准入/解析 | parser | `SourceBundle` | `skills/material_parsing.py`、`parsing/` |
| 3 | 文种与权限判断 | 依据与事实准备 | planner | `GenreDecision` | `skills/genre_authority.py` |
| 4 | 政策依据检索 | 依据与事实准备 | retriever | `PolicyPack` | `skills/policy_retrieval.py` |
| 5 | 事实账本构建 | 依据与事实准备 | ledger | `FactLedger` | `skills/fact_ledger.py` |
| 6 | 方案与提纲规划 | 提纲确认 | outliner | `OutlinePlan` | `skills/outline_planning.py` |
| 7 | 受约束起草 | 起草 | drafter | `DocumentIR` | `skills/drafting.py` |
| 8 | 跨材料一致性检查 | 审校 | reviewer | `ConsistencyReport` | `skills/consistency_check.py`、`rules/consistency.py` |
| 9 | 独立审校 | 审校 | reviewer | `ReviewReport` | `skills/independent_review.py`、`rules/` |
| 10 | 定向修订 | 定向修订 | reviser | `PatchSet` | `skills/revision.py` |
| 11 | 版式编译与检查 | 排版检查 | layout | `LayoutReport` | `skills/layout_compile.py`、`layout/` |
| 12 | 送审打包与留痕 | 人工送审 | packager | `ReviewPackage` | `skills/review_package.py` |

技能是按需调用的能力模块，不是常驻角色。每项技能都有：Agent Skills 格式的说明（`agent_skills/<name>/SKILL.md`，按需加载）、输入输出 Schema（`gongwen skills schemas` 导出）、工具白名单、规则依据和测试。**真正的权限约束由执行层实施**：每项技能以独立的“通道”主体运行，通道能力由 `harness/permissions.py` 的 `CHANNEL_GRANTS` 决定，例如审校通道没有 `ir.write`、起草通道没有 `approval.import`。

## 四、五个逻辑隔离的数据区（设计 §4.1）

| 数据区 | 实现 | 能支撑什么 |
|---|---|---|
| 权威规范与政策依据库 | `knowledge/policy_library.py`，种子数据 `knowledge/seed/policies.yaml`、`tiaoli_2012.json`；单位依据经 `gongwen policy add` 登记到数据目录 | 依据（可以依据什么、需要遵守什么） |
| 本事项材料库 | `knowledge/stores.py: MaterialStore`（原件按内容哈希只读存放） | 事实（本单位事实的唯一来源） |
| 历史案例与文风库 | `knowledge/stores.py: CaseLibrary`（检索结果中的数字、机构、日期一律脱敏） | 文风与结构参考，不提供事实 |
| 可执行规则库 | `rules/registry.py`（84 条，带来源层级与条件性） | 检查条件 |
| 任务与审计成果库 | `knowledge/stores.py: TaskStore`（状态、产物哈希、版本、输出）、`harness/session.py`（哈希链日志） | 追溯与复核 |

隔离原则落在代码中：示例/范文材料的事实带 `example` 标记，审校规则 `GW-FACT-005` 禁止其进入文稿；文风库检索结果经 `mask_specifics` 脱敏。

## 五、数据结构：意图—依据—事实—措施—表达（设计 §2.1）

前四层先形成结构化对象，第五层才组织语言：

- 意图：`TaskSpec`（目的、发文主体、受文主体、行文关系、文种、事由、适用时点、地域、禁止补写字段、缺口）；
- 依据：`PolicyPack`（条款原文、适用性判断、实体依据/程序规范之分、冲突）；
- 事实：`FactLedger`（七种状态、来源定位、口径时点、公式、核验记录、冲突）；
- 措施：`OutlinePlan.measures`（主体—行为—对象—条件—时限—义务强度—例外—依据）；
- 表达：`DocumentIR`（每句带证据引用、功能与来源：system/model/human/patch），由版式配置编译为文件。

## 六、内核与插件

`kernel/context.py` 实现服务注入、事件与可回收副作用（借鉴 Cordis 的“一切皆插件”）。`runtime.py` 以插件方式装配存储、知识、治理与技能；单位可在配置中追加插件：

```toml
[[plugins]]
module = "my_unit.checks:UnitRulesPlugin"   # 例如追加本单位规则检查器
config = { strict = true }
```

## 七、模型接入与路由（设计 §8.2）

- `llm/presets.py`：各服务商预设（地址与区域变体、密钥环境变量、默认型号、接口差异）与别名；`gongwen model presets/add/list/test/remote` 基于它接入与核验；
- `llm/openai_compat.py`：OpenAI GPT、DeepSeek、智谱 GLM、MiniMax、通义千问、Kimi、xAI、Ollama、vLLM 及任意 OpenAI 兼容服务（按预设处理输出上限参数、采样温度、JSON 模式、思考内容等差异）；
- `llm/anthropic_provider.py`：Claude（官方 SDK；结构化输出、严格工具、拒答检查、服务端拒答回退）；
- `llm/router.py`：按任务复杂度路由（light 抽取分类、heavy 起草修订、reviewer 独立审校、agent 对话；均可在 `[routing]` 指定具名模型），每次调用依次经过出网网关、预算、审计与拒答检查；
- 未配置模型时所有技能走确定性路径，流程完整可用；配置模型后，模型输出仍要经逐句校验（不得新增数字、升级事实状态、改变义务强度、写入审批结论、删除占位），不通过即回退；
- 模型拒答、输出不是 JSON 或结构不符：记录后回退确定性路径；接口故障（网络、超时、HTTP 错误，已按配置重试）统一为 `ModelCallFailed`，写入审计 `model.error`；起草、审校等可选的模型步骤改用确定性路径继续，并在任务提示中写明“模型接口调用失败，已改用确定性路径”，对话模式报告后可继续。

## 八、交付件

见 [`skills/review_package.py`](../src/gongwen/skills/review_package.py)：文稿（DOCX/HTML/MD）、`workbench.html`（文稿、证据、问题、待确认、修改记录、版式同屏，点击定位）、`review_package.json`、`审阅说明.md`。文稿状态由 `assess_status` 按规则判定。
