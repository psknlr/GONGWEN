# 演示：从“写一份报告”到送审包

材料（均为合成示例，不含真实单位数据）：
- `情况说明.md`：示范点建设进展（已建成 8 个；拟新建 12 个）
- `经费测算表.csv`：设备购置 60、场地改造 40、人员培训 20，合计 120（万元）

## 一、无头执行（与 `codex exec`、`grok --prompt` 类似）
```bash
cd examples/demo
gongwen init --unit-name 示例市卫生健康委员会 --region 示例省
gongwen exec "写一份向主管部门申请基层医疗示范点建设经费的报告" \
  --material 情况说明.md --material 经费测算表.csv --clearance 公开 \
  --to 示例市人民政府 --issuer 政府部门 --accept task_confirm
```
- 系统会提示：用户说“报告”，但实质是请求批准，更适合“请示”（条例第十五条：报告不得夹带请示事项）；
- 未加 `--accept outline_confirm` 时停在“提纲与措施确认”（退出码 3），由你确认已核实的数据：
```bash
gongwen task status <任务编号>
gongwen task confirm <任务编号> <节点编号> edit --data '{"confirm_facts": ["F-002", "F-003"]}'
```
- 到达“人工送审”后查看：
```bash
gongwen task draft <任务编号>          # 带句号的文稿
gongwen task evidence <任务编号> s-003  # 某句的来源、原文、状态
gongwen task issues <任务编号>          # 审校问题与规则来源层级
gongwen serve --open                    # 本地审阅工作台（证据、问题、待确认、修改记录、版式）
```
- 关键数据变更（联动正文、合计与附件）：
```bash
gongwen task revise <任务编号> --fact F-00x=40 --reason 核减
```

## 二、对话模式
```bash
gongwen chat
公文> 帮我起草一份向市政府申请示范点建设经费的请示，材料在本目录
公文> /status
公文> /confirm CP-001 confirm --data '{"materials": {"MAT-001": "公开", "MAT-002": "公开"}}'
```
未配置模型时，对话模式仍可用斜杠命令完成全部流程（确定性路径）。

## 三、检查和排版已有文稿
```bash
gongwen check ../drafts/通知稿.txt
gongwen format ../drafts/通知稿.txt -o out
```
