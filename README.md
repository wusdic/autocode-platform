# autocode-platform

[![CI](https://github.com/wusdic/autocode-platform/actions/workflows/ci.yml/badge.svg)](https://github.com/wusdic/autocode-platform/actions/workflows/ci.yml)

多项目自动编程平台——基于 Hermes Agent v0.16.0 的完整设计方案、操作手册与**可部署实现**。

每个项目 = 一个独立 Hermes 实例 + 一块独立 Kanban 看板；项目内由 CEO（只沟通、不干活）
通过看板把任务派给一组命名角色 profile（产品 / 架构 / 研发 / 质控 / 变更管控），
各角色用不同大模型协作与互相质疑；用户只通过自建的 Web/API 网关对接 CEO，其余全自动。

## 设计文档

- **[01-最终设计方案.md](01-最终设计方案.md)**：完整架构设计，包含五条底层原则、三层隔离模型、13 个角色权限表、需求双层结构、设计委员会多模型质疑机制、先设计再执行的双层硬拦、90 轮问题处理、风险规避表。
- **[02-从零开始操作手册.md](02-从零开始操作手册.md)**：13 个阶段可逐条执行的操作手册。
- **[03-本地全流程部署与验证手册.md](03-本地全流程部署与验证手册.md)**：在真实 Ubuntu 主机上从零部署、跑通一个 demo 项目并验证六项端到端目标的 step-by-step（含目的/命令/预期输出/打勾、🧑‍💻 需你介入处、监测告警部署）。
- **[docs/hermes-验证矩阵.md](docs/hermes-验证矩阵.md)**：仓库用到的每个 Hermes 命令/配置键/API 行为的核验状态（✅ 已对官方文档确认 / ⚠️ 待真机核对）+ 来源，并列出两大真机不确定点（角色识别、task_id 可达）与已知设计限制。

## 仓库结构

```
platform/
  control_plane.py      FastAPI 控制平面（对外唯一入口，10 个 /api 端点）
  policy_plugin.py      第二层权限硬拦（pre_tool_call hook：no-code/QA/release/dev-worker 闸）
  launch_project.sh     项目启动器：建实例 + board + 17 个角色 profile + gateway
  orchestrator.py       状态机：产品→架构→dev→QA→release 全流程幂等编排（systemd timer 每分钟）
  watchdog.sh           异常续跑 + 熔断 + review 放行 + 限流暂停跳过（不做正常编排，归 orchestrator）
  monitor.sh            健康监测+告警：gateway/卡死/权限漂移/日志/磁盘/Docker属主/余额/限流/策略降级
  hook_canary.sh        每小时探测设计闸门 hook 是否仍在 kanban-worker 路径生效
platform-base/
  templates/            AGENTS.md（全局约束）+ 各角色 SOUL.*.md + requirements.yaml 模板
docker/
  python-sandbox.Dockerfile  非 root 沙箱镜像（映射宿主 UID，产物属主正确）
scripts/
  00-host-setup.sh      宿主机准备（资源预检 / Docker / Hermes）
  01-deploy-platform.sh 部署 + 建沙箱镜像 + 装控制平面 + 装自动化三循环 systemd 定时器
  check_docs.py         校验手册内嵌代码块语法（CI 用）
tests/
  test_policy_plugin.py    权限各闸单测
  test_control_plane.py    控制平面端点单测（TestClient + 注入式 FakeGateway）
  test_static_contracts.py 静态契约：挡住 --workers/config get/占位模型 等回归
docs/                   验证矩阵 + 综合修正方案 + 优化方案
```

## 快速开始

> ⚠️ 真正运行需要一台 **Ubuntu 22.04+** 服务器，装好 **Hermes Agent v0.16 + Docker + PostgreSQL + Redis**。
> 本仓库的 Python/脚本可在任意机器上测试，但端到端跑通必须在上述环境，详见操作手册。

```bash
# 1) 宿主机准备（依赖 + Docker + Hermes）
./scripts/00-host-setup.sh
hermes setup --portal          # 配模型供应商；key 写入 ~/.hermes/.env（GLM_API_KEY/DEEPSEEK_API_KEY）

# 2) 部署：铺文件 + 建沙箱镜像 + 把控制平面装成 systemd 服务并启动（127.0.0.1:9000）
./scripts/01-deploy-platform.sh
systemctl --user status autocode-control-plane.service   # 确认 active

# 3) 创建第一个项目（注意 Content-Type，否则 422）
TOKEN="$(cat ~/platform/.platform_token)"
curl -s -X POST http://127.0.0.1:9000/api/projects \
  -H "X-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"project_id":"demo1"}'
```
> 完整 step-by-step（含与 CEO 沟通、确认方案、设计/构建、六项端到端验证、监测告警）见《03-本地全流程部署与验证手册.md》。

## 开发与测试

```bash
make dev-install   # 安装 fastapi/httpx/pytest 等
make test          # 运行单元测试（含角色识别、设计闸门、端口持久化等）
make lint-sh       # bash 语法检查
make check-docs    # 校验手册里嵌入的代码块（防文档/代码漂移）
make ci            # 一键跑全套（= GitHub Actions 所做）
```

每次 push / PR（也可在 Actions 页 `workflow_dispatch` 手动触发）由 **GitHub Actions**
（`.github/workflows/ci.yml`）自动跑 `pytest` + `bash -n` + `shellcheck` +
手册嵌入代码块校验。`scripts/check_docs.py` 会把《02-操作手册》里 heredoc 内嵌的
policy_plugin / control_plane / launcher / watchdog 抽出来编译，**任何文档与代码的语法漂移都会让 CI 变红**。

## 对外 API（控制平面，对应设计方案 §8）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/projects` | 创建项目（建实例 + board + profiles + gateway） |
| POST | `/api/projects/{id}/messages` | 与 CEO 多轮沟通需求 |
| POST | `/api/projects/{id}/confirm-plan` | 落盘需求（请求体 `requirements`）→ 启动产品委员会 swarm |
| POST | `/api/projects/{id}/architecture-swarm` | PRD 产出后显式起架构委员会 swarm（watchdog 也会自动兜底） |
| POST | `/api/projects/{id}/change-requests` | 提交变更（建 change-guardian 卡，触发设计闸门） |
| GET | `/api/projects/{id}/tasks` | 读 Kanban 看板 |
| GET | `/api/projects/{id}/requirements` | 读需求双层结构 |
| GET | `/api/projects/{id}/artifacts` | 阶段成果产物清单 |
| GET | `/api/projects/{id}/demo` | 阶段性可运行成果 |
| GET | `/api/projects/{id}/events` | SSE 事件流（当前为一次性看板快照，持续推送属阶段 13） |

**安全红线**：Hermes 的 `/v1/*` 与 dashboard 绝不直接对外（直连等于暴露 terminal/file/web 全工具）；
对外鉴权全在本网关，且只绑 localhost，生产环境前面架 nginx/Caddy 做 TLS。

## 技术栈

Hermes Agent v0.16 · Kanban multi-agent · FastAPI · Docker · PostgreSQL · Redis
