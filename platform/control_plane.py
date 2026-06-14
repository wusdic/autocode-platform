"""FastAPI 控制平面 —— 对外唯一入口。

对应《01-最终设计方案.md》第 8 节、《02-从零开始操作手册.md》阶段 4。

安全红线：Hermes 的 /v1/* 与 dashboard 绝不直接对外；对外鉴权全在本网关。
本网关只绑 127.0.0.1，生产环境前面架 nginx/Caddy 做 TLS + 对外鉴权。

相对手册原稿的改进：
  * 修复 ``PROJECTS.get(pid) or HTTPException(404)`` 不抛异常的 bug，统一用
    ``get_project`` 在缺失时 raise 404。
  * 把对 Hermes 的进程/HTTP 交互收敛到可注入的 ``HermesGateway``，便于单测。
  * 补齐设计方案第 8 节列出但手册骨架缺失的端点：events / confirm-plan /
    change-requests / artifacts / demo。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

try:
    import fcntl  # POSIX 文件锁；非 POSIX 平台降级为无锁
except ImportError:  # pragma: no cover
    fcntl = None

# 严格 slug：防止 project_id 路径穿越 / systemd 单元名污染。
PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,63}$")


def validate_project_id(pid: str) -> None:
    if not PROJECT_ID_RE.match(pid or ""):
        raise HTTPException(
            400,
            "invalid project_id; must match ^[a-z0-9][a-z0-9_-]{2,63}$",
        )


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
@dataclass
class Settings:
    token: str = field(default_factory=lambda: os.environ.get("PLATFORM_TOKEN", "change-me"))
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
        """运行 launch_project.sh，建实例 + board + profiles + gateway。"""
        subprocess.run(["bash", self.settings.launcher, pid, str(port)], check=True)
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
        out = subprocess.run(cmd, env=env, capture_output=True, text=True)
        if out.returncode != 0:
            raise RuntimeError(f"hermes kanban failed: {out.stderr.strip() or out.stdout.strip()}")
        try:
            return json.loads(out.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"hermes kanban returned non-JSON: {out.stdout[:200]!r}") from exc

    def kanban_create(self, project: Project, title: str, assignee: str, *extra: str) -> None:
        env = dict(os.environ, HERMES_HOME=project.home)
        cmd = [
            "hermes", "kanban", "--board", project.project_id,
            "create", title, "--assignee", assignee, *extra,
        ]
        subprocess.run(cmd, env=env, check=True)

    def swarm(self, project: Project, goal: str, workers: list[str],
              verifier: str, synthesizer: str) -> None:
        """显式创建设计委员会 swarm（fan-out + critic + synthesizer）。

        对应设计方案 §5/§7：编排由平台显式发起，不依赖 CEO 自觉。
        """
        env = dict(os.environ, HERMES_HOME=project.home)
        cmd = [
            "hermes", "kanban", "--board", project.project_id, "swarm", goal,
            "--workers", ",".join(workers),
            "--verifier", verifier,
            "--synthesizer", synthesizer,
        ]
        subprocess.run(cmd, env=env, check=True)


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


# --------------------------------------------------------------------------- #
# App 工厂
# --------------------------------------------------------------------------- #
def create_app(
    settings: Optional[Settings] = None,
    registry: Optional[ProjectRegistry] = None,
    gateway: Optional[HermesGateway] = None,
) -> FastAPI:
    settings = settings or Settings()
    registry = registry or ProjectRegistry()
    gateway = gateway or HermesGateway(settings)
    ports = PortAllocator(settings.data_root, settings.base_port)
    rehydrate(registry, settings)

    app = FastAPI(title="Auto-Coding Control Plane")

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
        project = gateway.launch(body.project_id, port)
        registry.add(project)
        return {"project_id": project.project_id, "port": project.port, "status": "ready"}

    @app.post("/api/projects/{pid}/messages")
    async def message_ceo(pid: str, body: Msg, x_token: str = Header(None)):
        auth(x_token)
        project = get_project(pid)
        return await gateway.chat(project, body.message, body.session_id)

    @app.post("/api/projects/{pid}/confirm-plan")
    async def confirm_plan(pid: str, x_token: str = Header(None)):
        auth(x_token)
        project = get_project(pid)
        # 显式编排：平台直接创建产品委员会 swarm，而非只 prompt CEO（见手册阶段 7）。
        # 架构委员会在 PRD 产出后由 dispatcher / change-guardian 衔接。
        gateway.swarm(
            project,
            goal="产出 PRD：基于 workspace/design/requirements.yaml 的 core_need",
            workers=["pm-research-a", "pm-research-b"],
            verifier="pm-critic",
            synthesizer="pm-synthesizer",
        )
        # 同时通知 CEO 跟进推进到 core_need 达成。
        reply = await gateway.chat(
            project,
            "用户已确认需求双层结构，产品委员会 swarm 已启动；请跟进设计与构建直至 core_need 达成。",
            "main",
        )
        return {"status": "plan-confirmed", "swarm": "product-council", "ceo": reply}

    @app.post("/api/projects/{pid}/change-requests")
    def change_request(pid: str, body: ChangeRequest, x_token: str = Header(None)):
        auth(x_token)
        project = get_project(pid)
        # 建 change-request 卡，assignee = change-guardian（先设计再执行，见阶段 10）。
        gateway.kanban_create(
            project,
            f"变更请求：{body.change}",
            "change-guardian",
            "--goal",
        )
        return {"status": "change-request-created", "change": body.change}

    @app.get("/api/projects/{pid}/tasks")
    def list_tasks(pid: str, x_token: str = Header(None)):
        auth(x_token)
        project = get_project(pid)
        try:
            return gateway.kanban(project, "list", "--json")
        except RuntimeError as exc:
            raise HTTPException(502, str(exc))

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
        for sub in ("design", "src"):
            base = ws / sub
            if base.exists():
                for f in sorted(base.rglob("*")):
                    if f.is_file():
                        items.append({
                            "path": str(f.relative_to(ws)),
                            "size": f.stat().st_size,
                        })
        return {"artifacts": items}

    @app.get("/api/projects/{pid}/demo")
    def demo(pid: str, x_token: str = Header(None)):
        auth(x_token)
        project = get_project(pid)
        src = Path(project.workspace) / "src"
        return {"demo_path": str(src), "exists": src.exists()}

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

    return app


# 供 ``uvicorn control_plane:app`` 使用的默认实例。
app = create_app()
