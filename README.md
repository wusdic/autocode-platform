# autocode-platform

多项目自动编程平台——基于 Hermes Agent v0.16.0 的完整设计方案、操作手册与**可部署实现**。

每个项目 = 一个独立 Hermes 实例 + 一块独立 Kanban 看板；项目内由 CEO（只沟通、不干活）
通过看板把任务派给一组命名角色 profile（产品 / 架构 / 研发 / 质控 / 变更管控），
各角色用不同大模型协作与互相质疑；用户只通过自建的 Web/API 网关对接 CEO，其余全自动。

## 设计文档

- **[01-最终设计方案.md](01-最终设计方案.md)**：完整架构设计，包含五条底层原则、三层隔离模型、13 个角色权限表、需求双层结构、设计委员会多模型质疑机制、先设计再执行的双层硬拦、90 轮问题处理、风险规避表。
- **[02-从零开始操作手册.md](02-从零开始操作手册.md)**：13 个阶段可逐条执行的操作手册。

## 仓库结构

```
platform/
  control_plane.py      FastAPI 控制平面（对外唯一入口，已修 bug + 补齐 §8 全部端点）
  policy_plugin.py      第二层权限硬拦（pre_tool_call hook 三道闸）
  launch_project.sh     项目启动器：建实例 + board + 17 个角色 profile + gateway
  watchdog.sh           处理 90 轮预算 / 崩溃 / 超时，自动建 continuation 卡
platform-base/
  templates/            AGENTS.md（全局约束）+ 各角色 SOUL.*.md + requirements.yaml 模板
  skills/               跨项目复用的 skill 快照（占位）
scripts/
  00-host-setup.sh      宿主机准备（依赖 / Docker / Hermes，对应阶段 0）
  01-deploy-platform.sh 把仓库文件部署到 ~/platform、~/platform-base 运行时布局
tests/
  test_policy_plugin.py 权限三道闸单测
  test_control_plane.py 控制平面端点单测（FastAPI TestClient + 注入式 FakeGateway）
```

## 快速开始

> ⚠️ 真正运行需要一台 **Ubuntu 22.04+** 服务器，装好 **Hermes Agent v0.16 + Docker + PostgreSQL + Redis**。
> 本仓库的 Python/脚本可在任意机器上测试，但端到端跑通必须在上述环境，详见操作手册。

```bash
# 1) 宿主机准备（依赖 + Docker + Hermes）
./scripts/00-host-setup.sh
hermes setup --portal          # 配模型供应商

# 2) 部署平台文件到运行时布局
./scripts/01-deploy-platform.sh

# 3) 启动控制平面（只绑 127.0.0.1）
PLATFORM_TOKEN="$(openssl rand -hex 16)" \
  ~/platform/venv/bin/uvicorn control_plane:app \
  --app-dir ~/platform --host 127.0.0.1 --port 9000

# 4) 创建第一个项目并与 CEO 沟通
curl -s -X POST http://127.0.0.1:9000/api/projects \
  -H "X-Token: $PLATFORM_TOKEN" -d '{"project_id":"demo1"}'
```

## 开发与测试

```bash
make dev-install   # 安装 fastapi/httpx/pytest 等
make test          # 运行单元测试（17 个用例）
make lint-sh       # bash 语法检查
```

## 对外 API（控制平面，对应设计方案 §8）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/projects` | 创建项目（建实例 + board + profiles + gateway） |
| POST | `/api/projects/{id}/messages` | 与 CEO 多轮沟通需求 |
| POST | `/api/projects/{id}/confirm-plan` | 确认方案 → 启动设计委员会与构建 |
| POST | `/api/projects/{id}/change-requests` | 提交变更（建 change-guardian 卡，触发设计闸门） |
| GET | `/api/projects/{id}/tasks` | 读 Kanban 看板 |
| GET | `/api/projects/{id}/requirements` | 读需求双层结构 |
| GET | `/api/projects/{id}/artifacts` | 阶段成果产物清单 |
| GET | `/api/projects/{id}/demo` | 阶段性可运行成果 |
| GET | `/api/projects/{id}/events` | SSE 实时事件流（看板快照） |

**安全红线**：Hermes 的 `/v1/*` 与 dashboard 绝不直接对外（直连等于暴露 terminal/file/web 全工具）；
对外鉴权全在本网关，且只绑 localhost，生产环境前面架 nginx/Caddy 做 TLS。

## 技术栈

Hermes Agent v0.16 · Kanban multi-agent · FastAPI · Docker · PostgreSQL · Redis
