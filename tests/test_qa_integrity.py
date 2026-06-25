"""交付完整性模块测试：用真实临时 git 仓库覆盖 min_release_ok 的稳健硬闸。"""
import subprocess

import qa_integrity as q


def _git(ws, *a):
    subprocess.run(["git", "-C", str(ws), *a], check=True,
                   capture_output=True, text=True)


def _init_repo(ws):
    (ws / "design").mkdir(parents=True, exist_ok=True)
    _git(ws, "init", "-q", "-b", "main")
    _git(ws, "config", "user.email", "a@b")
    _git(ws, "config", "user.name", "a")
    _git(ws, "commit", "-q", "--allow-empty", "-m", "init workspace")


def test_no_git_skips_gate(tmp_path):
    ok, _ = q.min_release_ok(tmp_path, dev_done=True, status={})
    assert ok is True


def test_dev_done_but_only_init_commit_blocks(tmp_path):
    _init_repo(tmp_path)
    ok, reason = q.min_release_ok(tmp_path, dev_done=True, status={})
    assert ok is False and "init" in reason


def test_dev_done_with_committed_src_passes(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "feat: a")
    ok, _ = q.min_release_ok(tmp_path, dev_done=True, status={})
    assert ok is True


def test_no_dev_work_skips(tmp_path):
    _init_repo(tmp_path)
    ok, _ = q.min_release_ok(tmp_path, dev_done=False, status={})
    assert ok is True


def test_integrity_block_in_status_fails_gate(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "feat")
    bad = {"integrity": {"git_clean": False, "expected_files_present": True, "todo_markers": []}}
    ok, _ = q.min_release_ok(tmp_path, dev_done=True, status=bad)
    assert ok is False


def test_todo_markers_block_integrity():
    assert q.integrity_block_ok({"todo_markers": ["src/store.py"]}) is False
    assert q.integrity_block_ok({"git_clean": True, "expected_files_present": True,
                                 "todo_markers": []}) is True
    assert q.integrity_block_ok({}) is True  # 未提供不在此拦


def test_release_ok_when_source_only_in_worktree(tmp_path):
    # worktree 并行流：产物在 feature 分支的 worktree 里、main 工作树为空（release 才合并）。
    # 起 release 时刻完整性闸门不得因 main 没文件而误拦（否则并行流死锁）。
    _init_repo(tmp_path)  # main 仅 init 提交，无 src
    (tmp_path / ".worktrees").mkdir(exist_ok=True)
    wt = tmp_path / ".worktrees" / "impl"
    _git(tmp_path, "worktree", "add", "-q", "-b", "feat-impl", str(wt), "main")
    (wt / ".autocode_task_id").write_text("t_impl0001")
    (tmp_path / "design" / "allowed_paths.t_impl0001.txt").write_text("src/\n")
    (wt / "src").mkdir()
    (wt / "src" / "main.py").write_text("print(1)\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "feat: impl")
    ok, reason = q.min_release_ok(tmp_path, dev_done=True, status={"release_allowed": True})
    assert ok is True, reason


def test_scope_violation_blocks_release(tmp_path):
    # 在 worktree 里提交 allowed_paths 外的文件 → min_release_ok 应拦（terminal 绕过场景）
    _init_repo(tmp_path)
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "a.py").write_text("x = 1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "feat")
    (tmp_path / "design").mkdir(exist_ok=True)
    (tmp_path / ".worktrees").mkdir(exist_ok=True)
    wt = tmp_path / ".worktrees" / "task1"
    _git(tmp_path, "worktree", "add", "-q", "-b", "feat-task1", str(wt), "main")
    (wt / ".autocode_task_id").write_text("t_task0001")
    (tmp_path / "design" / "allowed_paths.t_task0001.txt").write_text("src/store.py\n")
    (wt / "src" / "evil.py").write_text("z = 3\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "out of scope")
    ok, reason = q.min_release_ok(tmp_path, dev_done=True, status={})
    assert ok is False and "范围审计" in reason


def test_compute_reports_signals(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("# TODO(DEV-4): checks go here\n")
    out = q.compute(tmp_path)
    assert out["git_commit_count"] >= 1
    assert out["expected_files_present"] is True
    assert "src/a.py" in out["todo_markers"]
