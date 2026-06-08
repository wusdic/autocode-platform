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
    r = client.post("/api/projects", json={"project_id": "p1"}, headers=_h("wrong"))
    assert r.status_code == 401


def test_create_and_duplicate_project(client):
    r = client.post("/api/projects", json={"project_id": "p1"}, headers=_h())
    assert r.status_code == 200
    body = r.json()
    assert body["project_id"] == "p1" and body["status"] == "ready"
    # 重复创建 → 409
    r2 = client.post("/api/projects", json={"project_id": "p1"}, headers=_h())
    assert r2.status_code == 409


def test_unknown_project_returns_404(client):
    r = client.get("/api/projects/nope/tasks", headers=_h())
    assert r.status_code == 404


def test_message_ceo_roundtrip(client):
    client.post("/api/projects", json={"project_id": "p1"}, headers=_h())
    r = client.post("/api/projects/p1/messages",
                    json={"message": "hello"}, headers=_h())
    assert r.status_code == 200
    assert "echo: hello" in r.json()["content"]


def test_list_tasks(client):
    client.post("/api/projects", json={"project_id": "p1"}, headers=_h())
    r = client.get("/api/projects/p1/tasks", headers=_h())
    assert r.status_code == 200 and r.json()[0]["id"] == "t1"


def test_requirements_absent_then_present(client, tmp_path):
    client.post("/api/projects", json={"project_id": "p1"}, headers=_h())
    r = client.get("/api/projects/p1/requirements", headers=_h())
    assert r.json()["requirements"] is None
    # 写入 requirements.yaml 后再读
    design = tmp_path / "p1" / "workspace" / "design"
    design.mkdir(parents=True)
    (design / "requirements.yaml").write_text("core_need: build it\n")
    r2 = client.get("/api/projects/p1/requirements", headers=_h())
    assert "core_need" in r2.json()["requirements"]


def test_change_request_creates_guardian_card(client):
    client.post("/api/projects", json={"project_id": "p1"}, headers=_h())
    r = client.post("/api/projects/p1/change-requests",
                    json={"change": "支持优先级排序"}, headers=_h())
    assert r.status_code == 200
    title, assignee, extra = client.gateway.created[-1]
    assert assignee == "change-guardian" and "优先级排序" in title
    assert "--goal" in extra


def test_port_allocator_persists_across_restart(tmp_path):
    # 第一次分配
    a1 = cp.PortAllocator(str(tmp_path), base_port=8650)
    assert a1.allocate("p1") == 8650
    assert a1.allocate("p2") == 8651
    assert a1.allocate("p1") == 8650          # 幂等：同 pid 同端口
    # 模拟控制平面重启：新实例从磁盘恢复 next，不撞已分配端口
    a2 = cp.PortAllocator(str(tmp_path), base_port=8650)
    assert a2.allocate("p3") == 8652
    assert a2.allocate("p1") == 8650


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
    client.post("/api/projects", json={"project_id": "p1"}, headers=_h())
    src = tmp_path / "p1" / "workspace" / "src"
    src.mkdir(parents=True)
    (src / "main.py").write_text("print('hi')\n")
    r = client.get("/api/projects/p1/artifacts", headers=_h())
    paths = [a["path"] for a in r.json()["artifacts"]]
    assert "src/main.py" in paths
