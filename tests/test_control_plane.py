"""控制平面端点测试：用注入的 FakeGateway 替换真实 Hermes 交互。"""
import pytest
from fastapi.testclient import TestClient

import control_plane as cp

TOKEN = "test-token"


class FakeGateway:
    """模拟 HermesGateway，不触碰真实 hermes / 子进程 / HTTP。"""

    def __init__(self, settings):
        self.settings = settings
        self.created = []   # 记录 kanban_create 调用
        self.swarms = []    # 记录 swarm 调用

    def launch(self, pid, port):
        workspace = f"{self.settings.data_root}/{pid}/workspace"
        return cp.Project(
            project_id=pid, port=port, key="fake-key",
            home=f"{self.settings.data_root}/{pid}/.hermes", workspace=workspace,
        )

    async def chat(self, project, message, session_id):
        return {"role": "assistant", "content": f"[{project.project_id}] echo: {message}"}

    def kanban(self, project, *args):
        return [{"id": "t1", "title": "demo", "status": "ready"}]

    def kanban_create(self, project, title, assignee, *extra):
        self.created.append((title, assignee, extra))

    def swarm(self, project, goal, workers, verifier, synthesizer):
        self.swarms.append((goal, workers, verifier, synthesizer))


@pytest.fixture
def client(tmp_path):
    settings = cp.Settings(token=TOKEN, base_port=9000, data_root=str(tmp_path))
    gateway = FakeGateway(settings)
    app = cp.create_app(settings=settings, gateway=gateway)
    c = TestClient(app)
    c.gateway = gateway          # 暴露给测试断言
    c.data_root = str(tmp_path)
    return c


def _h(token=TOKEN):
    return {"X-Token": token}


def test_auth_rejects_bad_token(client):
    r = client.post("/api/projects", json={"project_id": "proj1"}, headers=_h("wrong"))
    assert r.status_code == 401


# --- D18：建项目失败要回滚端口、回 502，不泄漏孤儿端口 ----------------------------
def test_create_project_rolls_back_on_launch_failure(tmp_path):
    class FailingGateway(FakeGateway):
        def launch(self, pid, port):
            raise RuntimeError("disk full")
    settings = cp.Settings(token=TOKEN, base_port=9000, data_root=str(tmp_path))
    gw = FailingGateway(settings)
    app = cp.create_app(settings=settings, gateway=gw)
    c = TestClient(app)
    r = c.post("/api/projects", json={"project_id": "projx"}, headers=_h())
    assert r.status_code == 502 and "disk full" in r.json()["detail"]
    # 端口已回滚：.ports.json 不应残留 projx
    import json as _j
    ports = _j.loads((tmp_path / ".ports.json").read_text())
    assert "projx" not in ports.get("assigned", {})


# --- D20：单个项目 kanban 超时/失败不应让 /api/projects 整体 500 ------------------
def test_list_projects_survives_kanban_error(tmp_path):
    class HangingGateway(FakeGateway):
        def kanban(self, project, *args):
            raise RuntimeError("hermes kanban timed out after 15s")
    settings = cp.Settings(token=TOKEN, base_port=9000, data_root=str(tmp_path))
    gw = HangingGateway(settings)
    app = cp.create_app(settings=settings, gateway=gw)
    c = TestClient(app)
    c.post("/api/projects", json={"project_id": "proj1"}, headers=_h())
    r = c.get("/api/projects", headers=_h())
    assert r.status_code == 200
    p = next(p for p in r.json()["projects"] if p["project_id"] == "proj1")
    assert p["task_summary"] == {} and p["total_tasks"] == 0


# --- 用户面只读端点（Web UI 后端）-------------------------------------------------
def _mk(client, pid="proj1"):
    client.post("/api/projects", json={"project_id": pid}, headers=_h())
    from pathlib import Path
    return Path(client.data_root) / pid / "workspace"


def test_list_projects_returns_created(client):
    _mk(client)
    r = client.get("/api/projects", headers=_h())
    assert r.status_code == 200
    ids = [p["project_id"] for p in r.json()["projects"]]
    assert "proj1" in ids
    p = next(p for p in r.json()["projects"] if p["project_id"] == "proj1")
    assert "stage" in p and "task_summary" in p


def test_list_projects_requires_token(client):
    assert client.get("/api/projects").status_code == 401


def test_project_state_reports_design_files(client):
    ws = _mk(client)
    (ws / "design").mkdir(parents=True, exist_ok=True)
    (ws / "design" / "PRD.md").write_text("prd")
    r = client.get("/api/projects/proj1/state", headers=_h())
    assert r.status_code == 200
    assert r.json()["design_files"]["PRD.md"] is True
    assert r.json()["design_files"]["ADR.md"] is False


def test_artifact_content_allows_whitelisted(client):
    ws = _mk(client)
    (ws / "design").mkdir(parents=True, exist_ok=True)
    (ws / "design" / "PRD.md").write_text("# hello")
    r = client.get("/api/projects/proj1/artifact-content",
                   params={"path": "design/PRD.md"}, headers=_h())
    assert r.status_code == 200 and r.json()["content"] == "# hello"


def test_artifact_content_blocks_traversal(client):
    _mk(client)
    r = client.get("/api/projects/proj1/artifact-content",
                   params={"path": "../../etc/passwd"}, headers=_h())
    assert r.status_code == 403


def test_artifact_content_blocks_internal_dirs(client):
    ws = _mk(client)
    (ws / ".autocode").mkdir(parents=True, exist_ok=True)
    (ws / ".autocode" / "state.json").write_text("{}")
    r = client.get("/api/projects/proj1/artifact-content",
                   params={"path": ".autocode/state.json"}, headers=_h())
    assert r.status_code == 403


def test_conversation_reads_platform_jsonl(client):
    _mk(client)
    client.post("/api/projects/proj1/messages",
                json={"message": "hi", "session_id": "main"}, headers=_h())
    r = client.get("/api/projects/proj1/conversation",
                   params={"session_id": "main"}, headers=_h())
    assert r.status_code == 200
    roles = [m["role"] for m in r.json()["messages"]]
    assert "user" in roles and "assistant" in roles


def test_message_rejects_traversal_session_id(client):
    _mk(client)
    r = client.post("/api/projects/proj1/messages",
                    json={"message": "x", "session_id": "../../etc/cron"}, headers=_h())
    assert r.status_code == 400
    # 确认没在 conversations 目录外落文件
    from pathlib import Path
    assert not (Path(client.data_root) / "proj1" / "etc").exists()


def test_conversation_rejects_traversal_session_id(client):
    _mk(client)
    r = client.get("/api/projects/proj1/conversation",
                   params={"session_id": "../../../secret"}, headers=_h())
    assert r.status_code == 400


def test_message_returns_502_when_ceo_unreachable(tmp_path):
    class DownGateway(FakeGateway):
        async def chat(self, project, message, session_id):
            raise RuntimeError("connection refused")
    settings = cp.Settings(token=TOKEN, base_port=9000, data_root=str(tmp_path))
    app = cp.create_app(settings=settings, gateway=DownGateway(settings))
    c = TestClient(app)
    c.post("/api/projects", json={"project_id": "proj1"}, headers=_h())
    r = c.post("/api/projects/proj1/messages",
               json={"message": "hi", "session_id": "main"}, headers=_h())
    assert r.status_code == 502 and "CEO" in r.json()["detail"]


def test_create_and_duplicate_project(client):
    r = client.post("/api/projects", json={"project_id": "proj1"}, headers=_h())
    assert r.status_code == 200
    body = r.json()
    assert body["project_id"] == "proj1" and body["status"] == "ready"
    # 重复创建 → 409
    r2 = client.post("/api/projects", json={"project_id": "proj1"}, headers=_h())
    assert r2.status_code == 409


def test_unknown_project_returns_404(client):
    r = client.get("/api/projects/nope/tasks", headers=_h())
    assert r.status_code == 404


def test_invalid_project_id_rejected(client):
    # 路径穿越 / 非法字符必须被拒
    for bad in ["../etc", "a/b", "Bad ID", "x", "UPPER"]:
        r = client.post("/api/projects", json={"project_id": bad}, headers=_h())
        assert r.status_code == 400, bad


def test_confirm_plan_creates_product_swarm(client):
    client.post("/api/projects", json={"project_id": "proj1"}, headers=_h())
    r = client.post("/api/projects/proj1/confirm-plan", headers=_h())
    assert r.status_code == 200
    assert r.json()["swarm"] == "product-council"
    goal, workers, verifier, synthesizer = client.gateway.swarms[-1]
    assert workers == ["pm-research-a", "pm-research-b"]
    assert verifier == "pm-critic" and synthesizer == "pm-synthesizer"


def test_architecture_swarm_endpoint(client):
    client.post("/api/projects", json={"project_id": "proj1"}, headers=_h())
    r = client.post("/api/projects/proj1/architecture-swarm", headers=_h())
    assert r.status_code == 200 and r.json()["swarm"] == "architecture-council"
    _, workers, verifier, synthesizer = client.gateway.swarms[-1]
    assert workers == ["arch-simple", "arch-scale", "arch-security"]
    assert verifier == "arch-critic" and synthesizer == "arch-synthesizer"


def test_architecture_swarm_endpoint_idempotent(client):
    """评审 D：手动重复调用不得重复起架构委员会 swarm（与 orchestrator 共享 arch_started）。"""
    client.post("/api/projects", json={"project_id": "proj1"}, headers=_h())
    client.post("/api/projects/proj1/architecture-swarm", headers=_h())
    n_after_first = len(client.gateway.swarms)
    r = client.post("/api/projects/proj1/architecture-swarm", headers=_h())
    assert r.status_code == 200
    assert r.json()["status"] == "architecture-swarm-already-started"
    assert len(client.gateway.swarms) == n_after_first  # 没有第二次 swarm


def test_swarm_cmd_uses_singular_worker_flag(monkeypatch, tmp_path):
    """回归 NEW-E：真实 HermesGateway.swarm 必须用单数可重复 --worker。"""
    calls = {}

    def fake_run(cmd, env=None, check=False, **kw):
        calls["cmd"] = list(cmd)

    monkeypatch.setattr(cp.subprocess, "run", fake_run)
    gw = cp.HermesGateway(cp.Settings(data_root=str(tmp_path)))
    proj = cp.Project("proj1", 8650, "k", str(tmp_path / ".hermes"), str(tmp_path / "ws"))
    gw.swarm(proj, "goal", ["pm-research-a", "pm-research-b"], "pm-critic", "pm-synthesizer")
    cmd = calls["cmd"]
    assert "--workers" not in cmd
    assert cmd.count("--worker") == 2
    assert "pm-research-a:pm-research-a" in cmd and "pm-research-b:pm-research-b" in cmd


def test_confirm_plan_persists_requirements(client, tmp_path):
    # 闭环验证：confirm-plan 带 requirements → 落盘，GET /requirements 能读到
    client.post("/api/projects", json={"project_id": "proj1"}, headers=_h())
    r = client.post("/api/projects/proj1/confirm-plan", headers=_h(),
                    json={"requirements": "core_need: build a CLI todo\n"})
    assert r.status_code == 200
    got = client.get("/api/projects/proj1/requirements", headers=_h())
    assert "core_need" in (got.json()["requirements"] or "")


def test_message_ceo_roundtrip(client):
    client.post("/api/projects", json={"project_id": "proj1"}, headers=_h())
    r = client.post("/api/projects/proj1/messages",
                    json={"message": "hello"}, headers=_h())
    assert r.status_code == 200
    assert "echo: hello" in r.json()["content"]


def test_list_tasks(client):
    client.post("/api/projects", json={"project_id": "proj1"}, headers=_h())
    r = client.get("/api/projects/proj1/tasks", headers=_h())
    assert r.status_code == 200 and r.json()[0]["id"] == "t1"


def test_kanban_failure_returns_502(tmp_path):
    class FailingGateway(FakeGateway):
        def kanban(self, project, *args):
            raise RuntimeError("hermes kanban failed: boom")

    settings = cp.Settings(token=TOKEN, base_port=9000, data_root=str(tmp_path))
    app = cp.create_app(settings=settings, gateway=FailingGateway(settings))
    c = TestClient(app)
    c.post("/api/projects", json={"project_id": "proj1"}, headers=_h())
    r = c.get("/api/projects/proj1/tasks", headers=_h())
    assert r.status_code == 502


def test_requirements_absent_then_present(client, tmp_path):
    client.post("/api/projects", json={"project_id": "proj1"}, headers=_h())
    r = client.get("/api/projects/proj1/requirements", headers=_h())
    assert r.json()["requirements"] is None
    # 写入 requirements.yaml 后再读
    design = tmp_path / "proj1" / "workspace" / "design"
    design.mkdir(parents=True)
    (design / "requirements.yaml").write_text("core_need: build it\n")
    r2 = client.get("/api/projects/proj1/requirements", headers=_h())
    assert "core_need" in r2.json()["requirements"]


def test_change_request_creates_guardian_card(client):
    client.post("/api/projects", json={"project_id": "proj1"}, headers=_h())
    r = client.post("/api/projects/proj1/change-requests",
                    json={"change": "支持优先级排序"}, headers=_h())
    assert r.status_code == 200
    title, assignee, extra = client.gateway.created[-1]
    assert assignee == "change-guardian" and "优先级排序" in title
    assert "--goal" in extra


def test_port_allocator_persists_across_restart(tmp_path):
    # 第一次分配
    a1 = cp.PortAllocator(str(tmp_path), base_port=8650)
    assert a1.allocate("proj1") == 8650
    assert a1.allocate("p2") == 8651
    assert a1.allocate("proj1") == 8650          # 幂等：同 pid 同端口
    # 模拟控制平面重启：新实例从磁盘恢复 next，不撞已分配端口
    a2 = cp.PortAllocator(str(tmp_path), base_port=8650)
    assert a2.allocate("p3") == 8652
    assert a2.allocate("proj1") == 8650


def test_rehydrate_restores_project_from_disk(tmp_path):
    # 在磁盘上伪造一个已存在的项目
    env = tmp_path / "old" / ".hermes" / "profiles" / "ceo"
    env.mkdir(parents=True)
    (env / ".env").write_text("API_SERVER_PORT=8655\nAPI_SERVER_KEY=secret\n")
    settings = cp.Settings(token=TOKEN, base_port=8650, data_root=str(tmp_path))
    app = cp.create_app(settings=settings, gateway=FakeGateway(settings))
    c = TestClient(app)
    # 重启后无需重新创建即可访问老项目
    r = c.get("/api/projects/old/tasks", headers=_h())
    assert r.status_code == 200


def test_artifacts_lists_workspace_files(client, tmp_path):
    client.post("/api/projects", json={"project_id": "proj1"}, headers=_h())
    src = tmp_path / "proj1" / "workspace" / "src"
    src.mkdir(parents=True)
    (src / "main.py").write_text("print('hi')\n")
    r = client.get("/api/projects/proj1/artifacts", headers=_h())
    paths = [a["path"] for a in r.json()["artifacts"]]
    assert "src/main.py" in paths
