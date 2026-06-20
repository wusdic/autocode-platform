"""Orchestrator 状态机测试：注入 FakeGateway + 临时 workspace，覆盖各阶段跃迁 + 幂等。"""
import json
from pathlib import Path

import orchestrator as orch_mod
from control_plane import Project


class FakeGateway:
    def __init__(self):
        self.swarms = []
        self.created = []
        self.cards = []   # 测试可改

    def swarm(self, project, goal, workers, verifier, synthesizer):
        self.swarms.append((goal, workers, verifier, synthesizer))

    def kanban_create(self, project, title, assignee, *extra):
        self.created.append((title, assignee, extra))

    def kanban(self, project, *args):
        return self.cards


def _project(tmp_path):
    """每个测试独立 data_root（tmp_path/data），项目在 data_root/demo1。"""
    data_root = tmp_path / "data"
    proj = data_root / "demo1"
    (proj / ".hermes").mkdir(parents=True)
    ws = proj / "workspace"
    (ws / "design").mkdir(parents=True)
    return Project("demo1", 0, "", str(proj / ".hermes"), str(ws)), ws, data_root


def _orch(data_root, gw):
    return orch_mod.Orchestrator(gw, data_root=str(data_root))


def test_prd_triggers_architecture_swarm(tmp_path):
    gw = FakeGateway()
    project, ws, data_root = _project(tmp_path)
    (ws / "design" / "PRD.md").write_text("prd")
    stage = _orch(data_root, gw).tick(project)
    assert stage == "architecture"
    assert gw.swarms and gw.swarms[0][1] == ["arch-simple", "arch-scale", "arch-security"]
    # 幂等：再 tick 不重复起 swarm
    _orch(data_root, gw).tick(project)
    assert len(gw.swarms) == 1


def test_adr_and_approved_triggers_dev_plan(tmp_path):
    gw = FakeGateway()
    project, ws, data_root = _project(tmp_path)
    (ws / "design" / "PRD.md").write_text("prd")
    (ws / "design" / "ADR.md").write_text("adr")
    (ws / "design" / "approved_versions.txt").write_text("v1\n")
    stage = _orch(data_root, gw).tick(project)
    assert stage == "development"
    assert any(a == "dev-lead" for _, a, _ in gw.created)


def test_all_dev_done_triggers_qa(tmp_path):
    gw = FakeGateway()
    project, ws, data_root = _project(tmp_path)
    for f in ("PRD.md", "ADR.md"):
        (ws / "design" / f).write_text("x")
    (ws / "design" / "approved_versions.txt").write_text("v1\n")
    # 先推进到 development
    _orch(data_root, gw).tick(project)
    # dev 卡全部 done
    gw.cards = [{"id": "d1", "assignee": "dev-worker-1", "status": "done"},
                {"id": "d2", "assignee": "dev-worker-2", "status": "done"}]
    stage = _orch(data_root, gw).tick(project)
    assert stage == "qa"
    assert any(a == "qa" for _, a, _ in gw.created)


def test_dev_not_done_does_not_trigger_qa(tmp_path):
    gw = FakeGateway()
    project, ws, data_root = _project(tmp_path)
    for f in ("PRD.md", "ADR.md"):
        (ws / "design" / f).write_text("x")
    (ws / "design" / "approved_versions.txt").write_text("v1\n")
    _orch(data_root, gw).tick(project)
    gw.cards = [{"id": "d1", "assignee": "dev-worker-1", "status": "in_progress"}]
    _orch(data_root, gw).tick(project)
    assert not any(a == "qa" for _, a, _ in gw.created)


def _advance_to_qa(gw, project, ws, data_root):
    """把项目推进到 qa_started=True（PRD→架构→dev→dev全done→QA）。"""
    for f in ("PRD.md", "ADR.md"):
        (ws / "design" / f).write_text("x")
    (ws / "design" / "approved_versions.txt").write_text("v1\n")
    _orch(data_root, gw).tick(project)
    gw.cards = [{"id": "d1", "assignee": "dev-worker-1", "status": "done"}]
    assert _orch(data_root, gw).tick(project) == "qa"


def test_qa_pass_triggers_release_then_complete(tmp_path):
    gw = FakeGateway()
    project, ws, data_root = _project(tmp_path)
    _advance_to_qa(gw, project, ws, data_root)
    qa = ws / "reports" / "qa"
    qa.mkdir(parents=True)
    (qa / "status.json").write_text('{"release_allowed": true}')
    stage = _orch(data_root, gw).tick(project)
    assert stage == "release"
    assert any(a == "release" for _, a, _ in gw.created)
    # release 卡 done → complete
    gw.cards = [{"id": "r1", "assignee": "release", "status": "done"}]
    assert _orch(data_root, gw).tick(project) == "complete"


def test_stale_qa_status_does_not_trigger_release(tmp_path):
    """残留旧 reports/qa/status.json（release_allowed=true）在本轮未跑 QA 时不得误触发 release。"""
    gw = FakeGateway()
    project, ws, data_root = _project(tmp_path)
    qa = ws / "reports" / "qa"
    qa.mkdir(parents=True)
    (qa / "status.json").write_text('{"release_allowed": true}')
    # 仅有 PRD（未到 QA 阶段），残留旧放行文件不得触发 release
    (ws / "design" / "PRD.md").write_text("prd")
    _orch(data_root, gw).tick(project)
    assert not any(a == "release" for _, a, _ in gw.created)


def test_provider_pause_blocks_new_swarm(tmp_path):
    gw = FakeGateway()
    project, ws, data_root = _project(tmp_path)
    (ws / "design" / "PRD.md").write_text("prd")
    (data_root / ".provider_pause").write_text(str(2 ** 31))  # 远未来
    stage = _orch(data_root, gw).tick(project)
    assert stage != "architecture" and not gw.swarms


def test_state_persisted(tmp_path):
    gw = FakeGateway()
    project, ws, data_root = _project(tmp_path)
    (ws / "design" / "PRD.md").write_text("prd")
    _orch(data_root, gw).tick(project)
    state = json.loads((ws / ".autocode" / "state.json").read_text())
    assert state["arch_started"] is True and state["stage"] == "architecture"


def test_tick_all_scans_projects(tmp_path):
    gw = FakeGateway()
    _, ws, data_root = _project(tmp_path)
    (ws / "design" / "PRD.md").write_text("prd")
    result = _orch(data_root, gw).tick_all()
    assert result.get("demo1") == "architecture"
