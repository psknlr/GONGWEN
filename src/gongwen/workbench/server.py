"""本地审阅服务：在浏览器中查看审阅工作台并通过人工通道处理待确认事项。

安全边界：
* 只绑定本机回环地址；
* 写操作必须携带本次启动生成的令牌（X-GW-Token），并校验 Host/Origin，防止跨站请求；
* 服务代表启动它的本地用户（人工主体）处理审核节点；任何模型通道都无法调用这些接口。
"""

from __future__ import annotations

import html
import json
import secrets
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from ..harness.permissions import Principal
from ..orchestrator import Engine
from ..schemas.ir import DocumentIR
from .page import PAGE_CSS, build_page

LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


def task_list_html(engine: Engine) -> str:
    rows = []
    for tid in reversed(engine.store.list_tasks()):
        try:
            st = engine.load_state(tid)
        except Exception:
            continue
        req = html.escape(str(st.options.get("request", ""))[:60])
        rows.append(
            f'<div class="card"><h4><a href="/task/{tid}">{tid}</a> <span class="st">{html.escape(st.stage.value)}</span> '
            f'<span class="st">{html.escape(st.doc_status.value)}</span></h4><div class="meta">{req}</div>'
            f'<div class="meta">待确认 {len(st.pending_checkpoints())} 项｜第 {st.current_version} 版</div></div>'
        )
    body = "".join(rows) or '<div class="empty">暂无任务。使用 gongwen task new 创建。</div>'
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>公文智能体</title><style>{PAGE_CSS} .wrap{{max-width:900px;margin:0 auto;padding:16px}}</style></head>
<body><header class="top"><h1>公文智能体 · 任务列表</h1></header><div class="wrap">{body}</div></body></html>"""


def early_stage_html(engine: Engine, task_id: str, api: str, token: str) -> str:
    """尚未形成文稿的阶段：只展示任务状态与待确认事项。"""
    st = engine.load_state(task_id)
    ir = DocumentIR(doc_id="-", matter_id=st.matter_id, title=str(st.options.get("request", "")))
    data = {
        "task_id": task_id,
        "status": st.doc_status.value,
        "genre": "",
        "counts": {},
        "issues": [],
        "evidence": {},
        "pending": [],
        "checkpoints": [
            {"cp_id": c.cp_id, "kind": c.kind.value, "question": c.question, "details": c.details, "options": [o.model_dump() for o in c.options]}
            for c in st.pending_checkpoints()
        ],
        "versions": [],
        "patches": [],
        "layout": None,
    }
    page = build_page(ir, data, api=api, token=token)
    return page.replace('<nav class="tabs"><button data-t="evidence" class="on">', '<nav class="tabs"><button data-t="evidence">').replace(
        '<button data-t="pending">', '<button data-t="pending" class="on">'
    ).replace('<div class="pane on" id="p-evidence"></div>', '<div class="pane" id="p-evidence"></div>').replace(
        '<div class="pane" id="p-pending"></div>', '<div class="pane on" id="p-pending"></div>'
    )


def make_handler(engine: Engine, user: Principal, token: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "GongwenWorkbench/1.0"

        def log_message(self, fmt, *args):  # 不把请求细节写到标准错误
            return

        def _send(self, code: int, body: str, ctype: str = "text/html; charset=utf-8") -> None:
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'; connect-src 'self'")
            self.end_headers()
            self.wfile.write(data)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False, default=str), "application/json; charset=utf-8")

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0]
            origin = self.headers.get("Origin")
            if host not in LOCAL_HOSTS:
                return False
            if origin and urlparse(origin).hostname not in {"127.0.0.1", "localhost", "::1"}:
                return False
            return True

        def do_GET(self):  # noqa: N802
            if not self._host_ok():
                return self._send(HTTPStatus.FORBIDDEN, "forbidden")
            path = urlparse(self.path).path
            if path == "/":
                return self._send(200, task_list_html(engine))
            if path.startswith("/task/"):
                tid = path.split("/")[2]
                try:
                    st = engine.load_state(tid)
                except KeyError:
                    return self._send(404, "任务不存在")
                page = engine.workbench_page(tid, api="/api", token=token)
                if page is not None:
                    return self._send(200, page)
                return self._send(200, early_stage_html(engine, tid, "/api", token))
            if path.startswith("/api/task/"):
                tid = path.split("/")[3]
                try:
                    return self._json(200, engine.status(tid))
                except KeyError:
                    return self._json(404, {"message": "任务不存在"})
            return self._send(404, "not found")

        def do_POST(self):  # noqa: N802
            if not self._host_ok() or not secrets.compare_digest(self.headers.get("X-GW-Token", ""), token):
                return self._json(HTTPStatus.FORBIDDEN, {"message": "令牌无效或来源不受信任"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._json(400, {"message": "Content-Length 无效"})
            if length < 0:
                return self._json(400, {"message": "Content-Length 无效"})
            if length > 1_000_000:
                return self._json(413, {"message": "请求过大"})
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:  # 含 JSONDecodeError 与非 UTF-8 内容
                return self._json(400, {"message": "请求体不是有效 JSON"})
            if not isinstance(body, dict):
                return self._json(400, {"message": "请求体须为 JSON 对象"})
            path = urlparse(self.path).path
            try:
                if path == "/api/checkpoint":
                    note = body.get("note") or ""
                    if not isinstance(note, str):
                        raise ValueError("note 须为文本")
                    engine.resolve_checkpoint(body["task_id"], body["cp_id"], body["option"], by=user, note=note, data=body.get("data") or {})
                    st = engine.advance(body["task_id"], by=user)
                    return self._json(200, {"message": f"已处理，当前阶段：{st.stage.value}", "stage": st.stage.value})
                if path == "/api/revise":
                    engine.request_revision(body["task_id"], by=user, instruction=body.get("instruction"), edits=body.get("edits"), fact_changes=body.get("fact_changes"))
                    st = engine.advance(body["task_id"], by=user)
                    return self._json(200, {"message": f"已提交修订，当前阶段：{st.stage.value}", "stage": st.stage.value})
            except (KeyError, ValueError, TypeError, PermissionError) as exc:
                return self._json(400, {"message": f"处理失败：{exc}"})
            except Exception as exc:  # 失败必须可见：返回错误说明，而不是断开连接
                return self._json(500, {"message": f"处理失败：{type(exc).__name__}: {exc}"})
            return self._json(404, {"message": "not found"})

    return Handler


def serve(engine: Engine, user: Principal, host: str = "127.0.0.1", port: int = 8765) -> tuple[ThreadingHTTPServer, str]:
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("审阅服务只允许绑定本机回环地址")
    token = secrets.token_urlsafe(24)
    httpd = ThreadingHTTPServer((host, port), make_handler(engine, user, token))
    return httpd, token
