"""Orchestrator 状态机测试：注入 FakeGateway + 临时 workspace，覆盖各阶段跃迁 + 幂等。"""
import json
import subprocess
from pathlib import Path

import orchestrator as orch_mod
from control_plane import Project


def _git(ws, *a):
    subprocess.run(["git", "-C", str(ws), *a], check=True, capture_output=True, text=True)


def _init_repo(ws, with_src=False):
    _git(ws, "init", "-q")
    _git(ws, "config", "user.email", "a@b")
    _git(ws, "config", "user.name", "a")
    _git(ws, "commit", "-q", "--allow-empty", "-m", "init workspace")
    if with_src:
        (ws / "src").mkdir(exist_ok=True)
        (ws / "src" / "a.py").write_text("x = 1\n")
        _git(ws, "add", "-A")
        _git(ws, "commit", "-q", "-m", "feat: a")


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


def test_noncanonical_prd_filename_tolerated_with_warning(tmp_path):
    gw = FakeGateway()
    project, ws, data_root = _project(tmp_path)
    (ws / "design" / "prd-todo-cli.md").write_text("prd")   # agent 自由命名
    assert _orch(data_root, gw).tick(project) == "architecture"
    warns = (ws / ".autocode" / "warnings.jsonl").read_text()
    assert "noncanonical_design_filename" in warns


def test_adr_without_approved_creates_repair_card(tmp_path):
    gw = FakeGateway()
    project, ws, data_root = _project(tmp_path)
    (ws / "design" / "PRD.md").write_text("p")
    (ws / "design" / "ADR.md").write_text("a")   # ADR 出了但没 approved_versions.txt
    _orch(data_root, gw).tick(project)
    assert not any(a == "dev-lead" and "切分" in t for t, a, _ in gw.created)  # 没进开发
    assert any(a == "arch-synthesizer" and "补齐架构批准文件" in t for t, a, _ in gw.created)


def test_direct_to_qa_when_no_fanout_but_source_committed(tmp_path):
    # D30 + 第十轮收紧：dev-lead done、无 dev-worker 卡、源码落地 **且 git 有真实提交** → 进 QA。
    # 提交判据与 min_release_ok 同口径——没有提交的散码不进 QA（防 release 前死锁）。
    gw = FakeGateway()
    project, ws, data_root = _project(tmp_path)
    for f in ("PRD.md", "ADR.md"):
        (ws / "design" / f).write_text("x")
    (ws / "design" / "approved_versions.txt").write_text("v1\n")
    _orch(data_root, gw).tick(project)   # → development
    _init_repo(ws, with_src=True)        # 源码已真实提交（baseline 已完成）
    gw.cards = [{"id": "L", "assignee": "dev-lead", "status": "done"}]
    assert _orch(data_root, gw).tick(project) == "qa"


def test_uncommitted_source_spawns_baseline_validation_not_qa(tmp_path):
    # 第十轮 P0：源码落地但**无真实提交** → 不进 QA；自愈建 baseline-validation 卡给
    # dev-worker（dev-lead 无 terminal，不可能自己 commit/test——派给它必死锁）。
    gw = FakeGateway()
    project, ws, data_root = _project(tmp_path)
    for f in ("PRD.md", "ADR.md"):
        (ws / "design" / f).write_text("x")
    (ws / "design" / "approved_versions.txt").write_text("v1\n")
    _orch(data_root, gw).tick(project)   # → development
    _init_repo(ws)                       # 仅 init 提交
    (ws / "src").mkdir(exist_ok=True); (ws / "src" / "main.py").write_text("print(1)\n")  # 未提交
    gw.cards = [{"id": "L", "assignee": "dev-lead", "status": "done"}]
    stage = _orch(data_root, gw).tick(project)
    assert stage != "qa"
    assert any(a == "dev-worker-1" and "baseline-validation" in t for t, a, _ in gw.created)
    n = len(gw.created)
    _orch(data_root, gw).tick(project)   # 幂等：不重复建卡
    assert len(gw.created) == n


def test_direct_to_qa_blocked_when_no_source(tmp_path):
    # 反例：dev-lead done 但无源码 → 不进 QA（防空手放行）
    gw = FakeGateway()
    project, ws, data_root = _project(tmp_path)
    for f in ("PRD.md", "ADR.md"):
        (ws / "design" / f).write_text("x")
    (ws / "design" / "approved_versions.txt").write_text("v1\n")
    _orch(data_root, gw).tick(project)
    gw.cards = [{"id": "L", "assignee": "dev-lead", "status": "done"}]
    assert _orch(data_root, gw).tick(project) != "qa"


def test_billing_dead_blocks_new_swarm(tmp_path):
    gw = FakeGateway()
    project, ws, data_root = _project(tmp_path)
    (ws / "design" / "PRD.md").write_text("prd")
    (data_root / ".provider_billing_dead").write_text("insufficient balance")
    assert _orch(data_root, gw).tick(project) != "architecture" and not gw.swarms


def test_qa_done_without_status_creates_repair_card(tmp_path):
    gw = FakeGateway()
    project, ws, data_root = _project(tmp_path)
    _advance_to_qa(gw, project, ws, data_root)   # qa_started, 无 status.json
    gw.cards = [{"id": "q1", "assignee": "qa", "status": "done"}]
    _orch(data_root, gw).tick(project)
    assert any(a == "qa" and "补齐 QA 结论" in t for t, a, _ in gw.created)


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
    # release 卡 done 但缺 manifest → 不 complete，建补齐卡
    gw.cards = [{"id": "r1", "assignee": "release", "status": "done"}]
    assert _orch(data_root, gw).tick(project) != "complete"
    assert any("发布清单" in t for t, _, _ in gw.created)
    # 补上 release manifest → complete（自然完成）
    rel = ws / "reports" / "release"; rel.mkdir(parents=True)
    (rel / "manifest.json").write_text('{"version": "0.1.0", "run_command": "python src/main.py"}')
    assert _orch(data_root, gw).tick(project) == "complete"
    import json as _j
    st = _j.loads((ws / ".autocode" / "state.json").read_text())
    assert st.get("completion_mode") == "natural"


def test_complete_archives_leftover_cards(tmp_path):
    # D31（第十轮重设计）：complete 时归档所有仍非 done 的遗留卡（被 repair 旁路
    # supersede 的 blocked 卡不再永久污染看板）。cancel 失败仅记审计、不阻断收口。
    class ArchivingGateway(FakeGateway):
        def __init__(self):
            super().__init__()
            self.cancelled = []
        def cancel_card(self, project, tid):
            self.cancelled.append(tid); return True
    gw = ArchivingGateway()
    project, ws, data_root = _project(tmp_path)
    _advance_to_qa(gw, project, ws, data_root)
    (ws / "reports" / "qa").mkdir(parents=True)
    (ws / "reports" / "qa" / "status.json").write_text('{"release_allowed": true}')
    _orch(data_root, gw).tick(project)          # → release
    rel = ws / "reports" / "release"; rel.mkdir(parents=True)
    (rel / "manifest.json").write_text('{"version":"0.1.0","run_command":"python src/main.py"}')
    gw.cards = [
        {"id": "r1", "assignee": "release", "status": "done"},
        {"id": "z1", "assignee": "arch-critic", "status": "blocked"},   # 被 supersede 的遗留卡
        {"id": "z2", "assignee": "arch-synthesizer", "status": "todo"},
    ]
    assert _orch(data_root, gw).tick(project) == "complete"
    assert set(gw.cancelled) == {"z1", "z2"}    # 非 done 卡全归档，done 卡不动
    # 审计有 card_archived 事件
    import json as _j
    events = [_j.loads(l) for l in (ws / ".autocode" / "audit.jsonl").read_text().splitlines()]
    assert sum(1 for e in events if e["action"] == "card_archived") == 2


def test_complete_tolerates_gateway_without_cancel(tmp_path):
    # FakeGateway 无 cancel_card（旧网关/降级实现）→ complete 仍正常收口，不炸
    gw = FakeGateway()
    project, ws, data_root = _project(tmp_path)
    _advance_to_qa(gw, project, ws, data_root)
    (ws / "reports" / "qa").mkdir(parents=True)
    (ws / "reports" / "qa" / "status.json").write_text('{"release_allowed": true}')
    _orch(data_root, gw).tick(project)
    rel = ws / "reports" / "release"; rel.mkdir(parents=True)
    (rel / "manifest.json").write_text('{"version":"0.1.0","run_command":"x"}')
    gw.cards = [{"id": "r1", "assignee": "release", "status": "done"},
                {"id": "z1", "assignee": "arch-critic", "status": "blocked"}]
    assert _orch(data_root, gw).tick(project) == "complete"


def test_integrity_gate_blocks_release_when_no_artifacts(tmp_path):
    """dev 卡 done + QA 放行，但 git 只有 init 提交（产物没落地）→ 不起 release，建复验卡。"""
    gw = FakeGateway()
    project, ws, data_root = _project(tmp_path)
    _advance_to_qa(gw, project, ws, data_root)
    _init_repo(ws, with_src=False)  # 仅 init 提交
    qa = ws / "reports" / "qa"; qa.mkdir(parents=True)
    (qa / "status.json").write_text('{"release_allowed": true}')
    stage = _orch(data_root, gw).tick(project)
    assert stage != "release"
    assert not any(a == "release" for _, a, _ in gw.created)
    assert any("产物落地校验失败" in t for t, _, _ in gw.created)
    state = json.loads((ws / ".autocode" / "state.json").read_text())
    assert state.get("integrity_blocked") is True


def test_integrity_gate_passes_when_artifacts_present(tmp_path):
    """补上真实提交后，完整性闸门放行 → 起 release。"""
    gw = FakeGateway()
    project, ws, data_root = _project(tmp_path)
    _advance_to_qa(gw, project, ws, data_root)
    _init_repo(ws, with_src=True)  # init + feat 提交
    qa = ws / "reports" / "qa"; qa.mkdir(parents=True)
    (qa / "status.json").write_text('{"release_allowed": true}')
    stage = _orch(data_root, gw).tick(project)
    assert stage == "release"
    assert any(a == "release" for _, a, _ in gw.created)


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
