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

角色识别：据官方文档，**每个 profile 有独立的 HERMES_HOME**，形如
``<base>/.hermes/profiles/<name>``，运行某 profile 时 ``HERMES_HOME`` 会被设到该子目录，
故 ``Path(HERMES_HOME).name`` 通常 == profile 名（这是文档化的当前 profile 信号，
因为并不存在 ``HERMES_PROFILE`` 环境变量）。resolve_role 仍优先用 hook kwargs/环境变量，
再回退到 HERMES_HOME 末段——后者在标准布局下可靠。
⚠️ 唯一需在真实环境确认的：本平台用「每项目自定义 HERMES_HOME 基目录」时，
dispatcher spawn worker 是否仍把 HERMES_HOME 设为 ``<base>/.hermes/profiles/<role>``。
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

# 可执行/改码的具名角色（dev-worker-* 另按前缀判断）
EXECUTOR_ROLES = {"qa", "release"}
# QA 只能写测试/报告，release 只能写产物——不得改业务代码（设计 §3.2 职责边界）。
QA_WRITE_PREFIXES = ["tests", "test", "reports/qa", "coverage"]
RELEASE_WRITE_PREFIXES = ["dist", "release", "reports/release"]
# 敏感工具：角色识别不出时，对这些一律 fail-closed 拒绝
SENSITIVE_TOOLS = {"terminal", "patch", "write_file"}

DESIGN_DIR = "design"

# Hermes 可能用于传递当前 profile/角色的键，按可靠性从高到低尝试。
_ROLE_KW_KEYS = ("role", "profile", "profile_name", "agent", "agent_name")
_ROLE_ENV_KEYS = ("HERMES_PROFILE", "HERMES_PROFILE_NAME", "HERMES_AGENT")
# 当前 kanban 卡 id 可能的来源：hook kwargs 优先，其次环境变量（dispatcher 已知会
# 给子进程设 HERMES_KANBAN_BOARD，task id 也可能经类似 env 提供）。
_TASK_KW_KEYS = ("task_id", "task", "kanban_task_id", "card_id")
_TASK_ENV_KEYS = ("HERMES_KANBAN_TASK", "HERMES_KANBAN_TASK_ID", "HERMES_TASK_ID")


def resolve_role(explicit: str | None = None, kwargs: dict | None = None) -> str:
    """解析当前角色名。

    优先 hook 显式参数 / kwargs / 环境变量；再回退到 ``Path(HERMES_HOME).name``——
    据官方文档，每个 profile 有独立 HERMES_HOME（``…/profiles/<name>``），故该末段
    通常即 profile 名，是可靠回退。仍保留 fail-closed：若最终拿到的不是已知角色，
    enforce() 会拒绝敏感工具。
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
    # 回退：从 HERMES_HOME 解析。标准布局是 ``…/profiles/<name>``，所以优先取
    # ``profiles`` 段后的那一节（比纯 basename 更稳，能容忍尾部多一层目录）；
    # 找不到 profiles 段时退回末段（项目级 ``…/.hermes`` 会得到 ".hermes"，
    # 非已知角色 → enforce() fail-closed）。
    home = os.environ.get("HERMES_HOME", "")
    parts = Path(home).parts
    # 标准布局 <base>/.hermes/profiles/<role>：锚定到 ".hermes" 之后的 profiles 段，
    # 避免项目名恰好叫 "profiles" 时 index() 取到错误位置（问题B）。
    for i in range(len(parts) - 1):
        if parts[i] == "profiles" and i > 0 and parts[i - 1].endswith(".hermes"):
            return parts[i + 1]
    return Path(home).name or "unknown"


def workspace_dir() -> str:
    """worker 的工作目录：优先 TERMINAL_CWD，回退到当前目录。"""
    return os.environ.get("TERMINAL_CWD") or os.getcwd()


def resolve_task_id(explicit=None, kwargs=None):
    """解析当前 kanban 卡 id：hook 显式参数 → kwargs → 环境变量。

    ⚠️ 这是平台第二大不确定点（问题A）：若 Hermes 的 pre_tool_call 既不经参数也不经
    环境传 task id，则第三道闸（allowed_paths）拿不到 id，会 fail-closed。必须真机验证
    （见《03》Step 8-5 与验证矩阵 B 节）。这里多探几个可能的来源以尽量降低"全锁"风险。
    """
    if explicit:
        return explicit
    kwargs = kwargs or {}
    for k in _TASK_KW_KEYS:
        if kwargs.get(k):
            return str(kwargs[k])
    for env in _TASK_ENV_KEYS:
        if os.environ.get(env):
            return os.environ[env]
    return None


def _under_design(target: str) -> bool:
    t = (target or "").replace("\\", "/")
    return t == DESIGN_DIR or t.startswith(DESIGN_DIR + "/") or f"/{DESIGN_DIR}/" in t


def normalize_target(ws: str, target: str):
    """把目标路径规范化为相对 workspace 的 posix 路径；越界（绝对路径外/.. 逃逸/
    symlink 逃逸）返回 (None, 原因)。供路径白名单判断前统一收口。"""
    try:
        root = Path(ws).resolve()
        raw = Path(target or "")
        candidate = raw if raw.is_absolute() else root / raw
        rel = candidate.resolve().relative_to(root).as_posix()
        return rel, None
    except (OSError, ValueError):
        return None, f"target '{target}' is outside workspace"


def _path_allowed(target: str, allow: list[str]) -> bool:
    """带目录边界的前缀匹配（问题C）：``src/crud`` 不应放行 ``src/crud_secret.py``。

    每条 allow 视为"精确文件"或"目录前缀"：target 命中当且仅当 ``target == p``
    或 ``target`` 以 ``p`` + ``/`` 开头（p 去掉尾部斜杠后）。
    """
    t = (target or "").replace("\\", "/")
    for p in allow:
        p = p.replace("\\", "/").rstrip("/")
        if not p:
            continue
        if t == p or t.startswith(p + "/"):
            return True
    return False


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


def is_known_role(role: str) -> bool:
    return role in NO_CODE_ROLES or role in EXECUTOR_ROLES or role.startswith("dev-worker")


def enforce(tool_name, args, task_id=None, role=None, ws=None, **kwargs):
    """pre_tool_call hook 主体。

    返回 None 放行；返回 ``{"action": "block", "message": ...}`` 拦截。
    role / ws 参数仅用于测试注入；正常运行时从 kwargs/环境推断。

    设计为 **fail-closed**：角色识别不出时拒绝一切敏感工具，避免因
    Hermes 未按预期暴露 profile 而默默放行（防 fail-open）。
    """
    role = resolve_role(role, kwargs)
    args = args or {}
    target = args.get("path", "")

    # 失败即关闭：识别不出角色 → 敏感工具一律拒绝
    if not is_known_role(role) and tool_name in SENSITIVE_TOOLS:
        return _block(
            f"Cannot determine caller role (got '{role}'); refusing '{tool_name}'. "
            "Ensure Hermes passes the profile to the hook (see resolve_role)."
        )

    # 闸门 1：no-code 角色
    if role in NO_CODE_ROLES:
        if tool_name in CODE_TOOLS:
            return _block(
                f"Role '{role}' is not allowed to call '{tool_name}'. "
                "Create a kanban task for an executor role instead."
            )
        # write_file 仅允许写 design/（设计文档，含 approved_versions.txt）。
        # 先规范化路径（堵 design/../ 越界），再判断是否在 design/ 内。
        if tool_name == "write_file" and target:
            rel, err = normalize_target(ws or workspace_dir(), target)
            if err or not _under_design(rel or ""):
                return _block(
                    f"Role '{role}' may only write under 'design/'. "
                    f"'{target}' is outside design/; route code to a dev-worker task."
                )
        return None

    # QA / release：可执行（terminal 跑测试/构建），但写文件范围受限，不得改业务代码。
    if role in EXECUTOR_ROLES and tool_name in WRITE_TOOLS:
        ws = ws or workspace_dir()
        rel, err = normalize_target(ws, target)
        prefixes = QA_WRITE_PREFIXES if role == "qa" else RELEASE_WRITE_PREFIXES
        if err or (rel and not _path_allowed(rel, prefixes)):
            return _block(
                f"Role '{role}' may only write under {prefixes}; "
                "business code changes must go to a dev-worker task."
            )
        return None

    # 闸门 2 & 3：工程师
    if role.startswith("dev-worker"):
        ws = ws or workspace_dir()
        approved = approved_designs(ws)

        # 无批准设计：不准改码，也不准用 terminal 执行（terminal 同样能写文件，
        # 否则可绕过设计闸门）。
        if tool_name in (WRITE_TOOLS | {"terminal"}) and not approved:
            return _block(
                "No approved design_version found. "
                "Code changes (incl. terminal) require an approved design first."
            )

        # 改文件：必须有显式 allowed_paths（绑定合法 task id）——fail-closed。
        if tool_name in WRITE_TOOLS:
            tid = resolve_task_id(task_id, kwargs)
            allow = allowed_paths(ws, tid)
            if allow is None:
                return _block(
                    f"No 'design/allowed_paths.{tid}.txt' for this task (task_id={tid}). "
                    "Declare the file scope before writing code."
                )
            # 先规范化（堵绝对路径越界 / .. / symlink 逃逸），再做白名单匹配。
            rel, err = normalize_target(ws, target)
            if err:
                return _block(err)
            if rel and not _path_allowed(rel, allow):
                return _block(
                    f"File '{target}' is outside this task's allowed_paths. "
                    "Do not modify files beyond your task scope."
                )

    return None


def register(ctx):
    """Hermes 插件入口：注册 pre_tool_call hook。"""
    ctx.register_hook("pre_tool_call", enforce)
