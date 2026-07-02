#!/usr/bin/env python3
"""提交级范围审计 —— 堵住 dev-worker 用 terminal 绕过 allowed_paths 写文件的洞。

policy_plugin 的 pre_tool_call hook 只能拦 write_file/patch；但 dev-worker 拥有 terminal，
可以 `python -c "open('src/x','w')..."` / `sh -c 'echo > ...'` 直接写，hook 看不到。
所以 per-task 隔离不能只靠 hook，必须有一个**事后、基于 git diff 的硬闸**：每个任务 worktree
里实际变更的文件，必须全部落在该任务 design/allowed_paths.<task_id>.txt 声明的范围内。

由 qa_integrity 调用（写进 status.json.integrity.scope_violations），更关键的是由 orchestrator
在【宿主侧】起 release 前独立调用——不信任容器里可被改写的脚本。

判定口径：
  * 只审计 workspace/.worktrees/ 下的 worktree（无 worktree 的串行项目不受影响）。
  * 每个 worktree：解析 task_id（.autocode_task_id 标记 > 分支名 > 目录名里的 t_<id>），
    读 allowed_paths，对 `git diff`（相对 main 的 merge-base + 未提交改动）逐文件做目录边界匹配。
  * 有变更但找不到 allowed_paths / task_id → 记为 violation（无法证明在范围内）。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

_TASK_ID_RE = re.compile(r"^t_[A-Za-z0-9_-]{4,}$")

# 平台内部文件不算项目产物，不参与范围审计（否则 .autocode_task_id 标记自己会被当越界）。
_IGNORE_PREFIXES = (".autocode/", ".worktrees/")
_IGNORE_EXACT = {".autocode_task_id", ".gitignore"}


def _is_internal(path: str) -> bool:
    p = (path or "").replace("\\", "/")
    if p.startswith("./"):
        p = p[2:]
    return p in _IGNORE_EXACT or any(p.startswith(pre) for pre in _IGNORE_PREFIXES)


def _git(wt: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(wt), *args],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def _path_allowed(target: str, allow: list[str]) -> bool:
    """目录边界前缀匹配：src/crud 放行 src/crud/x 但不放行 src/crud_secret.py。"""
    t = (target or "").replace("\\", "/")
    for p in allow:
        p = p.replace("\\", "/").rstrip("/")
        if p and (t == p or t.startswith(p + "/")):
            return True
    return False


def _resolve_task_id(wt: Path) -> str | None:
    marker = wt / ".autocode_task_id"
    if marker.exists():
        try:
            v = marker.read_text(encoding="utf-8").strip()
            if _TASK_ID_RE.match(v):
                return v
        except OSError:
            pass
    # 分支名
    try:
        br = _git(wt, "rev-parse", "--abbrev-ref", "HEAD")
        for part in re.split(r"[/_-]", br):
            if _TASK_ID_RE.match(part):
                return part
    except (subprocess.CalledProcessError, OSError):
        pass
    # 目录名片段
    for part in re.split(r"[/_-]", wt.name):
        if _TASK_ID_RE.match(part):
            return part
    return None


def _base_ref(ws: Path) -> str:
    """主线分支名：取主 workspace 当前分支（init 默认可能是 main 或 master，不写死）。"""
    try:
        ref = _git(ws, "rev-parse", "--abbrev-ref", "HEAD")
        if ref and ref != "HEAD":
            return ref
    except (subprocess.CalledProcessError, OSError):
        pass
    return "main"


def _changed_files(wt: Path, base_ref: str) -> list[str]:
    """该 worktree 相对主线的全部变更文件（已提交 + 未提交），去重。"""
    files: set[str] = set()
    try:
        base = _git(wt, "merge-base", base_ref, "HEAD")
        for f in _git(wt, "diff", "--name-only", base, "HEAD").splitlines():
            if f.strip():
                files.add(f.strip())
    except (subprocess.CalledProcessError, OSError):
        pass
    try:  # 未提交（工作区 + 暂存）
        for f in _git(wt, "diff", "--name-only", "HEAD").splitlines():
            if f.strip():
                files.add(f.strip())
    except (subprocess.CalledProcessError, OSError):
        pass
    return sorted(f for f in files if not _is_internal(f))


# 主 workspace 里这些文件/目录是各角色的合法产物，不列入"主 ws 出现代码"观测。
_MAIN_WS_DOC_EXACT = {"README.md", "AGENTS.md", ".gitignore", "requirements.txt",
                      "pyproject.toml", "Makefile"}
_MAIN_WS_DOC_PREFIXES = ("design/", "reports/", "docs/", "dist/")


def main_workspace_findings(ws: Path) -> list[str]:
    """主 workspace 相对首个 commit 的代码类变更（含未提交），**仅观测、不阻断**。

    为什么不并入 violations（第十轮，来自第七轮 D16 的两难）：
      * 阻断版曾实现过并在真机撤销——direct-to-QA/baseline 流程的合法主 ws 提交、
        release 阶段把 worktree 分支合回主线，都会让主 ws diff 出现代码 → 全量误报，
        把合法项目堵死在 release 门（scope_guard 无法归因"这文件是谁写的"）。
      * 但完全不看主 ws 又留下 D16 盲区（角色绕过 worktree 直接写主 ws）。
    折中：作为**信息性发现**返回，由 qa_integrity 带进 status.json（不参与放行判定）、
    落 audit/warnings 供人与 monitor 复核——可见而不误杀。
    """
    ws = Path(ws)
    if not (ws / ".git").exists():
        return []
    files: set[str] = set()
    try:
        root = _git(ws, "rev-list", "--max-parents=0", "HEAD").splitlines()[0].strip()
        for f in _git(ws, "diff", "--name-only", root, "HEAD").splitlines():
            if f.strip():
                files.add(f.strip())
    except (subprocess.CalledProcessError, OSError, IndexError):
        pass
    try:  # 未提交（工作区 + 暂存）
        for f in _git(ws, "status", "--porcelain").splitlines():
            name = f[3:].strip() if len(f) > 3 else ""
            if name:
                files.add(name)
    except (subprocess.CalledProcessError, OSError):
        pass
    return sorted(
        f for f in files
        if not _is_internal(f)
        and f not in _MAIN_WS_DOC_EXACT
        and not any(f.startswith(p) for p in _MAIN_WS_DOC_PREFIXES)
    )


def scan(ws: Path) -> dict:
    """审计 workspace 下所有 worktree，返回 {scope_ok, violations, main_workspace_findings}。
    main_workspace_findings 仅观测（见 main_workspace_findings 的说明），不影响 scope_ok。"""
    ws = Path(ws)
    wt_root = ws / ".worktrees"
    violations: list[dict] = []
    findings = main_workspace_findings(ws)
    if not wt_root.exists():
        return {"scope_ok": True, "violations": [],
                "main_workspace_findings": findings}
    base_ref = _base_ref(ws)
    for wt in sorted(p for p in wt_root.iterdir() if p.is_dir()):
        changed = _changed_files(wt, base_ref)
        if not changed:
            continue
        tid = _resolve_task_id(wt)
        if not tid:
            violations.append({"worktree": wt.name, "task_id": None,
                               "reason": "cannot resolve task_id", "files": changed})
            continue
        ap_file = ws / "design" / f"allowed_paths.{tid}.txt"
        if not ap_file.exists():
            violations.append({"worktree": wt.name, "task_id": tid,
                               "reason": "no allowed_paths file", "files": changed})
            continue
        allow = [l.strip() for l in ap_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        out = [f for f in changed if not _path_allowed(f, allow)]
        if out:
            violations.append({"worktree": wt.name, "task_id": tid,
                               "reason": "files outside allowed_paths",
                               "allowed_paths": allow, "files": out})
    return {"scope_ok": not violations, "violations": violations,
            "main_workspace_findings": findings}


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    ws = Path(argv[0]) if argv else Path.cwd()
    print(json.dumps(scan(ws), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
