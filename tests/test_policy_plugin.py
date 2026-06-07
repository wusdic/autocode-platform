"""policy_plugin 三道闸的单元测试（对应设计方案第 3.1、6.2 节）。"""
import policy_plugin as pp


# --- 闸门 1：非执行角色禁止执行类工具 ----------------------------------------
def test_ceo_blocked_from_terminal():
    res = pp.enforce("terminal", {}, role="ceo")
    assert res and res["action"] == "block"


def test_dev_lead_blocked_from_patch():
    res = pp.enforce("patch", {"path": "src/x.py"}, role="dev-lead")
    assert res and res["action"] == "block"


def test_pm_research_blocked_from_write_file():
    res = pp.enforce("write_file", {"path": "design/x.md"}, role="pm-research-a")
    assert res and res["action"] == "block"


def test_executor_role_allowed_to_run_terminal():
    # qa / release 是执行角色，不在 NO_EXEC_ROLES，应放行
    assert pp.enforce("terminal", {}, role="qa") is None
    assert pp.enforce("terminal", {}, role="release") is None


# --- 闸门 2：无 approved design_version 不准改代码 ----------------------------
def test_dev_worker_blocked_without_approved_design(tmp_path):
    ws = tmp_path
    (ws / "design").mkdir()
    res = pp.enforce("patch", {"path": "src/a.py"}, task_id="t1",
                     role="dev-worker-1", ws=str(ws))
    assert res and "approved design" in res["message"]


# --- 闸门 3：只能改 allowed_paths 内文件 -------------------------------------
def _ws_with_approved(tmp_path):
    ws = tmp_path
    (ws / "design").mkdir()
    (ws / "design" / "approved_versions.txt").write_text("v1\n")
    return ws


def test_dev_worker_allowed_when_no_allowed_paths_file(tmp_path):
    # 有批准版本、但没有 allowed_paths 文件 => 未约束，放行
    ws = _ws_with_approved(tmp_path)
    res = pp.enforce("patch", {"path": "src/a.py"}, task_id="t1",
                     role="dev-worker-1", ws=str(ws))
    assert res is None


def test_dev_worker_blocked_outside_allowed_paths(tmp_path):
    ws = _ws_with_approved(tmp_path)
    (ws / "design" / "allowed_paths.t1.txt").write_text("src/crud/\n")
    res = pp.enforce("patch", {"path": "src/storage/db.py"}, task_id="t1",
                     role="dev-worker-1", ws=str(ws))
    assert res and "allowed_paths" in res["message"]


def test_dev_worker_allowed_inside_allowed_paths(tmp_path):
    ws = _ws_with_approved(tmp_path)
    (ws / "design" / "allowed_paths.t1.txt").write_text("src/crud/\n")
    res = pp.enforce("patch", {"path": "src/crud/core.py"}, task_id="t1",
                     role="dev-worker-1", ws=str(ws))
    assert res is None


def test_register_wires_pre_tool_call():
    captured = {}

    class Ctx:
        def register_hook(self, event, fn):
            captured[event] = fn

    pp.register(Ctx())
    assert captured.get("pre_tool_call") is pp.enforce
