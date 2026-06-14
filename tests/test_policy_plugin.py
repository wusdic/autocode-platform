"""policy_plugin 三道闸的单元测试（对应设计方案第 3.1、6.2 节）。"""
import policy_plugin as pp


# --- 闸门 1：非执行角色禁止执行类工具 ----------------------------------------
def test_ceo_blocked_from_terminal():
    res = pp.enforce("terminal", {}, role="ceo")
    assert res and res["action"] == "block"


def test_dev_lead_blocked_from_patch():
    res = pp.enforce("patch", {"path": "src/x.py"}, role="dev-lead")
    assert res and res["action"] == "block"


def test_design_role_may_write_design_dir():
    # 设计角色写 design/ 应放行（不是一刀切禁止）
    assert pp.enforce("write_file", {"path": "design/PRD.md"}, role="pm-research-a") is None


def test_design_role_blocked_from_writing_code():
    # 但写 design/ 之外（疑似代码）应拦截
    res = pp.enforce("write_file", {"path": "src/app.py"}, role="pm-research-a")
    assert res and res["action"] == "block"


def test_synthesizer_can_write_approved_versions_key():
    # 死锁修复：synthesizer 必须能写 design/approved_versions.txt 这把"开闸钥匙"
    res = pp.enforce("write_file", {"path": "design/approved_versions.txt"},
                     role="arch-synthesizer")
    assert res is None


def test_change_guardian_can_write_design_but_not_run_terminal():
    assert pp.enforce("write_file", {"path": "design/impact.md"},
                      role="change-guardian") is None
    assert pp.enforce("terminal", {}, role="change-guardian")["action"] == "block"


def test_executor_role_allowed_to_run_terminal():
    # qa / release 是执行角色，不在 NO_CODE_ROLES，应放行
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


def test_dev_worker_blocked_when_no_allowed_paths_file(tmp_path):
    # fail-closed：有批准版本但缺 allowed_paths 文件 => 拒绝（必须先声明范围）
    ws = _ws_with_approved(tmp_path)
    res = pp.enforce("patch", {"path": "src/a.py"}, task_id="t1",
                     role="dev-worker-1", ws=str(ws))
    assert res and "allowed_paths" in res["message"]


def test_dev_worker_terminal_blocked_without_approved_design(tmp_path):
    # terminal 也能写文件，无批准设计时一并拦截（防绕过设计闸门）
    ws = tmp_path
    (ws / "design").mkdir()
    res = pp.enforce("terminal", {}, task_id="t1", role="dev-worker-1", ws=str(ws))
    assert res and "approved design" in res["message"]


def test_dev_worker_terminal_allowed_with_approved_design(tmp_path):
    ws = _ws_with_approved(tmp_path)
    assert pp.enforce("terminal", {}, task_id="t1",
                      role="dev-worker-1", ws=str(ws)) is None


def test_unknown_role_fails_closed_on_sensitive_tools():
    # 角色识别不出（如兜底的 ".hermes"/"unknown"）→ 敏感工具一律拒绝
    for bad in (".hermes", "unknown", ""):
        assert pp.enforce("write_file", {"path": "design/x.md"}, role=bad)["action"] == "block"
        assert pp.enforce("terminal", {}, role=bad)["action"] == "block"


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


# --- 角色识别（此前完全没覆盖，恰好是出过 bug 的路径）-----------------------
def test_resolve_role_prefers_explicit():
    assert pp.resolve_role("dev-worker-1", {"role": "ceo"}) == "dev-worker-1"


def test_resolve_role_from_kwargs():
    assert pp.resolve_role(None, {"profile": "qa"}) == "qa"


def test_resolve_role_from_env(monkeypatch):
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.setenv("HERMES_PROFILE", "release")
    assert pp.resolve_role(None, {}) == "release"


def test_resolve_role_from_profiles_path_segment(monkeypatch):
    # 真机运行时的文档化布局：HERMES_HOME=…/profiles/<role> → 应解析出 <role>
    for k in ("HERMES_PROFILE", "HERMES_PROFILE_NAME", "HERMES_AGENT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HERMES_HOME", "/data/projects/demo1/.hermes/profiles/dev-worker-1")
    assert pp.resolve_role(None, {}) == "dev-worker-1"
    monkeypatch.setenv("HERMES_HOME", "/data/projects/demo1/.hermes/profiles/ceo")
    assert pp.resolve_role(None, {}) == "ceo"


def test_resolve_role_does_not_use_hermes_home_basename(monkeypatch):
    # 关键回归：HERMES_HOME 末段是 ".hermes"，绝不能被当成角色名误判为可执行
    for k in ("HERMES_PROFILE", "HERMES_PROFILE_NAME", "HERMES_AGENT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HERMES_HOME", "/data/projects/demo1/.hermes")
    assert pp.resolve_role(None, {}) == ".hermes"   # 兜底值，不等于任何真实角色
    # 因为它不在 NO_CODE_ROLES，也不以 dev-worker 开头，enforce 不会误判；
    # 真实部署必须让 Hermes 通过 kwargs/env 提供真角色（见 resolve_role 文档）。


def test_enforce_blocks_when_role_supplied_via_kwargs():
    # 模拟 Hermes 通过 kwargs 传 profile=ceo 的情形
    res = pp.enforce("terminal", {}, profile="ceo")
    assert res and res["action"] == "block"


def test_register_wires_pre_tool_call():
    captured = {}

    class Ctx:
        def register_hook(self, event, fn):
            captured[event] = fn

    pp.register(Ctx())
    assert captured.get("pre_tool_call") is pp.enforce
