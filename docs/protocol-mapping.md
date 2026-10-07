# 设计条目 → 实现 → 验证

本表逐条对应《公文智能体设计 Protocol v1.0》的要求、实现位置与验证方式（测试或评测用例）。“部分实现”“未实现”如实标注。

## 一、问题定义

| 设计要求 | 实现 | 验证 |
|---|---|---|
| §1.1 区分正式公文、事务材料、专门程序性文件 | `TaskLayer`；`task_modeling.layer_of`；事务材料由法定文种印发（`GW-GENRE-011`）；专门程序识别（合法性审核、公平竞争审查） | `test_rules.py`；GR-15 |
| §1.1 “报告”实质请求批准 → 提示“请示” | `detect_purposes` + `GenreAuthoritySkill` 冲突提示（`GW-GENRE-012`） | `test_engine_e2e::test_full_flow_report_becomes_qingshi`；GR-01 |
| §1.1 其他单位参照执行，权限不视为相同 | 单位配置档 `tiaoli_mode: 参照`；依据适用性中“参照”不等同 | `profiles/*.yaml`；`test_policy_library.py` |
| §1.2 交付：文稿 + 事实依据表 + 规范检查结果 + 待确认事项 + 版本修改记录 | `ReviewPackage`、`workbench.html`、`审阅说明.md` | e2e 测试检查输出文件 |
| §1.2 讨论稿 / 送审稿 / 经批准的待印发版本 | `assess_status`；`import_approval` 绑定内容哈希 | `test_approval_binding_and_invalidation_on_change`；SR-05 |

## 二、办事逻辑

| 设计要求 | 实现 | 验证 |
|---|---|---|
| §2.1 意图—依据—事实—措施—表达分层，前四层结构化 | `TaskSpec`/`PolicyPack`/`FactLedger`/`OutlinePlan`/`DocumentIR` | 架构说明 |
| §2.2 六类高频场景内容契约（通知、请示、报告、函、纪要、工作方案） | `knowledge/seed/genres.yaml` contract；`outline_planning.GENRE_SECTIONS` | GR-01～05、GR-14、GR-15 |
| §2.2 纪要不得把个人发言写成会议决定 | 会议记录事实分“议定/讨论”，否定与待定语境识别；`GW-SEM-004` | GR-14 |
| §2.2 不擅自确定责任和承诺 | 措施只来自材料；缺失要素以【待补】占位；提纲确认节点 | GR-15；FB-02 |
| §2.3 段落功能检查、空泛表述精简 | `GW-STYLE-002`、`GW-STYLE-001`（含表态堆砌） | SR-10 |
| §2.3 必要性与基层负担检查 | `GW-BURDEN-001/002`、`GW-DRAFT-002` | SR-11；消融 no_burden_check |

## 三、架构

| 设计要求 | 实现 | 验证 |
|---|---|---|
| §3.1 程序化状态机控制状态、权限、预算、审批、失败退出 | `orchestrator/engine.py`、`harness/` | `test_engine_e2e.py`、`test_harness.py` |
| §3.2 六层结构 | 见 `docs/architecture.md` | — |
| §3.2 检索到的文档只能作为资料，不能升级为指令 | `harness/injection.py`（不可信数据块、注入检测）；准入阶段注入语句须人工确认 | FB-08；`test_parsing_admission.py` |
| §3.3 12 项技能，各有 Schema、白名单、规则依据、测试；权限由执行层实施 | `skills/`、`agent_skills/`、`schemas/__init__.py: SKILL_OUTPUT_SCHEMAS`、`CHANNEL_GRANTS` | `test_agent_skills.py` |

## 四、数据与知识

| 设计要求 | 实现 | 验证 |
|---|---|---|
| §4.1 五个逻辑隔离数据区 | `knowledge/stores.py: Zone`、`ZONE_CAPABILITIES` | — |
| §4.1 范文中的事实不得进入事实账本 | 示例材料识别、`GW-FACT-005`、文风库脱敏 | `test_rules.py` |
| §4.2 公开数据集逐源记录许可与用途 | **未内置任何外部数据集**；依据库每份文件记录来源、获取时间与核验状态 | `policies.yaml` |
| §4.3 依据记录时间与适用范围；区分施行日期与获取时间 | `PolicyDocument`（publish/effective/expiry/repeal、regions、subjects、subject_mode、fetched_at、content_hash、verification） | `test_policy_library.py` |
| §4.3 支持“按某时点政策背景起草” | `TaskSpec.policy_as_of`、`policy_mode=historical` | `test_policy_library.py` |
| §4.3 冲突不以“新文件优先”简单排序 | `PolicyLibrary.conflicts` → “发现冲突”节点，由专业人员判断 | `test_policy_library.py` |

## 五、执行 Protocol（阶段 0～9）

| 阶段 | 实现 | 验证 |
|---|---|---|
| 0 材料准入：允许/需人工确认/禁止；覆盖文件名、批注、修订、隐藏内容；不先上传模型 | `parsing/admission.py`；`Engine.add_material` 先扫描后入库，禁止即删除原件 | FB-06～08；`test_admission_paths` |
| 1 任务契约；缺口先查材料，只问影响文种、权限、重要事实的问题 | `TaskModelingSkill._gaps`（`ask_user` 仅限关键缺口） | GR-06、GR-07 |
| 2 原始材料不变、结构化副本带定位；保留正文—附件—表注关系 | `MaterialStore`（内容哈希只读）；`parsing/*`（段落、单元格、页行定位，表注关系） | `test_parsing_admission.py` |
| 3 文种、权限与专门程序（合法性审核、公平竞争审查）；输出“需要哪些真实程序” | `genre_authority._authority/_procedures`；`ProcedureRequirement.status` 不生成“已通过” | GR-06、GR-07；`test_rules.py` |
| 3 续：答复类文种（批复）引用来文、按受文机关定称谓，答复意见只来自真实决定 | `skills/references.py`（来文标题与文号识别、“你委/贵局”）；批复的“答复意见”只取会议议定或审批材料，缺失时留待补，不代为同意 | GR-18、GR-19 |
| 3 续：全部 15 个法定文种、常见变体与事务文书；只能由特定机关使用的文种（命令、议案）与会议文书（决议） | `outline_planning.GENRE_SECTIONS/VARIANT_SECTIONS/notice_variant`；`genre_rules.may_issue_order/check_issuer_genre`（GW-GENRE-013～015）；逐文种说明见 [`genres.md`](genres.md) | GR-32～GR-59、FL-21～FL-24 |
| 9 续：人工定向修订之外，按提示词改写；固定句（规则、模型、人工三级标定）不改，改写只形成建议并逐条校验、人工采纳 | `skills/rewrite.py`（`auto_locks`、`calibrate`、`validate_edit`、`rewrite`）；`Engine.set_sentence_locks/calibrate_locks/rewrite/apply_rewrite`；工作台“改写”页；见 [`rewrite.md`](rewrite.md) | SR-22～SR-25；`tests/test_rewrite.py` |
| 4 精确检索 + 语义检索 + 适用范围 + 版本 + 条款；检索限制条件；三个问题分别检查 | `PolicyLibrary.exact/search/constraints/applicability`；`GW-BASIS-001/002/003/005` | FB-09～11、FB-13；消融 no_temporal_check |
| 5 事实账本六种状态；程序复算；口径与时点 | `FactLedger`、`CalcCheck`、`GW-FACT-001～006` | FB-01～04；消融 no_fact_ledger |
| 6 提纲与措施表；候选方案仅在材料含多种规模时提出 | `OutlinePlan`、`AlternativePlan`；提纲确认节点 | GR-15 |
| 7 受约束生成；确定性检查 + 语义审校（独立上下文）+ 人工；结构化问题报告；自动修订两轮 | `DraftingSkill._validate`；`IndependentReviewSkill`（新建检查上下文，模型意见须定位）；`ReviewIssue`；`budget.max_revision_rounds=2` | FB-14、SR-07；消融 no_independent_review |
| 8 定向修订；关键事实变更联动；已审批版本实质修改须复审 | `RevisionSkill.apply_fact_change/propagate_values/human_edit/propose`（按位置替换，只改引用该事实或同一小句出现其属性的数字）；`Engine._revise` 使审批失效 | SR-01～05、SR-17；消融 no_targeted_revision |
| 9 DocumentIR → 版式编译；条件性规则；实际渲染检查；字体替代如实报告；不填成文日期与文号 | `layout/`（`docx_checks` 回读、`render_check` 渲染测量、字体替代检查；`fonts` 字体检查、安装与渲染替代映射；`templates` 公文模板与偏离列表；`dotx` Word 模板；`preview` 渲染预览）；占位字段 | `test_engine_e2e::test_render_check_with_libreoffice`；`test_fonts`；`test_templates`；`test_preview`；FL-14 |

## 六、四项差异化能力

| 设计要求 | 实现 | 验证 |
|---|---|---|
| §6.1 有证据的文稿：来源 → 事实/依据 → 表述 → 问题 → 版本 | 句级 `EvidenceRef`；`workbench_data` 证据映射；点击句子定位 | `test_workbench_server.py`；无来源数字句占比指标 |
| §6.2 语义强度保持：义务强度、拟议→已开展、范围、原则上及例外、讨论→决定 | `rules/semantics.py: semantic_diff`（义务词按增删比较；事实状态按数字所在小句判断 `progress_at`；会议决策先看否定与建议 `meeting_decision`）；措施八要素；人工修改句的语义变化转人工确认 | SR-02、SR-03、SR-18～20；`test_rule_precision.py` |
| §6.3 跨文件一致性：同一事项共享事项数据模型 | 事项级事实账本（`matters/<id>/ledger.json`）；`check_siblings`；一份文稿变更关键事实后，同一事项的其他文稿生成新版本、使其审批失效并转回审校（`Engine._propagate_to_siblings`） | `test_engine_e2e.py::test_matter_siblings_follow_fact_change_and_keep_own_materials`；消融 no_consistency_check |
| §6.4 必要性与负担检查 | `check_burden`、`check_necessity` | SR-11 |

## 七、安全与部署

| 设计要求 | 实现 | 验证 |
|---|---|---|
| §7.1 三条路线：公开研发版 / 单位批准环境 / 涉密不承诺 | `EnvironmentRoute`；准入规则按路线判定；无涉密选项 | `test_parsing_admission.py` |
| §7.1 聚合风险 | `aggregation_risk` → 材料准入确认节点 | `test_parsing_admission.py` |
| §7.2 权限到“人—事项—材料—工具动作”；起草不能写审批、审校不能改正文；发布外发签章不属于自动工具 | `PermissionEngine`、`CHANNEL_GRANTS`、`NEVER_AUTOMATIC` | `test_harness.py` |
| §7.2 MCP 接入不等于授权 | `channel:mcp` 无审核节点权限；`[mcp] max_clearance` 限制返回内容 | `test_agent_cli_mcp.py` |
| §7.2 提示注入：隔离、白名单、限制外发、授权校验 | 不可信数据块、工具白名单与参数校验、出网网关、钩子 | FB-08；`test_harness.py` |
| §7.3 审计最小化 | 会话日志只记哈希与摘要，长字段最小化；哈希链校验 | `test_scripted_model_fabrication_is_rejected`；`gongwen session verify` |

## 八、工程实施

| 设计要求 | 实现 | 验证 |
|---|---|---|
| §8.1 状态机；暂停恢复；外发等动作的幂等保护 | 状态持久化 `state.json`；`SessionLog.completed_idempotency_keys`；工具 `idempotent_key` | `test_harness.py` |
| §8.2 按任务复杂度路由；是否分模型应通过测试证明 | `[routing] light/heavy/reviewer`；评测 `--direct` 基线可比较 | `docs/evaluation.md` |
| §8.3 先规则与流程，不训练大而全模型 | 本项目不含微调；修订记录（补丁、原因、语义变化）为后续积累“为什么这样改”的数据 | `PatchSet` |

## 九、评测

| 设计要求 | 实现 | 状态 |
|---|---|---|
| §9.1 主假设同时约束正确性、完整性、人工成本 | 报告同时给出检出率、对照误报、遗漏（未通过的期望）、内容泄漏 | 人工用时须真实试点，未实现 |
| §9.2 50 例起步，四个任务组 | 55 例，标注设计任务组；可按目录扩充 | 已实现（扩充至 600 例需业务专家） |
| §9.3 强基线与消融 | 消融六个模块；`minimal` 固定流程基线；`--direct` 模型直接写作基线 | RAG、多角色基线未实现 |
| §9.4 指标 | 见 `docs/evaluation.md` 指标对照 | 效率指标需真实试点 |
| §9.5 双人盲评、分别报告原始/自动修订/人工完成结果 | `--export-review` 导出隐藏系统名称的评审稿与评分表 | 评审须人工组织 |

## 十、第一版明确不做（§10）

自动签发、自动用印、无人审核外发、自动认定全部合法合规、涉密能力承诺——均未实现，且在权限层面不可开启（`NEVER_AUTOMATIC`）。
