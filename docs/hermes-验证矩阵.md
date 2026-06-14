# Hermes 命令 / 配置键 · 验证矩阵

> 本表记录仓库用到的每一个 Hermes 命令、子命令、配置键、API 行为的**核验状态**与来源。
> 来源均为 NousResearch/hermes-agent 官方文档或 issue（核验于 2026-06）。
> ✅ = 官方文档/示例已确认　·　⚠️ = 已使用但尚未在官方文档逐字确认（需真机 `--help` 核对）

## A. Kanban CLI
| 用法 | 状态 | 来源 / 备注 |
|---|---|---|
| `hermes kanban boards create <slug> --name --description --icon --switch` | ✅ | kanban.md 官方示例 |
| `hermes kanban create "<t>" --assignee <p> --json` | ✅ | kanban.md 官方示例 |
| `--idempotency-key` | ✅ | kanban.md 官方示例（watchdog/canary 用它去重） |
| `hermes kanban swarm "<goal>" --workers a,b --verifier c --synthesizer d` | ✅ | kanban.md：建 root/blackboard + N worker + gated verifier + gated synthesizer |
| workspace 种类 `scratch / worktree:<p> / dir:<p>` | ✅ | kanban.md |
| 每 board 独立 `kanban.db` + dispatcher 设 `HERMES_KANBAN_BOARD` | ✅ | kanban.md |
| dispatcher 内嵌 gateway、60s tick | ✅ | kanban.md |
| `--goal` / `--goal-max-turns N`（goal-mode 长任务） | ⚠️ | goal-mode 概念已确认；这两个具体 flag 名需 `hermes kanban create --help` 核对 |
| `--body` / `--parent` / `--workspace` / `list --status` flag | ⚠️ | 任务依赖官方靠 `kanban_link`；这些 CLI flag 需核对 |
| `kanban.dispatch_in_gateway` / `kanban.max_in_progress` / `kanban.failure_limit` 配置键 | ⚠️ | 行为已确认，**具体键名待核对** |

## B. Profiles / 角色识别
| 用法 | 状态 | 来源 / 备注 |
|---|---|---|
| `hermes profile create <name> --description "<role>"` | ✅ | profiles.md（--description 供编排路由） |
| `hermes -p <name>` / `--profile` | ✅ | profiles.md |
| profile = 独立 HERMES_HOME（`…/.hermes/profiles/<name>`） | ✅ | profiles.md（运行某 profile 时 HERMES_HOME 设到该子目录） |
| profile **不是** 沙箱（local backend 有完整文件权限） | ✅ | profiles.md |
| 不存在 `HERMES_PROFILE` 环境变量 | ✅ | profiles.md（当前 profile 靠 HERMES_HOME 体现） |
| **自定义 base HERMES_HOME 下，dispatcher 是否把 worker HERMES_HOME 设到 `<base>/.hermes/profiles/<role>`** | ⚠️ | **平台最大不确定点**；由《03》Step 8 第 5 项实测；不成立时 fail-closed 兜底 |

## C. 配置键
| 键 | 状态 | 来源 / 备注 |
|---|---|---|
| `model.default` | ✅ | configuration.md |
| `agent.max_turns`（默认 90） | ✅ | configuration.md 逐字 |
| `terminal.cwd` | ✅ | configuration.md |
| `terminal.backend`（值 `docker` 合法） | ✅ | configuration.md（六种 backend） |
| `terminal.docker_image` | ✅ | configuration.md |
| `terminal.docker_volumes`（docker `-v` 语法） | ✅ | configuration.md（**曾误用 docker_mounts，已修**） |
| `agent.disabled_toolsets`（denylist，跨 CLI+gateway） | ✅ | configuration.md（第一层权限的确定保证） |
| `toolsets` allowlist（`config set toolsets "a,b"`） | ⚠️ | 逗号集已确认；**allowlist 这个键本身待核对**，故已加 disabled_toolsets 兜底 |

## D. Hooks / 插件
| 用法 | 状态 | 来源 / 备注 |
|---|---|---|
| `pre_tool_call` 可在工具执行前 block | ✅ | hooks.md |
| Python 插件 `register(ctx)` + `ctx.register_hook(...)` | ✅ | hooks.md（Python 先注册，优先于 shell hook） |
| **pre_tool_call 在 kanban-worker 路径是否可靠触发** | ⚠️🔴 | **issue #25204**：shell pre_tool_call 在 kanban-worker `chat -q` 不可靠（v0.13）；我们用 Python 插件更稳，但必须实测（Step 8-5 + hook_canary.sh 持续监测） |
| 相关已知 bug | — | #2817（部分 hook 文档有却不触发）、#12922（post_tool_call 不覆盖内置工具） |

## E. API Server / Gateway
| 用法 | 状态 | 来源 / 备注 |
|---|---|---|
| OpenAI 兼容 `/v1/chat/completions`、`/v1/responses` | ✅ | api-server.md |
| `X-Hermes-Session-Key` 做服务端会话 scope（多轮上下文） | ✅ | api-server.md（threaded 到 memory provider）——**"多轮会丢"担忧基本被否** |
| `"model":"ceo"` 路由 | ✅(N/A) | model 字段被接受但实际模型走服务端配置；**定向靠"打该 profile 的 gateway 端口"**，我们正是这么做 |
| `API_SERVER_ENABLED` / `API_SERVER_KEY`、默认端口 8642 | ✅ | api-server.md / issue #39365 |
| `API_SERVER_PORT` 环境变量名 | ⚠️ | 端口可配已确认，确切 env 名待核对 |
| `hermes gateway install/start/status`、`--all` | ✅ | cli-commands.md（systemd/launchd） |
| `gateway install` 的单元是**单个机器级 `hermes-gateway.service`** | ✅ | cli-commands.md；故我们改用每项目唯一命名 user 单元，隔离性待真机确认 |

## F. 已知设计限制（reviewer 提出且成立，留作后续）
- **架构委员会 swarm 未由代码触发**：`confirm-plan` 只显式建**产品**委员会 swarm；架构 swarm 目前依赖 dispatcher/change-guardian 衔接，未做"PRD 完成→自动起架构 swarm"的门控编排。正解：监听产品 synthesizer 卡完成事件后由控制平面起架构 swarm（需事件流，属阶段 13）。
- **SSE `/events` 为一次性快照**：持续推送属阶段 13（Redis 事件总线）。
- **systemd `Type=simple` vs `gateway start` 是否自后台化**：未知；若自 daemon 化需改 `Type=forking`，已在脚本注释标注，待真机核对。
