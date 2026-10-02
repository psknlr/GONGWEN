"""终端输出格式：状态、审核节点、问题与证据（命令行与对话共用）。"""

from __future__ import annotations

from typing import Any


def fmt_status(s: dict[str, Any]) -> str:
    lines = [
        f"任务 {s['task_id']}（事项 {s['matter_id']}）",
        f"  阶段：{s['stage']}　文稿状态：{s['doc_status']}　版本：第 {s['version']} 版",
    ]
    counts = s.get("issue_counts") or {}
    if counts:
        lines.append("  审校问题：" + "　".join(f"{k} {v}" for k, v in counts.items()))
    for e in s.get("errors") or []:
        lines.append(f"  ⚠ {e}")
    cps = s.get("pending_checkpoints") or []
    if cps:
        lines.append("  待人工处理：")
        for cp in cps:
            lines.append(f"    [{cp['cp_id']}] {cp['kind']}：{cp['question']}")
            for d in (cp.get("details") or [])[:12]:
                lines.append(f"        · {d}")
            lines.append(f"        选项：{'；'.join(cp['options'])}")
    for p in s.get("pending_proposals") or []:
        lines.append(f"  待采纳修改建议 [{p['proposal_id']}]：{p['instruction'][:80]}")
    lines.append(f"  输出目录：{s.get('outputs', '')}")
    models = s.get("models") or {}
    if models:
        lines.append("  模型：" + "　".join(f"{k}={v}" for k, v in models.items()))
    return "\n".join(lines)


def fmt_issues(items: list[dict[str, Any]]) -> str:
    if not items:
        return "没有未决问题。"
    if len(items) == 1 and "message" in items[0]:
        return items[0]["message"]
    out = []
    for i in items:
        loc = i.get("location") or ""
        sid = f" {i['sid']}" if i.get("sid") else ""
        out.append(f"[{i['id']}] {i['severity']}｜{i['type']}｜{loc}{sid}")
        if i.get("original"):
            out.append(f"    原文：{i['original'][:120]}")
        out.append(f"    建议：{i['suggestion']}")
        if i.get("rule"):
            out.append(f"    依据：{i['rule']}")
        if i.get("needs_human"):
            out.append("    （须人工判断）")
    return "\n".join(out)


def fmt_evidence(ev: dict[str, Any]) -> str:
    if "message" in ev:
        return ev["message"]
    lines = [f"{ev.get('location', '')}：{ev.get('text', '')}", f"确认状态：{ev.get('confirmation', '')}"]
    for r in ev.get("refs", []):
        lines.append(f"  - {r['label']}（{r['status']}）")
        for k, v in (r.get("detail") or {}).items():
            if v:
                lines.append(f"      {k}：{v}")
    return "\n".join(lines)


def fmt_check(d: dict[str, Any]) -> str:
    head = f"《{d.get('title') or '（未识别标题）'}》 文种：{d.get('genre') or '未识别'}　行文方向：{d.get('direction') or '未识别'}"
    counts = d.get("counts") or {}
    lines = [head, "问题计数：" + ("　".join(f"{k} {v}" for k, v in counts.items()) or "无")]
    for i in d.get("issues", []):
        lines.append(f"[{i['id']}] {i['severity']}｜{i['type']}｜{i['location']}")
        if i.get("original"):
            lines.append(f"    原文：{i['original'][:120]}")
        lines.append(f"    建议：{i['suggestion']}")
        if i.get("rule"):
            lines.append(f"    依据：{i['rule']}")
    for u in d.get("unverifiable", []):
        lines.append(f"须人工核验：{u}")
    return "\n".join(lines)
