# 更多示例：批复、纪要、印发类通知与差错检查

材料均为合成示例。先在本目录初始化：

```bash
cd examples/more
gongwen init --unit-name 示例市人民政府 --region 示例省
```

## 一、批复（答复意见只来自真实决定）

```bash
gongwen exec "起草一份对市卫生健康委员会请示的批复" \
  --material 来文请示.txt --material 常务会议纪要摘录.md --clearance 公开 \
  --to 示例市卫生健康委员会 --issuer 人民政府 --accept task_confirm --accept outline_confirm
```

- 标题由来文事由生成；开头引用来文标题与发文字号：“你委《……的请示》（示卫〔2026〕5号）收悉。”；
- 答复意见取自常务会议的决定（100 万元），不照抄来文申请的 120 万元；
- 去掉 `--material 常务会议纪要摘录.md` 再试：答复意见留【待补】，系统不代为“同意”。

## 二、纪要（只写确已议定的事项）

```bash
gongwen -c environment.unit_name="示例市卫生健康委员会" exec "根据会议记录起草示范点建设工作专题会议纪要" \
  --material 会议记录.md --clearance 公开 --issuer 政府部门 --accept task_confirm --accept outline_confirm
```

- 开头取会议记录的时间、地点、主持人；“会议同意”“会议要求”列为议定事项；
- “会议未作决定”的建议列为待研究事项，不写成决定；出席、请假名单照录。

## 三、印发类通知（方案作为附件）

```bash
gongwen -c environment.unit_name="示例市卫生健康委员会" exec "起草一份印发基层医疗示范点建设工作方案的通知" \
  --material 工作安排.md --clearance 公开 --to 各区卫生健康局 --issuer 政府部门 \
  --accept task_confirm --accept outline_confirm
```

- 标题“关于印发《基层医疗示范点建设工作方案》的通知”；通知正文只写印发事项，方案按目标、任务、分工、进度编排在附件中；
- 材料中没有的部分（如工作范围）留【待补】，不擅自补写。

## 四、检查并自动修订一份有差错的文稿

```bash
gongwen check 有差错的请示.txt
gongwen check 有差错的请示.txt --fix -o out
```

- 检出：以报告申请事项（应为请示）、报告夹带请示、上行文主送两个机关、抄送下级、对上级称“你局”、年份简写、
  引用已废止的 2000 年办法、引用顺序、附件序号与标点、汉字成文日期等；
- `--fix` 只改标点、日期写法、附件序号等机械性问题，输出 `out/有差错的请示.修订.md` 与逐处修改清单；
  文种、称谓、行文关系等须人工处理的问题保留在复检结果中。
