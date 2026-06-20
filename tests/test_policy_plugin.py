"""policy_plugin 各闸的单元测试（no-code / QA / release-QA-gate / dev-worker 设计闸门 /
fail-closed），对应设计方案第 3.1、6.2、6.3 节。"""
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


def test_design_role_escape_via_dotdot_blocked(tmp_path):
    # design/../src/evil.py 规范化后落在 design/ 外，必须拦截
    (tmp_path / "design").mkdir()
    res = pp.enforce("write_file", {"path": "design/../src/evil.py"},
                     role="pm-research-a", ws=str(tmp_path))
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


def test_qa_role_allowed_to_run_terminal(tmp_path):
    # qa 是执行角色，跑测试需要 terminal（不受 release 的 QA gate 限制）
    assert pp.enforce("terminal", {}, role="qa", ws=str(tmp_path)) is None


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


def test_dev_worker_strict_blocked_when_no_allowed_paths_file(tmp_path, monkeypatch):
    # 严格模式（POLICY_REQUIRE_TASK_ID=1）：有批准版本但缺 allowed_paths => 拒绝
    monkeypatch.setenv("POLICY_REQUIRE_TASK_ID", "1")
    ws = _ws_with_approved(tmp_path)
    res = pp.enforce("patch", {"path": "src/a.py"}, task_id="t1",
                     role="dev-worker-1", ws=str(ws))
    assert res and "allowed_paths" in res["message"]


def test_dev_worker_taskless_fallback_allows_src_blocks_design(tmp_path, monkeypatch):
    # 默认降级（#2）：缺 allowed_paths 时，写 workspace 内放行、写 design/ 拦
    monkeypatch.delenv("POLICY_REQUIRE_TASK_ID", raising=False)
    ws = _ws_with_approved(tmp_path)
    assert pp.enforce("patch", {"path": "src/a.py"}, task_id="t1",
                      role="dev-worker-1", ws=str(ws)) is None
    blk = pp.enforce("write_file", {"path": "design/approved_versions.txt"}, task_id="t1",
                     role="dev-worker-1", ws=str(ws))
    assert blk and "fallback" in blk["message"].lower()


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


# --- 问题C：allowed_paths 目录边界，前缀不应跨目录名误匹配 -------------------
def test_allowed_paths_directory_boundary(tmp_path):
    ws = _ws_with_approved(tmp_path)
    # 注意：故意不带尾斜杠，模拟 dev-lead 漏写 '/' 的情况
    (ws / "design" / "allowed_paths.t1.txt").write_text("src/crud\n")
    # src/crud_secret.py 不在 src/crud/ 下，必须拦截
    blocked = pp.enforce("write_file", {"path": "src/crud_secret.py"}, task_id="t1",
                         role="dev-worker-1", ws=str(ws))
    assert blocked and "allowed_paths" in blocked["message"]
    # src/crud/x.py 在目录内，放行
    assert pp.enforce("write_file", {"path": "src/crud/x.py"}, task_id="t1",
                      role="dev-worker-1", ws=str(ws)) is None
    # 精确文件条目也应放行该文件本身
    (ws / "design" / "allowed_paths.t2.txt").write_text("src/main.py\n")
    assert pp.enforce("write_file", {"path": "src/main.py"}, task_id="t2",
                      role="dev-worker-1", ws=str(ws)) is None


# --- 问题A：task_id 解析（kwargs / env 回退），缺失即 fail-closed ------------
def test_resolve_task_id_prefers_explicit_then_kwargs_then_env(monkeypatch):
    assert pp.resolve_task_id("t9", {"task_id": "tX"}) == "t9"
    assert pp.resolve_task_id(None, {"kanban_task_id": "tK"}) == "tK"
    for k in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_TASK_ID", "HERMES_TASK_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "tEnv")
    assert pp.resolve_task_id(None, {}) == "tEnv"
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert pp.resolve_task_id(None, {}) is None


def test_dev_worker_write_uses_task_id_from_kwargs(tmp_path):
    # hook 不经位置参数传 task_id，而是放在 kwargs（模拟 Hermes 真实可能形态）
    ws = _ws_with_approved(tmp_path)
    (ws / "design" / "allowed_paths.tK.txt").write_text("src/crud/\n")
    res = pp.enforce("write_file", {"path": "src/crud/x.py"},
                     role="dev-worker-1", ws=str(ws), kanban_task_id="tK")
    assert res is None


def test_dev_worker_strict_blocked_when_task_id_missing(tmp_path, monkeypatch):
    # 严格模式下拿不到 task_id → fail-closed 全锁
    monkeypatch.setenv("POLICY_REQUIRE_TASK_ID", "1")
    ws = _ws_with_approved(tmp_path)
    res = pp.enforce("write_file", {"path": "src/crud/x.py"},
                     role="dev-worker-1", ws=str(ws))
    assert res and "allowed_paths" in res["message"]


def test_qa_may_write_tests_not_business_code(tmp_path):
    (tmp_path / "tests").mkdir()
    assert pp.enforce("write_file", {"path": "tests/test_x.py"},
                      role="qa", ws=str(tmp_path)) is None
    blocked = pp.enforce("write_file", {"path": "src/main.py"},
                         role="qa", ws=str(tmp_path))
    assert blocked and "may only write" in blocked["message"]


def _qa_pass(ws):
    qa = ws / "reports" / "qa"
    qa.mkdir(parents=True, exist_ok=True)
    (qa / "status.json").write_text('{"release_allowed": true}')


def test_release_may_write_dist_not_code(tmp_path):
    _qa_pass(tmp_path)   # 先让 QA gate 放行
    assert pp.enforce("write_file", {"path": "dist/pkg.whl"},
                      role="release", ws=str(tmp_path)) is None
    blocked = pp.enforce("patch", {"path": "src/main.py"},
                         role="release", ws=str(tmp_path))
    assert blocked and "may only write" in blocked["message"]


def test_release_blocked_without_qa_status(tmp_path):
    # QA gate：无 reports/qa/status.json → release 任何执行/写入都拦
    for tool, args in [("terminal", {}), ("write_file", {"path": "dist/pkg.whl"})]:
        res = pp.enforce(tool, args, role="release", ws=str(tmp_path))
        assert res and "Release blocked" in res["message"]


def test_release_blocked_when_qa_not_allowed(tmp_path):
    qa = tmp_path / "reports" / "qa"
    qa.mkdir(parents=True)
    (qa / "status.json").write_text('{"release_allowed": false, "failed": 2}')
    res = pp.enforce("terminal", {}, role="release", ws=str(tmp_path))
    assert res and "Release blocked" in res["message"]


def test_release_terminal_allowed_with_qa_pass(tmp_path):
    _qa_pass(tmp_path)
    assert pp.enforce("terminal", {}, role="release", ws=str(tmp_path)) is None


def test_qa_terminal_still_allowed(tmp_path):
    # qa 需要 terminal 跑测试（terminal 不是 WRITE_TOOLS）
    assert pp.enforce("terminal", {}, role="qa", ws=str(tmp_path)) is None


def test_dev_worker_path_escape_blocked(tmp_path):
    ws = _ws_with_approved(tmp_path)
    (ws / "design" / "allowed_paths.t1.txt").write_text("src/\n")
    # ../ 逃逸到 workspace 外应被拦
    res = pp.enforce("write_file", {"path": "../../etc/passwd"}, task_id="t1",
                     role="dev-worker-1", ws=str(ws))
    assert res and res["action"] == "block"


def test_resolve_task_id_from_worktree_cwd(monkeypatch):
    # worktree 目录名即 task id（t_xxx），从 cwd 反推
    for k in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_TASK_ID", "HERMES_TASK_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TERMINAL_CWD", "/data/projects/demo1/workspace/.worktrees/t_f8700557")
    assert pp.resolve_task_id(None, {}) == "t_f8700557"


def test_per_task_scoping_works_via_worktree_taskid(tmp_path, monkeypatch):
    # task_id 经 worktree cwd 解析到 → 第三道闸真正按 allowed_paths 生效
    ws = _ws_with_approved(tmp_path)
    (ws / "design" / "allowed_paths.t_abcd1234.txt").write_text("src/crud/\n")
    monkeypatch.setenv("TERMINAL_CWD", str(ws / ".worktrees" / "t_abcd1234"))
    assert pp.enforce("write_file", {"path": "src/crud/x.py"},
                      role="dev-worker-1", ws=str(ws)) is None
    blk = pp.enforce("write_file", {"path": "src/other/y.py"},
                     role="dev-worker-1", ws=str(ws))
    assert blk and "allowed_paths" in blk["message"]


def test_taskless_fallback_is_logged(tmp_path, monkeypatch):
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    ws = _ws_with_approved(tmp_path)
    assert pp.enforce("write_file", {"path": "src/a.py"},
                      role="dev-worker-1", ws=str(ws)) is None
    log = ws / "reports" / "security" / "policy_fallback.jsonl"
    assert log.exists() and "missing_task_allowed_paths" in log.read_text()


def test_resolve_role_project_named_profiles(monkeypatch):
    # 问题B：项目 id 恰好叫 profiles，不能误取第一段
    for k in ("HERMES_PROFILE", "HERMES_PROFILE_NAME", "HERMES_AGENT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HERMES_HOME", "/data/projects/profiles/.hermes/profiles/ceo")
    assert pp.resolve_role(None, {}) == "ceo"


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
