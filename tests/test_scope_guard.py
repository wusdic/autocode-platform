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
