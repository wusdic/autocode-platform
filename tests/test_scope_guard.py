"""提交级范围审计测试：真实 git worktree，覆盖 in/out-of-scope + 目录边界 + 内部文件忽略。"""
import subprocess

import scope_guard


def _git(d, *a):
    subprocess.run(["git", "-C", str(d), *a], check=True, capture_output=True, text=True)


def _project(tmp_path):
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "design").mkdir()
    (ws / ".worktrees").mkdir()
    _git(ws, "init", "-q", "-b", "main")
    _git(ws, "config", "user.email", "a@b")
    _git(ws, "config", "user.name", "a")
    (ws / "src" / "base.py").write_text("x = 1\n")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-q", "-m", "init")
    return ws


def _worktree(ws, slug, task_id, allowed):
    wt = ws / ".worktrees" / slug
    _git(ws, "worktree", "add", "-q", "-b", f"feat-{slug}", str(wt), "main")
    (wt / ".autocode_task_id").write_text(task_id)
    (ws / "design" / f"allowed_paths.{task_id}.txt").write_text("\n".join(allowed) + "\n")
    return wt


def test_no_worktrees_is_ok(tmp_path):
    ws = _project(tmp_path)
    assert scope_guard.scan(ws)["scope_ok"] is True


def test_in_scope_commit_passes(tmp_path):
    ws = _project(tmp_path)
    wt = _worktree(ws, "storage", "t_store01", ["src/store.py"])
    (wt / "src" / "store.py").write_text("y = 2\n")
    _git(wt, "add", "-A"); _git(wt, "commit", "-q", "-m", "feat: store")
    assert scope_guard.scan(ws)["scope_ok"] is True


def test_out_of_scope_commit_flagged(tmp_path):
    ws = _project(tmp_path)
    wt = _worktree(ws, "storage", "t_store01", ["src/store.py"])
    (wt / "src" / "evil.py").write_text("z = 3\n")
    _git(wt, "add", "-A"); _git(wt, "commit", "-q", "-m", "sneaky")
    res = scope_guard.scan(ws)
    assert res["scope_ok"] is False
    assert res["violations"][0]["files"] == ["src/evil.py"]


def test_directory_boundary(tmp_path):
    # allowed src/crud 不放行 src/crud_secret.py
    ws = _project(tmp_path)
    wt = _worktree(ws, "crud", "t_crud001", ["src/crud"])
    (wt / "src" / "crud_secret.py").write_text("secret\n")
    _git(wt, "add", "-A"); _git(wt, "commit", "-q", "-m", "x")
    assert scope_guard.scan(ws)["scope_ok"] is False


def test_missing_allowed_paths_is_violation(tmp_path):
    ws = _project(tmp_path)
    wt = ws / ".worktrees" / "nofile"
    _git(ws, "worktree", "add", "-q", "-b", "feat-nofile", str(wt), "main")
    (wt / ".autocode_task_id").write_text("t_nofile01")
    (wt / "src" / "a.py").write_text("a\n")
    _git(wt, "add", "-A"); _git(wt, "commit", "-q", "-m", "x")
    res = scope_guard.scan(ws)
    assert res["scope_ok"] is False
    assert res["violations"][0]["reason"] == "no allowed_paths file"


# --- 第十轮：主 workspace 观测（信息性，绝不阻断）------------------------------------
def test_main_workspace_findings_observed_not_blocking(tmp_path):
    # 主 ws 出现代码类提交 → 记入 main_workspace_findings，但 scope_ok 不受影响
    # （阻断版曾在真机撤销：direct-to-QA 基线提交/release 合并会全量误报）。
    ws = _project(tmp_path)   # _project 已 git init + init commit
    (ws / "src").mkdir(exist_ok=True)
    (ws / "src" / "sneaky.py").write_text("x=1\n")
    _git(ws, "add", "-A"); _git(ws, "commit", "-q", "-m", "code in main ws")
    res = scope_guard.scan(ws)
    assert res["scope_ok"] is True                       # 不阻断
    assert "src/sneaky.py" in res["main_workspace_findings"]   # 但可见


def test_main_workspace_findings_ignores_docs_and_reports(tmp_path):
    # design/reports/README 等合法产物不算 finding
    ws = _project(tmp_path)
    (ws / "design").mkdir(exist_ok=True); (ws / "design" / "PRD.md").write_text("p")
    (ws / "reports" / "qa").mkdir(parents=True); (ws / "reports" / "qa" / "s.json").write_text("{}")
    (ws / "README.md").write_text("r")
    _git(ws, "add", "-A"); _git(ws, "commit", "-q", "-m", "docs")
    assert scope_guard.scan(ws)["main_workspace_findings"] == []
