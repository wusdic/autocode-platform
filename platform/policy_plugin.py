"""角色权限 + 设计闸门：pre_tool_call 硬拦（第二层权限）。

对应《01-最终设计方案.md》第 3.1、6.2 节，《02-从零开始操作手册.md》阶段 3。

第一层权限是各 profile 的 toolset 裁剪（角色物理上没有越权工具）；
本插件是第二层兜底：即使 toolset 漏配，也在工具调用前 block。

三道闸：
  1. 非执行角色禁止调用执行类工具（terminal / patch / write_file）。
  2. 工程师改代码前必须存在 approved design_version。
  3. 工程师只能改本 task 的 allowed_paths 内的文件。

设计批准流程：change-guardian / arch-synthesizer 完成设计后，把版本号追加进
``workspace/design/approved_versions.txt``，并为每个编码任务写
``allowed_paths.<task_id>.txt``。这两个文件就是设计闸门的"钥匙"。
"""
from __future__ import annotations

import os
from pathlib import Path

# 哪些角色禁止执行类工具（兜底，即使 toolset 漏配）
NO_EXEC_ROLES = {
    "ceo", "pm-lead", "pm-critic", "arch-lead", "arch-critic",
    "change-guardian", "dev-lead",
    "pm-research-a", "pm-research-b", "pm-synthesizer",
    "arch-simple", "arch-scale", "arch-security", "arch-synthesizer",
}
EXEC_TOOLS = {"terminal", "patch", "write_file"}
WRITE_TOOLS = {"patch", "write_file"}


def current_role() -> str:
    """profile 名 = HERMES_HOME 末段目录名。"""
    return Path(os.environ.get("HERMES_HOME", "")).name or "unknown"


def workspace_dir() -> str:
    """worker 的工作目录：优先 TERMINAL_CWD，回退到当前目录。"""
    return os.environ.get("TERMINAL_CWD") or os.getcwd()


def approved_designs(ws: str) -> set[str]:
    f = Path(ws) / "design" / "approved_versions.txt"
    if f.exists():
        return {line.strip() for line in f.read_text().splitlines() if line.strip()}
    return set()


def allowed_paths(ws: str, task_id: str | None) -> list[str] | None:
    """返回该 task 的 allowed_paths 列表；文件不存在返回 None（表示未约束）。"""
    if not task_id:
        return None
    f = Path(ws) / "design" / f"allowed_paths.{task_id}.txt"
    if not f.exists():
        return None
    return [line.strip() for line in f.read_text().splitlines() if line.strip()]


def enforce(tool_name, args, task_id=None, role=None, ws=None, **kwargs):
    """pre_tool_call hook 主体。

    返回 None 放行；返回 ``{"action": "block", "message": ...}`` 拦截。
    role / ws 参数仅用于测试注入，正常运行时从环境推断。
    """
    role = role or current_role()
    args = args or {}

    # 闸门 1：非执行角色不准碰执行类工具
    if role in NO_EXEC_ROLES and tool_name in EXEC_TOOLS:
        return {
            "action": "block",
            "message": (
                f"Role '{role}' is not allowed to call '{tool_name}'. "
                "Create a kanban task for an executor role instead."
            ),
        }

    # 闸门 2 & 3：工程师改代码前，校验 design_version 与 allowed_paths
    if role.startswith("dev-worker") and tool_name in WRITE_TOOLS:
        ws = ws or workspace_dir()

        # 必须有已批准的设计版本
        if not approved_designs(ws):
            return {
                "action": "block",
                "message": (
                    "No approved design_version found. "
                    "Code changes require an approved design first."
                ),
            }

        # 目标文件必须在本 task 的 allowed_paths 内
        target = args.get("path", "")
        allow = allowed_paths(ws, task_id)
        if allow is not None and target and not any(target.startswith(p) for p in allow):
            return {
                "action": "block",
                "message": (
                    f"File '{target}' is outside this task's allowed_paths. "
                    "Do not modify files beyond your task scope."
                ),
            }

    return None


def register(ctx):
    """Hermes 插件入口：注册 pre_tool_call hook。"""
    ctx.register_hook("pre_tool_call", enforce)
