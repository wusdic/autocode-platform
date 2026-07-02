#!/usr/bin/env python3
"""交付完整性校验 —— 防"看板 done 但代码没落地"（第四轮真机：DEV-4 声称测试通过，
但 store.py 仍是 TODO、test_security.py 不存在、git 历史只有 init+DEV-1）。

设计目的：让"Kanban done == 产物真的在 workspace 里"成为可机器校验的硬事实，而不是
信任 agent 的口头汇报。两类使用方：

  1. **QA 角色**（在沙箱内）跑 ``python qa_integrity.py <workspace>`` 生成 integrity 块，
     合并进 ``reports/qa/status.json``，供 release 与 policy 插件校验（QA 侧自证）。
  2. **orchestrator**（宿主侧，不信任 agent）在起 release 前独立调用 ``min_release_ok()``：
     只用稳健信号（init 之外是否有提交、是否有源码/测试文件）做硬闸，agent 谎报也拦得住。

稳健优先：硬闸只用"提交数 / 文件存在"等不易误判的信号；TODO 占位扫描作为**提示信息**
随 integrity 块输出（交给 QA 判断），不直接作为 orchestrator 的硬拦条件，避免误杀正常 TODO。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

try:
    import scope_guard
except ImportError:  # 容器内副本与 qa_integrity 同目录，正常可导入；缺失则范围审计降级为空
    scope_guard = None

# 提示性标记：出现说明"声称实现但留了占位"，QA 应据此判 release_allowed=false。
# 仅作 integrity 块里的 informational 字段，不作 orchestrator 硬拦（避免误伤正常 TODO）。
DEFAULT_TODO_MARKERS = ("checks go here", "TODO(DEV-", "implement me", "FIXME(DEV-")


def has_git(ws: Path) -> bool:
    return (ws / ".git").exists()


def _git(ws: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(ws), *args],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def commit_count_all(ws: Path) -> int:
    """所有分支（含 worktree 分支）的提交数。init 之外有提交 → >1。"""
    if not has_git(ws):
        return 0
    try:
        return int(_git(ws, "rev-list", "--all", "--count") or "0")
    except (subprocess.CalledProcessError, ValueError, OSError):
        return 0


def git_clean(ws: Path) -> bool:
    if not has_git(ws):
        return True
    try:
        return _git(ws, "status", "--short") == ""
    except (subprocess.CalledProcessError, OSError):
        return True


def worktrees_present(ws: Path) -> bool:
    wt = ws / ".worktrees"
    return wt.exists() and any(p.is_dir() for p in wt.iterdir())


def _iter_src(ws: Path):
    # 注意 **不排除 .worktrees**：worktree 并行流里，dev 产物在 release 合并前都还在
    # 各 feature 分支的 worktree 工作树里（main 工作树可能为空）。若排除 .worktrees，
    # expected_files_present 在"起 release"时刻会误判为空 → 阻断 release → 整个并行流死锁。
    # 同理 TODO 占位扫描也该看 worktree 里的待交付产物（DEV-4 store.py 留 TODO 的场景）。
    skip = {".git", ".autocode", "design", "reports"}
    for p in ws.rglob("*.py"):
        if not any(part in skip for part in p.relative_to(ws).parts):
            yield p


def scan_todo_markers(ws: Path, markers=DEFAULT_TODO_MARKERS) -> list[str]:
    hits = []
    for p in _iter_src(ws):
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if any(m in text for m in markers):
            hits.append(str(p.relative_to(ws)))
    return hits


def expected_files_present(ws: Path) -> bool:
    """至少要有一个业务源码文件落地（空交付不算完成）。"""
    return next(_iter_src(ws), None) is not None


def scope_violations(ws: Path) -> list:
    """提交级范围审计违规（terminal 绕过 allowed_paths 写文件 → 在此被 git diff 抓出）。"""
    if scope_guard is None:
        return []
    try:
        return scope_guard.scan(ws).get("violations", [])
    except Exception:
        return []


def main_workspace_findings(ws: Path) -> list:
    """主 workspace 代码类变更观测（**信息性，不参与放行判定**——见 scope_guard 同名函数
    的两难说明：阻断版会误杀 direct-to-QA/release 合并等合法主 ws 提交，观测版保留 D16 可见性）。"""
    if scope_guard is None:
        return []
    try:
        return scope_guard.main_workspace_findings(ws)
    except Exception:
        return []


def compute(ws: Path) -> dict:
    """生成 integrity 块，供 QA 合并进 reports/qa/status.json。"""
    return {
        "git_clean": git_clean(ws),
        "git_commit_count": commit_count_all(ws),
        "worktrees_present": worktrees_present(ws),
        "expected_files_present": expected_files_present(ws),
        "todo_markers": scan_todo_markers(ws),
        "scope_violations": scope_violations(ws),
        # 信息性字段：integrity_block_ok/policy 均不检查它（观测不阻断）
        "main_workspace_findings": main_workspace_findings(ws),
    }


def integrity_block_ok(integrity: dict) -> bool:
    """status.json 里 integrity 块是否全部通过（QA 自证侧）。"""
    if not integrity:
        return True  # 未提供则不在此处拦（orchestrator 另有独立硬闸）
    return bool(
        integrity.get("git_clean", True) is True
        and integrity.get("expected_files_present", True) is True
        and not integrity.get("todo_markers")
        and not integrity.get("scope_violations")
    )


def min_release_ok(ws: Path, dev_done: bool, status: dict | None = None) -> tuple[bool, str]:
    """orchestrator 起 release 前的独立硬闸（不信任 agent 汇报）。

    仅在本轮确有 dev 工作（dev_done）且 workspace 是 git 仓库时校验稳健信号：
      * init 之外必须有提交（否则就是"看板 done 但没产物"）；
      * 必须有业务源码文件落地。
    另叠加 status.json 的 integrity 块（若 QA 提供）。返回 (ok, 原因)。
    """
    status = status or {}
    if not integrity_block_ok(status.get("integrity") or {}):
        return False, "reports/qa/status.json 的 integrity 块未全部通过（git 脏/缺文件/留占位/越界）"
    # 宿主侧独立范围审计（不信任容器内可被改写的脚本输出）：terminal 绕过 allowed_paths 在此被拦。
    sv = scope_violations(ws)
    if sv:
        return False, f"提交级范围审计失败：{len(sv)} 个 worktree 改了 allowed_paths 外的文件"
    if not dev_done or not has_git(ws):
        return True, "skip（无 dev 工作或非 git 仓库，不评估产物落地）"
    if commit_count_all(ws) <= 1:
        return False, "dev 卡 done 但 git 仅 init 提交——产物未提交/worktree 未生效"
    if not expected_files_present(ws):
        return False, "无任何业务源码文件落地（疑似 agent 谎报完成）"
    return True, "ok"


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    ws = Path(argv[0]) if argv else Path.cwd()
    print(json.dumps(compute(ws), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
