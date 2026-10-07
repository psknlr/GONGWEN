"""本地审阅服务：只绑定回环地址、写操作需令牌、人工可在浏览器中处理审核节点。"""

import json
import threading
import urllib.error
import urllib.request

import pytest
from test_engine_e2e import make_engine, start

from gongwen.orchestrator import default_user
from gongwen.schemas.facts import FactStatus
from gongwen.schemas.state import CheckpointKind, Stage
from gongwen.workbench.server import serve


@pytest.fixture()
def server(tmp_path):
    eng = make_engine(tmp_path)
    user = default_user()
    httpd, token = serve(eng, user, port=0)
    port = httpd.server_address[1]
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    yield eng, user, f"http://127.0.0.1:{port}", token
    httpd.shutdown()
    httpd.server_close()


def _get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, r.read().decode("utf-8")


def _post(url, body, headers=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST", headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_refuses_non_loopback(tmp_path):
    eng = make_engine(tmp_path)
    with pytest.raises(ValueError):
        serve(eng, default_user(), host="0.0.0.0", port=0)


def test_checkpoints_resolved_only_with_token(server):
    eng, user, base, token = server
    st = start(eng, user)
    st = eng.advance(st.task_id, by=user)
    assert st.stage == Stage.TASK_CONFIRM

    code, page = _get(base + "/")
    assert code == 200 and st.task_id in page
    code, page = _get(f"{base}/task/{st.task_id}")
    assert code == 200 and "任务契约确认" in page and '"api": "/api"' in page

    cp = next(c for c in st.pending_checkpoints() if c.kind == CheckpointKind.TASK_CONFIRM)
    body = {"task_id": st.task_id, "cp_id": cp.cp_id, "option": "accept"}
    code, _ = _post(base + "/api/checkpoint", body)
    assert code == 403  # 无令牌
    code, _ = _post(base + "/api/checkpoint", body, {"X-GW-Token": "guess"})
    assert code == 403
    code, _ = _post(base + "/api/checkpoint", body, {"X-GW-Token": token, "Origin": "https://evil.example"})
    assert code == 403  # 跨站来源
    assert eng.load_state(st.task_id).stage == Stage.TASK_CONFIRM

    code, res = _post(base + "/api/checkpoint", body, {"X-GW-Token": token})
    assert code == 200, res
    st = eng.load_state(st.task_id)
    assert st.stage == Stage.OUTLINE_CONFIRM

    ledger = eng.load_matter_ledger(st)
    money = [f.fact_id for f in ledger.facts if f.kind == "money" and f.status == FactStatus.RECORDED]
    cp = next(c for c in st.pending_checkpoints() if c.kind == CheckpointKind.OUTLINE_CONFIRM)
    code, res = _post(base + "/api/checkpoint", {"task_id": st.task_id, "cp_id": cp.cp_id, "option": "edit", "data": {"confirm_facts": money}}, {"X-GW-Token": token})
    assert code == 200 and res["stage"] == Stage.HUMAN_REVIEW.value

    code, page = _get(f"{base}/task/{st.task_id}")
    assert code == 200 and "data-sid" in page and "人工送审" in page
    code, status = _get(f"{base}/api/task/{st.task_id}")
    assert json.loads(status)["stage"] == Stage.HUMAN_REVIEW.value

    code, res = _post(base + "/api/checkpoint", {"task_id": st.task_id, "cp_id": "CP-404", "option": "submit"}, {"X-GW-Token": token})
    assert code == 400


def test_rejects_path_traversal_ids(server):
    eng, user, base, token = server
    for tid in ("..", "..%2F..%2Fetc", "T%00"):
        try:
            code, _ = _get(f"{base}/task/{tid}")
        except urllib.error.HTTPError as e:
            code = e.code
        assert code == 404
    assert not (eng.store.root / "etc").exists()
