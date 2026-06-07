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
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel


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
        """执行 ``hermes kanban --board <pid> <args> --json`` 并解析输出。"""
        env = dict(os.environ, HERMES_HOME=project.home)
        cmd = ["hermes", "kanban", "--board", project.project_id, *args]
        out = subprocess.run(cmd, env=env, capture_output=True, text=True)
        return json.loads(out.stdout or "[]")

    def kanban_create(self, project: Project, title: str, assignee: str, *extra: str) -> None:
        env = dict(os.environ, HERMES_HOME=project.home)
        cmd = [
            "hermes", "kanban", "--board", project.project_id,
            "create", title, "--assignee", assignee, *extra,
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
    next_port = [settings.base_port]

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
        if body.project_id in registry:
            raise HTTPException(409, f"project '{body.project_id}' already exists")
        port = next_port[0]
        next_port[0] += 1
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
        # 用户确认方案 → 通知 CEO 启动设计委员会与构建流水线（见手册阶段 7/8）。
        return await gateway.chat(
            project,
            "用户已确认需求双层结构，请启动设计委员会并推进到 core_need 达成。",
            "main",
        )

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
        return gateway.kanban(project, "list", "--json")

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
            snapshot = gateway.kanban(project, "list", "--json")
            yield f"event: tasks\ndata: {json.dumps(snapshot, ensure_ascii=False)}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    return app


# 供 ``uvicorn control_plane:app`` 使用的默认实例。
app = create_app()
