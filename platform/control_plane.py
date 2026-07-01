"""FastAPI 控制平面 —— 对外唯一入口。

对应《01-最终设计方案.md》第 8 节、《02-从零开始操作手册.md》阶段 4。

安全红线：Hermes 的 /v1/* 与 dashboard 绝不直接对外；对外鉴权全在本网关。
本网关默认只绑 127.0.0.1；要让局域网访问 CEO 交互页，设 ``PLATFORM_BIND_HOST=0.0.0.0``
（仅 X-Token 鉴权、无 TLS，仅限可信局域网；非本机地址 + 默认 token 会被拒绝启动）。
跨不可信网络/公网须前置 nginx/Caddy 做 TLS。注意：各项目的 Hermes ``/v1`` 网关始终只绑本机。

相对手册原稿的改进：
  * 修复 ``PROJECTS.get(pid) or HTTPException(404)`` 不抛异常的 bug，统一用
    ``get_project`` 在缺失时 raise 404。
  * 把对 Hermes 的进程/HTTP 交互收敛到可注入的 ``HermesGateway``，便于单测。
  * 补齐设计方案第 8 节列出但手册骨架缺失的端点：events / confirm-plan /
    change-requests / artifacts / demo。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import StreamingResponse, HTMLResponse
from pydantic import BaseModel

try:
    import fcntl  # POSIX 文件锁；非 POSIX 平台降级为无锁
except ImportError:  # pragma: no cover
    fcntl = None

# 严格 slug：防止 project_id 路径穿越 / systemd 单元名污染。
PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,63}$")
# session_id 会进文件名（conversations/<sid>.jsonl）与 X-Hermes-Session-Key 头——必须收口，
# 否则 "../../x" 可路径穿越写/读 workspace 外文件，含换行可注入 header。
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def validate_project_id(pid: str) -> None:
    if not PROJECT_ID_RE.match(pid or ""):
        raise HTTPException(
            400,
            "invalid project_id; must match ^[a-z0-9][a-z0-9_-]{2,63}$",
        )


def validate_session_id(sid: str) -> None:
    if not SESSION_ID_RE.match(sid or ""):
        raise HTTPException(400, "invalid session_id; must match ^[A-Za-z0-9_-]{1,64}$")


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def _default_token() -> str:
    """PLATFORM_TOKEN：环境变量 → ~/platform/.platform_token 文件 → change-me。

    Hermes 的密钥脱敏会阻止把 token 写进脚本文件传递（NEW-H），故支持文件 fallback；
    生产用 systemd 的 Environment= 固定，避免重启后回落 change-me 致 401（26.3）。
    """
    env = os.environ.get("PLATFORM_TOKEN")
    if env:
        return env
    p = os.path.expanduser("~/platform/.platform_token")
    if os.path.exists(p):
        return open(p).read().strip()
    return "change-me"


@dataclass
class Settings:
    token: str = field(default_factory=_default_token)
    base_port: int = field(default_factory=lambda: int(os.environ.get("PLATFORM_BASE_PORT", "8650")))
    data_root: str = field(default_factory=lambda: os.environ.get("PLATFORM_DATA_ROOT", "/data/projects"))
    launcher: str = field(
        default_factory=lambda: os.environ.get(
            "PLATFORM_LAUNCHER", os.path.expanduser("~/platform/launch_project.sh")
        )
    )


# --------------------------------------------------------------------------- #
# 端口分配（落盘，控制平面重启后不重复分配、不撞已在跑的项目端口）
# --------------------------------------------------------------------------- #
class PortAllocator:
    """把 project_id → port 的分配持久化到 ``{data_root}/.ports.json``。

    控制平面重启后 ``next`` 从磁盘恢复，避免内存计数器归零导致端口与已在
    运行的 Hermes gateway 冲突（对应手册阶段 13 端口注册表持久化的诉求）。
    """

    def __init__(self, data_root: str, base_port: int) -> None:
        self._path = Path(data_root) / ".ports.json"
        self._base = base_port
        self._state = self._load()

    def _load(self) -> dict:
        if self._path.exists():
            try:
                return json.loads(self._path.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return {"next": self._base, "assigned": {}}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._state, indent=2))

    def allocate(self, pid: str) -> int:
        """幂等分配：同一 pid 永远拿到同一端口。

        每次在文件锁下重新读取最新状态再写回，保证并发建项目时不撞端口。
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self._path.with_suffix(".lock")
        with open(lock_path, "w") as lockf:
            if fcntl is not None:
                fcntl.flock(lockf, fcntl.LOCK_EX)
            try:
                state = self._load()
                assigned = state["assigned"]
                if pid in assigned:
                    return assigned[pid]
                port = int(state["next"])
                assigned[pid] = port
                state["next"] = port + 1
                self._path.write_text(json.dumps(state, indent=2))
                self._state = state
                return port
            finally:
                if fcntl is not None:
                    fcntl.flock(lockf, fcntl.LOCK_UN)

    def release(self, pid: str) -> None:
        """回收某 pid 的端口分配（建项目失败时回滚，避免端口泄漏/孤儿占用，D16/D18）。

        只删 ``assigned`` 条目，不回退 ``next``（保持单调递增，避免与并发分配竞争撞端口）。
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self._path.with_suffix(".lock")
        with open(lock_path, "w") as lockf:
            if fcntl is not None:
                fcntl.flock(lockf, fcntl.LOCK_EX)
            try:
                state = self._load()
                if state["assigned"].pop(pid, None) is not None:
                    self._path.write_text(json.dumps(state, indent=2))
                    self._state = state
            finally:
                if fcntl is not None:
                    fcntl.flock(lockf, fcntl.LOCK_UN)


# --------------------------------------------------------------------------- #
# 项目注册表（内存实现；生产替换为 Postgres）
# --------------------------------------------------------------------------- #
@dataclass
class Project:
    project_id: str
    port: int
    key: str
    home: str
    workspace: str


class ProjectRegistry:
    def __init__(self) -> None:
        self._projects: dict[str, Project] = {}

    def add(self, project: Project) -> None:
        self._projects[project.project_id] = project

    def get(self, pid: str) -> Optional[Project]:
        return self._projects.get(pid)

    def __contains__(self, pid: str) -> bool:
        return pid in self._projects

    def all(self) -> list["Project"]:
        """公开迭代接口——端点不要直接碰私有 _projects（换 Postgres 后端时 _projects 不存在）。"""
        return sorted(self._projects.values(), key=lambda p: p.project_id)


def rehydrate(registry: "ProjectRegistry", settings: Settings) -> None:
    """控制平面启动时，从磁盘扫描已存在的项目重建注册表。

    读取每个项目 ``ceo/.env`` 里的 API_SERVER_PORT / API_SERVER_KEY，使重启后
    GET /tasks、/messages 等端点对老项目仍可用，而不必重新创建。
    """
    root = Path(settings.data_root)
    if not root.exists():
        return
    for proj_dir in sorted(root.iterdir()):
        env = proj_dir / ".hermes" / "profiles" / "ceo" / ".env"
        if not (proj_dir.is_dir() and env.exists()):
            continue
        port, key = None, ""
        for line in env.read_text().splitlines():
            if line.startswith("API_SERVER_PORT="):
                try:
                    port = int(line.split("=", 1)[1].strip())
                except ValueError:
                    pass
            elif line.startswith("API_SERVER_KEY="):
                key = line.split("=", 1)[1].strip()
        if port is None:
            continue
        registry.add(Project(
            project_id=proj_dir.name, port=port, key=key,
            home=str(proj_dir / ".hermes"),
            workspace=str(proj_dir / "workspace"),
        ))


# --------------------------------------------------------------------------- #
# 与 Hermes 实例交互（可注入，便于单测）
# --------------------------------------------------------------------------- #
class HermesGateway:
    """封装对 per-project Hermes 实例的进程管理与 HTTP 转发。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def launch(self, pid: str, port: int) -> Project:
        """运行 launch_project.sh，建实例 + board + profiles + gateway。

        **捕获输出并在失败时带出真实原因**：脚本 ``set -euo pipefail`` 下任何
        hermes/docker 命令非 0 都会中止，若不捕获，控制平面只能报 "exit status 1"，
        运维无法诊断（缺 key / docker 不可用 / 沙箱镜像缺失 / 模型预检失败 等都被吞）。
        失败时取 stdout+stderr 末尾若干行抛出，由 create_project 透到 502。
        """
        proc = subprocess.run(
            ["bash", self.settings.launcher, pid, str(port)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            lines = [l for l in ((proc.stdout or "") + (proc.stderr or "")).splitlines()
                     if l.strip()]
            tail = "\n".join(lines[-15:])[-1500:] or "(launcher 无输出)"
            raise RuntimeError(f"launch_project.sh 退出码 {proc.returncode}：\n{tail}")
        home = f"{self.settings.data_root}/{pid}/.hermes"
        workspace = f"{self.settings.data_root}/{pid}/workspace"
        key = self._read_api_key(home)
        return Project(project_id=pid, port=port, key=key, home=home, workspace=workspace)

    @staticmethod
    def _read_api_key(home: str) -> str:
        env_path = Path(home) / "profiles" / "ceo" / ".env"
        if not env_path.exists():
            return ""
        for line in env_path.read_text().splitlines():
            if line.startswith("API_SERVER_KEY="):
                return line.split("=", 1)[1].strip()
        return ""

    async def chat(self, project: Project, message: str, session_id: str) -> dict:
        """把用户消息转发给该项目的 CEO（OpenAI 兼容接口）。"""
        url = f"http://127.0.0.1:{project.port}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {project.key}",
            "X-Hermes-Session-Id": session_id,
            "X-Hermes-Session-Key": f"agent:ceo:webui:{session_id}",
        }
        payload = {"model": "ceo", "messages": [{"role": "user", "content": message}]}
        async with httpx.AsyncClient(timeout=600) as client:
            r = await client.post(url, headers=headers, json=payload)
            return r.json()

    def kanban(self, project: Project, *args: str) -> list | dict:
        """执行 ``hermes kanban --board <pid> <args> --json`` 并解析输出。

        命令失败或输出非 JSON 时抛 RuntimeError，由端点转成 502，避免
        json.loads 在错误输出上抛 JSONDecodeError 直接 500。
        """
        env = dict(os.environ, HERMES_HOME=project.home)
        cmd = ["hermes", "kanban", "--board", project.project_id, *args]
        # 加超时（D20）：单个 hermes kanban 卡住不应阻塞 /api/projects 列表（它会逐项目调用）。
        try:
            out = subprocess.run(cmd, env=env, capture_output=True, text=True,
                                 timeout=int(os.environ.get("KANBAN_TIMEOUT", "15")))
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"hermes kanban timed out after {exc.timeout}s") from exc
        if out.returncode != 0:
            raise RuntimeError(f"hermes kanban failed: {out.stderr.strip() or out.stdout.strip()}")
        try:
            return json.loads(out.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"hermes kanban returned non-JSON: {out.stdout[:200]!r}") from exc

    @staticmethod
    def _run_checked(cmd: list, env: dict, what: str) -> None:
        """跑 hermes 子命令并在失败时带出真实原因（否则端点只能回一个无信息的 500）。

        与 launch 同口径：捕获 stdout/stderr，非 0 时取末尾若干行抛 RuntimeError，
        由端点（confirm-plan / change-request）转成带原因的 502，运维可诊断。
        """
        try:
            proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
        except FileNotFoundError as exc:
            raise RuntimeError(f"{what} 失败：找不到 hermes 命令（{exc}）") from exc
        if proc.returncode != 0:
            lines = [l for l in ((proc.stdout or "") + (proc.stderr or "")).splitlines()
                     if l.strip()]
            tail = "\n".join(lines[-10:])[-1000:] or "(无输出)"
            raise RuntimeError(f"{what} 失败（退出码 {proc.returncode}）：{tail}")

    def kanban_create(self, project: Project, title: str, assignee: str, *extra: str) -> None:
        env = dict(os.environ, HERMES_HOME=project.home)
        cmd = [
            "hermes", "kanban", "--board", project.project_id,
            "create", title, "--assignee", assignee, *extra,
        ]
        self._run_checked(cmd, env, "创建 kanban 卡")

    def swarm(self, project: Project, goal: str, workers: list[str],
              verifier: str, synthesizer: str) -> None:
        """显式创建设计委员会 swarm（fan-out + critic + synthesizer）。

        对应设计方案 §5/§7：编排由平台显式发起，不依赖 CEO 自觉。
        """
        env = dict(os.environ, HERMES_HOME=project.home)
        # Hermes v0.16：单数、可重复的 worker flag，格式 PROFILE:TITLE（非复数逗号串）。
        cmd = ["hermes", "kanban", "--board", project.project_id, "swarm", goal]
        for w in workers:
            cmd += ["--worker", f"{w}:{w}"]
        cmd += ["--verifier", verifier, "--synthesizer", synthesizer]
        self._run_checked(cmd, env, "启动 swarm")


# --------------------------------------------------------------------------- #
# 请求体模型
# --------------------------------------------------------------------------- #
class CreateProject(BaseModel):
    project_id: str


class Msg(BaseModel):
    message: str
    session_id: str = "main"


class ChangeRequest(BaseModel):
    change: str


class ConfirmPlan(BaseModel):
    # 用户在 Step 5 与 CEO 对齐后的需求双层结构（YAML 文本）。控制平面据此落盘
    # workspace/design/requirements.yaml —— 闭合"谁来写 requirements.yaml"的链路：
    # CEO 无 file 工具集写不了，产品委员会 swarm 又以它为输入，必须由网关持久化。
    requirements: Optional[str] = None


# --------------------------------------------------------------------------- #
# App 工厂
# --------------------------------------------------------------------------- #
def create_app(
    settings: Optional[Settings] = None,
    registry: Optional[ProjectRegistry] = None,
    gateway: Optional[HermesGateway] = None,
) -> FastAPI:
    settings = settings or Settings()
    # 安全闸：绑定到非本机地址（局域网/公网）时，绝不允许沿用默认 token 'change-me'——
    # 否则等于把建项目/对话/读产物的能力裸奔给整个网络。设 PLATFORM_TOKEN 后再开放。
    _bind = os.environ.get("PLATFORM_BIND_HOST", "127.0.0.1")
    if _bind not in ("127.0.0.1", "localhost", "::1") and settings.token == "change-me":
        raise RuntimeError(
            f"拒绝以默认 token 'change-me' 绑定非本机地址 ({_bind})：请先设 PLATFORM_TOKEN 再开放到局域网"
            "（控制平面仅 X-Token 鉴权、无 TLS，仅限可信局域网）。")
    registry = registry or ProjectRegistry()
    gateway = gateway or HermesGateway(settings)
    ports = PortAllocator(settings.data_root, settings.base_port)
    rehydrate(registry, settings)

    @asynccontextmanager
    async def _lifespan(_app):
        # 内嵌编排器（可靠地推进流水线）。真机 shi 暴露：systemd --user timer 重启后失效
        # （"Failed to connect to bus"），orchestrator 不自动跑、流水线全靠人工 tick。控制平面是
        # 常驻 systemd 服务、最可靠的进程，故把 orchestrator tick 内嵌进来：
        #   * 仅当 AUTOCODE_EMBEDDED_ORCHESTRATOR=1 才开（部署写入；测试不设→不启动，零副作用）；
        #   * 跨进程 tick_lock 与 systemd timer 互斥，绝不双跑；
        #   * tick_all 是同步 subprocess 逻辑，用 asyncio.to_thread 跑，不阻塞事件循环；关停时 cancel。
        task = None
        if os.environ.get("AUTOCODE_EMBEDDED_ORCHESTRATOR", "0") == "1":
            from orchestrator import Orchestrator, tick_lock
            interval = int(os.environ.get("ORCHESTRATOR_INTERVAL_SECONDS", "60"))

            def _tick_once():
                with tick_lock(settings.data_root) as got:
                    if got:
                        Orchestrator(gateway, data_root=settings.data_root).tick_all()

            async def _loop():
                while True:
                    try:
                        await asyncio.to_thread(_tick_once)
                    except Exception as exc:
                        print(f"[orchestrator] embedded tick error: {exc}", file=sys.stderr)
                    await asyncio.sleep(interval)

            task = asyncio.create_task(_loop())
        try:
            yield
        finally:
            if task:
                task.cancel()

    app = FastAPI(title="Auto-Coding Control Plane", lifespan=_lifespan)

    def auth(token: Optional[str]) -> None:
        if token != settings.token:
            raise HTTPException(401, "bad token")

    def get_project(pid: str) -> Project:
        project = registry.get(pid)
        if project is None:
            raise HTTPException(404, f"project '{pid}' not found")
        return project

    @app.post("/api/projects")
    def create_project(body: CreateProject, x_token: str = Header(None)):
        auth(x_token)
        validate_project_id(body.project_id)
        if body.project_id in registry:
            raise HTTPException(409, f"project '{body.project_id}' already exists")
        port = ports.allocate(body.project_id)
        try:
            project = gateway.launch(body.project_id, port)
        except Exception as exc:
            # 建项目失败（launch_project.sh 非 0：磁盘不足/缺 key/docker 不可用等）：
            # 回滚端口分配，避免端口泄漏成孤儿占用（D16/D18），并回明确 502 而非泛化 500。
            ports.release(body.project_id)
            detail = exc.stderr.strip() if hasattr(exc, "stderr") and exc.stderr else str(exc)
            # 建项目失败无 project 对象、workspace 未必存在 → 审计落服务端日志（journald 持久），
            # 同时 502 带原因回调用方。
            print(f"{datetime.now(timezone.utc).isoformat()} [audit] project_create_failed "
                  f"pid={body.project_id}: {detail}", file=sys.stderr)
            raise HTTPException(502, f"failed to create project '{body.project_id}': {detail}")
        registry.add(project)
        _audit(project, "user", "project_created", {"port": project.port})
        return {"project_id": project.project_id, "port": project.port, "status": "ready"}

    def _log_turn(project: Project, session_id: str, role: str, content: str) -> None:
        """把一轮对话追加到平台自有 JSONL（不读 Hermes 私有 SQLite——schema 不稳）。"""
        try:
            d = Path(project.workspace) / ".autocode" / "conversations"
            d.mkdir(parents=True, exist_ok=True)
            with (d / f"{session_id}.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                                     "role": role, "content": content}, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _audit(project: Project, actor: str, action: str,
               detail: Optional[dict] = None, result: str = "ok") -> None:
        """统一审计事件流：append 到 <workspace>/.autocode/audit.jsonl（与 orchestrator.audit_append
        同格式：ts/actor/action/detail/result）。记录关键动作与错误，供 /audit 端点与 Web UI 事件页回溯。"""
        try:
            d = Path(project.workspace) / ".autocode"
            d.mkdir(parents=True, exist_ok=True)
            with (d / "audit.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "actor": actor,
                                     "action": action, "detail": detail or {}, "result": result},
                                    ensure_ascii=False) + "\n")
        except OSError:
            pass

    @app.post("/api/projects/{pid}/messages")
    async def message_ceo(pid: str, body: Msg, x_token: str = Header(None)):
        auth(x_token)
        validate_session_id(body.session_id)
        project = get_project(pid)
        _log_turn(project, body.session_id, "user", body.message)
        try:
            reply = await gateway.chat(project, body.message, body.session_id)
        except Exception as exc:
            # CEO gateway 宕/超时/网络错误：回明确 502 而非泛化 500（用户消息已记，下次可重发）。
            raise HTTPException(502, f"CEO 网关无响应: {exc}")
        try:
            text = reply["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            text = json.dumps(reply, ensure_ascii=False)
        _log_turn(project, body.session_id, "assistant", text)
        return reply

    @app.post("/api/projects/{pid}/confirm-plan")
    async def confirm_plan(pid: str, body: Optional[ConfirmPlan] = None,
                           x_token: str = Header(None)):
        auth(x_token)
        project = get_project(pid)
        ws = Path(project.workspace)
        # 幂等：产品委员会/架构委员会已起过就不重复起（用户多点几次"确认需求"不应起多个 swarm）。
        state_path = ws / ".autocode" / "state.json"
        state: dict = {}
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text())
            except (ValueError, OSError):
                state = {}
        if state.get("product_started") or state.get("arch_started"):
            return {"status": "already-started", "swarm": "product-council"}
        # 闭环：把需求双层结构落盘为 requirements.yaml（CEO 无 file 工具写不了；产品委员会 swarm
        # 以它为输入）。优先用请求体显式传入；Web UI 的"确认需求"只点按钮、不带 requirements，
        # 则**从 CEO 对话推导**——让 CEO 仅输出一个 yaml 代码块（core_need/extended_need/non_goals），
        # 由网关抽取落盘。否则产品委员会的 goal 引用的 design/requirements.yaml 根本不存在（真机隐患）。
        design_dir = ws / "design"
        design_dir.mkdir(parents=True, exist_ok=True)
        req_path = design_dir / "requirements.yaml"
        if body and body.requirements:
            req_path.write_text(body.requirements)
        elif not req_path.exists():
            text = ""
            try:
                r = await gateway.chat(
                    project,
                    "根据我们刚才确认的【定版需求】，输出本项目的需求结构。**只输出一个 yaml 代码块**，"
                    "含 core_need、extended_need、non_goals、acceptance_core 四个键（与你给用户的定版四块一致），"
                    "不要任何解释文字。",
                    "main")
                text = r["choices"][0]["message"]["content"]
            except Exception:
                text = ""
            m = re.search(r"```(?:ya?ml)?\s*\n(.*?)```", text, re.S)
            content = (m.group(1) if m else text).strip()
            req_path.write_text(content
                or "core_need: 见对话记录\nextended_need: []\nnon_goals: []\nacceptance_core: []\n")
        if not req_path.exists():
            raise HTTPException(500, "requirements.yaml 落盘失败，未启动产品委员会")
        # 显式编排：平台直接创建产品委员会 swarm（见手册阶段 7）。
        # swarm 失败（hermes 报错/profile 缺失）要带原因回 502，而非 opaque 500；
        # 此时 product_started 尚未落盘，重试不会重复起 swarm。
        try:
            gateway.swarm(
                project,
                goal="产出 PRD：基于 design/requirements.yaml 的 core_need",
                workers=["pm-research-a", "pm-research-b"],
                verifier="pm-critic",
                synthesizer="pm-synthesizer",
            )
        except RuntimeError as exc:
            _audit(project, "system", "error",
                   {"endpoint": "confirm-plan", "reason": str(exc)}, "error")
            raise HTTPException(502, f"启动产品委员会失败：{exc}")
        # 落 product_started（与 orchestrator 共享 state，互相幂等去重）。
        state.update(product_started=True, stage="product")
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2))
        # 系统消息写进对话（用户在 Web UI 能看到"已确认、产品委员会已启动"）。
        _log_turn(project, "main", "system", "✅ 已确认需求，产品委员会 swarm 已启动，开始产出 PRD。")
        # 通知 CEO 跟进。
        reply = await gateway.chat(
            project,
            "用户已确认需求双层结构，产品委员会 swarm 已启动；请跟进设计与构建直至 core_need 达成。",
            "main",
        )
        _audit(project, "user", "plan_confirmed", {"swarm": "product-council"})
        return {"status": "plan-confirmed", "swarm": "product-council", "ceo": reply}

    @app.post("/api/projects/{pid}/architecture-swarm")
    def architecture_swarm(pid: str, x_token: str = Header(None)):
        """产品委员会产出 PRD 后，显式起架构委员会 swarm（闭合 KNOWN-04）。

        生产由 orchestrator 状态机在 PRD.md 出现后自动起（见 orchestrator.py tick）；
        本端点是手动兜底/重试入口。**幂等**：与 orchestrator 共享
        workspace/.autocode/state.json 的 arch_started 标记——已起过则直接返回，
        避免手动调用与状态机重复起架构委员会 swarm（评审 D）。
        """
        auth(x_token)
        project = get_project(pid)
        state_path = Path(project.workspace) / ".autocode" / "state.json"
        state: dict = {}
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text())
            except (ValueError, OSError):
                state = {}
        if state.get("arch_started"):
            return {"status": "architecture-swarm-already-started",
                    "swarm": "architecture-council"}
        try:
            gateway.swarm(
                project,
                goal="产出 ADR + interface-spec + code-spec + TODO：基于 design/PRD.md",
                workers=["arch-simple", "arch-scale", "arch-security"],
                verifier="arch-critic",
                synthesizer="arch-synthesizer",
            )
        except RuntimeError as exc:
            _audit(project, "system", "error",
                   {"endpoint": "architecture-swarm", "reason": str(exc)}, "error")
            raise HTTPException(502, f"启动架构委员会失败：{exc}")
        # 落 arch_started，与 orchestrator.tick 幂等标记一致，互相去重。
        state.update(arch_started=True, stage="architecture")
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2))
        _audit(project, "user", "architecture_swarm", {"swarm": "architecture-council"})
        return {"status": "architecture-swarm-started", "swarm": "architecture-council"}

    @app.post("/api/projects/{pid}/change-requests")
    def change_request(pid: str, body: ChangeRequest, x_token: str = Header(None)):
        auth(x_token)
        project = get_project(pid)
        # 建 change-request 卡，assignee = change-guardian（先设计再执行，见阶段 10）。
        try:
            gateway.kanban_create(
                project,
                f"变更请求：{body.change}",
                "change-guardian",
                "--goal",
            )
        except RuntimeError as exc:
            _audit(project, "system", "error",
                   {"endpoint": "change-requests", "reason": str(exc)}, "error")
            raise HTTPException(502, f"提交变更请求失败：{exc}")
        _audit(project, "user", "change_request", {"change": body.change})
        return {"status": "change-request-created", "change": body.change}

    @app.get("/api/projects/{pid}/tasks")
    def list_tasks(pid: str, x_token: str = Header(None)):
        auth(x_token)
        project = get_project(pid)
        try:
            out = gateway.kanban(project, "list", "--json")
        except RuntimeError as exc:
            raise HTTPException(502, str(exc))
        # 归一成数组（与 orchestrator._cards 同口径）：保证前端看板 Array.isArray 契约，
        # 即便某版本 CLI 返回非 list 也不让看板崩。
        return out if isinstance(out, list) else []

    @app.get("/api/projects/{pid}/requirements")
    def requirements(pid: str, x_token: str = Header(None)):
        auth(x_token)
        project = get_project(pid)
        path = Path(project.workspace) / "design" / "requirements.yaml"
        if path.exists():
            return {"requirements": path.read_text()}
        return {"requirements": None}

    @app.get("/api/projects/{pid}/artifacts")
    def artifacts(pid: str, x_token: str = Header(None)):
        auth(x_token)
        project = get_project(pid)
        ws = Path(project.workspace)
        items = []
        # 完整交付面：不只 design/src，还含 tests、QA/发布报告、产物（与 artifact-content 白名单一致）。
        for sub in ("design", "src", "tests", "reports/qa", "reports/release", "dist"):
            base = ws / sub
            if base.exists():
                for f in sorted(base.rglob("*")):
                    if f.is_file():
                        items.append({
                            "path": str(f.relative_to(ws)),
                            "size": f.stat().st_size,
                        })
        for name in ("README.md",):
            f = ws / name
            if f.is_file():
                items.append({"path": name, "size": f.stat().st_size})
        return {"artifacts": items}

    @app.get("/api/projects/{pid}/demo")
    def demo(pid: str, x_token: str = Header(None)):
        auth(x_token)
        project = get_project(pid)
        src = Path(project.workspace) / "src"
        return {"demo_path": str(src), "exists": src.exists()}

    @app.get("/api/projects/{pid}/deliverable")
    def deliverable(pid: str, x_token: str = Header(None)):
        auth(x_token)
        project = get_project(pid)
        ws = Path(project.workspace)
        # is_done 必须四者皆满足，而不只看 QA release_allowed：阶段 complete + 发布清单存在 +
        # QA 放行 + 交付完整性通过。任一缺失即视为未真正交付。
        state = {}
        sp = ws / ".autocode" / "state.json"
        if sp.exists():
            try:
                state = json.loads(sp.read_text())
            except (ValueError, OSError):
                pass
        manifest = None
        mf = ws / "reports" / "release" / "manifest.json"
        if mf.exists():
            try:
                manifest = json.loads(mf.read_text())
            except (ValueError, OSError):
                manifest = None
        qa = {}
        qf = ws / "reports" / "qa" / "status.json"
        if qf.exists():
            try:
                qa = json.loads(qf.read_text())
            except (ValueError, OSError):
                pass
        integrity_ok = True
        integ = (qa.get("integrity") or {}) if isinstance(qa, dict) else {}
        if integ:
            integrity_ok = bool(integ.get("git_clean", True) is True
                                and integ.get("expected_files_present", True) is True
                                and not integ.get("todo_markers")
                                and not integ.get("scope_violations"))
        is_done = bool(state.get("stage") == "complete" and manifest is not None
                       and qa.get("release_allowed") is True and integrity_ok)
        # 入口提示：优先用 manifest 的 run_command；否则扫常见入口文件（仅作 fallback 提示）。
        run_command = manifest.get("run_command") if isinstance(manifest, dict) else None
        if not run_command:
            for cand in ("README.md", "src/main.py", "main.py", "run.sh", "package.json"):
                if (ws / cand).exists():
                    run_command = f"see {cand}"
                    break
        return {"is_done": is_done, "stage": state.get("stage", "created"),
                "completion_mode": state.get("completion_mode"),
                "manifest": manifest, "run_command": run_command,
                "release_allowed": qa.get("release_allowed") if isinstance(qa, dict) else None,
                "integrity_ok": integrity_ok}

    @app.get("/api/projects/{pid}/events")
    def events(pid: str, x_token: str = Header(None)):
        auth(x_token)
        project = get_project(pid)

        def stream():
            # 最小实现：推送一次当前看板快照。生产可改为订阅 Redis 事件总线持续推送。
            try:
                snapshot = gateway.kanban(project, "list", "--json")
            except RuntimeError as exc:
                yield f"event: error\ndata: {json.dumps(str(exc), ensure_ascii=False)}\n\n"
                return
            yield f"event: tasks\ndata: {json.dumps(snapshot, ensure_ascii=False)}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    # ------------------------------------------------------------------ #
    # 用户面只读端点（Web UI 后端）。只读、白名单、防穿越。
    # ------------------------------------------------------------------ #
    def _kanban_summary(project: Project) -> tuple[dict, int]:
        try:
            out = gateway.kanban(project, "list", "--json")
        except RuntimeError:
            return {}, 0
        # 归一成数组并跳过非 dict 项（与 orchestrator._cards 一致）：单个项目 kanban 输出
        # 异常不得让 /api/projects 整表 500（D20 设计意图：一个项目的问题不阻塞列表）。
        tasks = out if isinstance(out, list) else []
        by_status: dict = {}
        for t in tasks:
            if isinstance(t, dict):
                s = t.get("status", "?")
                by_status[s] = by_status.get(s, 0) + 1
        return by_status, len(tasks)

    @app.get("/api/projects")
    def list_projects(x_token: str = Header(None)):
        auth(x_token)
        result = []
        for project in registry.all():            # 公开接口，不碰私有 _projects
            ws = Path(project.workspace)
            stage = "created"
            sp = ws / ".autocode" / "state.json"
            if sp.exists():
                try:
                    stage = json.loads(sp.read_text()).get("stage", "created")
                except (ValueError, OSError):
                    pass
            by_status, total = _kanban_summary(project)
            result.append({"project_id": project.project_id, "port": project.port,
                           "stage": stage, "task_summary": by_status, "total_tasks": total})
        return {"projects": result}

    @app.get("/api/projects/{pid}/state")
    def project_state(pid: str, x_token: str = Header(None)):
        auth(x_token)
        project = get_project(pid)
        ws = Path(project.workspace)
        state = {"stage": "created"}
        sp = ws / ".autocode" / "state.json"
        if sp.exists():
            try:
                state = json.loads(sp.read_text())
            except (ValueError, OSError):
                pass
        design_files = {n: (ws / "design" / n).exists() for n in
                        ("requirements.yaml", "PRD.md", "ADR.md", "approved_versions.txt", "TODO.md")}
        qa_status = {}
        qa = ws / "reports" / "qa" / "status.json"
        if qa.exists():
            try:
                qa_status = json.loads(qa.read_text())
            except (ValueError, OSError):
                pass
        return {"project_id": pid, "state": state, "design_files": design_files,
                "qa_status": qa_status, "workspace": str(ws)}

    # 只读白名单：只放行交付面（design/src/tests/reports-qa/reports-release/dist + README，只读）；
    # 绝不暴露 .hermes / .autocode 等内部运行态目录，并用 is_relative_to 防路径穿越。
    def _safe_artifact(ws: Path, rel: str) -> Path:
        root = ws.resolve()
        target = (root / rel).resolve()
        if not target.is_relative_to(root):               # 防穿越（比 startswith 严谨）
            raise HTTPException(403, "path traversal denied")
        relposix = target.relative_to(root).as_posix()
        top = relposix.split("/", 1)[0]
        ok = (top in {"design", "src", "tests", "dist"}
              or relposix.startswith("reports/qa/")
              or relposix.startswith("reports/release/")
              or relposix == "README.md")
        if not ok:
            raise HTTPException(403, "artifact path not allowed")
        if not target.is_file():
            raise HTTPException(404, "file not found")
        return target

    @app.get("/api/projects/{pid}/artifact-content")
    def artifact_content(pid: str, path: str = Query(...), x_token: str = Header(None)):
        auth(x_token)
        project = get_project(pid)
        target = _safe_artifact(Path(project.workspace), path)
        max_size = 512 * 1024
        try:
            data = target.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            return {"binary": True, "content": None, "truncated": False}
        return {"content": data[:max_size], "truncated": len(data) > max_size, "binary": False}

    @app.get("/api/projects/{pid}/conversation")
    def get_conversation(pid: str, session_id: str = Query("main"), x_token: str = Header(None)):
        auth(x_token)
        validate_session_id(session_id)
        project = get_project(pid)
        # 只读平台自有 JSONL（见 message_ceo 的 _log_turn），不猜 Hermes 内部 sqlite。
        f = Path(project.workspace) / ".autocode" / "conversations" / f"{session_id}.jsonl"
        msgs = []
        if f.exists():
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    try:
                        msgs.append(json.loads(line))
                    except ValueError:
                        pass
        return {"messages": msgs, "session_id": session_id}

    @app.get("/api/projects/{pid}/audit")
    def get_audit(pid: str, x_token: str = Header(None)):
        """统一审计事件流（只读）：一站式回溯"什么时候什么地方发生了什么"。

        读平台自有 <workspace>/.autocode/audit.jsonl（控制平面动作 + 编排器阶段跃迁 + 错误）。
        按时间倒序返回，最多 500 条（避免超大响应）。
        """
        auth(x_token)
        project = get_project(pid)
        f = Path(project.workspace) / ".autocode" / "audit.jsonl"
        events = []
        if f.exists():
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except ValueError:
                        pass
        return {"events": events[-500:][::-1]}   # 最新在前

    _SECURITY_HEADERS = {
        # 纵深防御：即便前端某处有 XSS，CSP 也限制可加载资源与可外联的域。
        # script/style 允许内联（单文件 UI 用），但 connect-src 'self' 是关键：即便注入脚本
        # 执行了，也无法把 token 外联到 evil.com（配合前端转义渲染，双重挡住存储型 XSS）。
        "Content-Security-Policy": ("default-src 'self'; img-src 'self' data:; "
                                    "script-src 'self' 'unsafe-inline'; "
                                    "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
                                    "frame-ancestors 'none'"),
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
    }

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def webui_root():
        p = Path(__file__).parent / "webui.html"
        if not p.exists():
            return HTMLResponse("<h1>webui.html 未部署（把前端放到 ~/platform/webui.html）</h1>", 404)
        return HTMLResponse(p.read_text(encoding="utf-8"), headers=_SECURITY_HEADERS)

    return app


# 供 ``uvicorn control_plane:app`` 使用的默认实例。
app = create_app()
