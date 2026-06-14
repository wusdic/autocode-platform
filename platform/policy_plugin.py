"""角色权限 + 设计闸门：pre_tool_call 硬拦（第二层权限）。

对应《01-最终设计方案.md》第 3.1、6.2 节，《02-从零开始操作手册.md》阶段 3。

第一层权限是各 profile 的 toolset 裁剪（角色物理上没有越权工具）；
本插件是第二层兜底：即使 toolset 漏配，也在工具调用前 block。

三道闸：
  1. no-code 角色禁止 *写代码/执行*（terminal / patch）；其 write_file 仅允许写
     design/ 目录（设计文档），不得写代码。
  2. 工程师改代码前必须存在 approved design_version。
  3. 工程师只能改本 task 的 allowed_paths 内的文件。

第 1 道闸特意区分「执行/改码」与「写设计文档」：synthesizer / change-guardian
等角色需要写 design/（含 approved_versions.txt 这把"开闸钥匙"），若把 write_file
一刀切禁掉会造成设计闸门死锁——synthesizer 写不了 approved_versions.txt，
dev-worker 永远无法开工。

角色识别（重要）：Hermes 的 per-project HERMES_HOME 形如
``/data/projects/{id}/.hermes``，其末段恒为 ``.hermes`` 而非角色名，**不能**用它当
角色。正确来源优先级：hook 调用传入的显式 role/profile → kwargs → 环境变量
``HERMES_PROFILE`` 等 → 最后才退回 HERMES_HOME 末段（仅占位）。
具体哪个键由 Hermes 提供，请在阶段 2 实测确认（见 resolve_role）。
"""
from __future__ import annotations

import os
from pathlib import Path

# 不允许执行/改码的角色（兜底，即使 toolset 漏配）
NO_CODE_ROLES = {
    "ceo", "pm-lead", "pm-critic", "arch-lead", "arch-critic",
    "change-guardian", "dev-lead",
    "pm-research-a", "pm-research-b", "pm-synthesizer",
    "arch-simple", "arch-scale", "arch-security", "arch-synthesizer",
}
# 兼容旧名
NO_EXEC_ROLES = NO_CODE_ROLES

# 直接执行/改码的工具：no-code 角色一律禁止
CODE_TOOLS = {"terminal", "patch"}
# 写文件工具：no-code 角色仅允许写 design/；dev-worker 需过设计闸门
WRITE_TOOLS = {"patch", "write_file"}
# 兼容旧名（旧测试/文档引用）
EXEC_TOOLS = {"terminal", "patch", "write_file"}

DESIGN_DIR = "design"

# Hermes 可能用于传递当前 profile/角色的键，按可靠性从高到低尝试。
_ROLE_KW_KEYS = ("role", "profile", "profile_name", "agent", "agent_name")
_ROLE_ENV_KEYS = ("HERMES_PROFILE", "HERMES_PROFILE_NAME", "HERMES_AGENT")


def resolve_role(explicit: str | None = None, kwargs: dict | None = None) -> str:
    """解析当前角色名。

    注意：绝不能只靠 ``Path(HERMES_HOME).name``——它恒为 ``.hermes``。
    """
    if explicit:
        return explicit
    kwargs = kwargs or {}
    for k in _ROLE_KW_KEYS:
        v = kwargs.get(k)
        if v:
            return str(v)
    for env in _ROLE_ENV_KEYS:
        v = os.environ.get(env)
        if v:
            return v
    # 兜底：HERMES_HOME 末段（通常是 ".hermes"，并非角色名，仅占位以免崩溃）
    return Path(os.environ.get("HERMES_HOME", "")).name or "unknown"


def workspace_dir() -> str:
    """worker 的工作目录：优先 TERMINAL_CWD，回退到当前目录。"""
    return os.environ.get("TERMINAL_CWD") or os.getcwd()


def _under_design(target: str) -> bool:
    t = (target or "").replace("\\", "/")
    return t == DESIGN_DIR or t.startswith(DESIGN_DIR + "/") or f"/{DESIGN_DIR}/" in t


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


def _block(message: str) -> dict:
    return {"action": "block", "message": message}


def enforce(tool_name, args, task_id=None, role=None, ws=None, **kwargs):
    """pre_tool_call hook 主体。

    返回 None 放行；返回 ``{"action": "block", "message": ...}`` 拦截。
    role / ws 参数仅用于测试注入；正常运行时从 kwargs/环境推断。
    """
    role = resolve_role(role, kwargs)
    args = args or {}
    target = args.get("path", "")

    # 闸门 1：no-code 角色
    if role in NO_CODE_ROLES:
        if tool_name in CODE_TOOLS:
            return _block(
                f"Role '{role}' is not allowed to call '{tool_name}'. "
                "Create a kanban task for an executor role instead."
            )
        # write_file 仅允许写 design/（设计文档，含 approved_versions.txt）
        if tool_name == "write_file" and target and not _under_design(target):
            return _block(
                f"Role '{role}' may only write under 'design/'. "
                f"'{target}' looks like code; route it to a dev-worker task."
            )
        return None

    # 闸门 2 & 3：工程师改代码前，校验 design_version 与 allowed_paths
    if role.startswith("dev-worker") and tool_name in WRITE_TOOLS:
        ws = ws or workspace_dir()

        if not approved_designs(ws):
            return _block(
                "No approved design_version found. "
                "Code changes require an approved design first."
            )

        allow = allowed_paths(ws, task_id)
        if allow is not None and target and not any(target.startswith(p) for p in allow):
            return _block(
                f"File '{target}' is outside this task's allowed_paths. "
                "Do not modify files beyond your task scope."
            )

    return None


def register(ctx):
    """Hermes 插件入口：注册 pre_tool_call hook。"""
    ctx.register_hook("pre_tool_call", enforce)
