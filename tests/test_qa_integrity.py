"""交付完整性模块测试：用真实临时 git 仓库覆盖 min_release_ok 的稳健硬闸。"""
import subprocess

import qa_integrity as q


def _git(ws, *a):
    subprocess.run(["git", "-C", str(ws), *a], check=True,
                   capture_output=True, text=True)


def _init_repo(ws):
    (ws / "design").mkdir(parents=True, exist_ok=True)
    _git(ws, "init", "-q")
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


def test_compute_reports_signals(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("# TODO(DEV-4): checks go here\n")
    out = q.compute(tmp_path)
    assert out["git_commit_count"] >= 1
    assert out["expected_files_present"] is True
    assert "src/a.py" in out["todo_markers"]
