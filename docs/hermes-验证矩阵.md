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
| `hermes kanban swarm "<goal>" --worker PROFILE:TITLE --verifier c --synthesizer d` | ✅ | **真机实测修正**：单数、可重复 `--worker`（格式 `PROFILE:TITLE`）；复数 `--workers` 不存在（NEW-E）|
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
| 自定义 base HERMES_HOME 下角色识别（dispatcher 设 worker HERMES_HOME 到 `profiles/<role>`） | ✅ | **真机已证实**（round2 §5a）：设计闸门按角色正确拦截，说明 `resolve_role` 在真机生效 |
| **pre_tool_call hook 是否拿到 kanban `task_id`** | ❌(已缓解+恢复) | **真机确认（round2 §5b）：Hermes 不经 `HERMES_KANBAN_TASK`/kwargs 传 task_id**。但 dev-worker 用 `worktree:<task_id>` workspace，其**进程 cwd 末段目录名即 `t_xxx`**——`resolve_task_id()` 在 kwargs/env 之后**新增从 cwd（`TERMINAL_CWD`/`PWD`/`getcwd`）反解**（评审 A），匹配 `^t_[A-Za-z0-9_-]{4,}$`，让第三道闸（task 级 allowed_paths）**在 worktree 模式下恢复生效**。反解失败才走 #2 项目级降级兜底，并落 `reports/security/policy_fallback.jsonl`（评审 B，monitor 告警）；`POLICY_REQUIRE_TASK_ID=1` 可强制严格 |

## C. 配置键
| 键 | 状态 | 来源 / 备注 |
|---|---|---|
| `model.default` | ✅ | configuration.md |
| `model.provider` / `model.base_url`（跨供应商路由） | ✅ | **真机实测**：不设则错误模型名被发给默认供应商而失败；设了 zai/deepseek 各自路由成功 |
| `agent.max_turns`（默认 90） | ✅ | configuration.md 逐字 |
| `terminal.cwd` | ✅ | configuration.md |
| `terminal.backend`（值 `docker` 合法） | ✅ | configuration.md（六种 backend） |
| `terminal.docker_image` | ✅ | configuration.md |
| `terminal.docker_volumes` 必须是 **YAML 列表** | ✅ | **真机修正（Bug-1）**：`config set` 写它会存成 YAML **字符串标量** → DockerEnvironment 当非 list 静默丢弃 → 容器无卷挂载 → runc 报错。须**直接写 config.yaml 为 YAML 列表**（pyyaml）|
| `agent.disabled_toolsets`（denylist，跨 CLI+gateway） | ✅ | configuration.md（第一层权限的确定保证） |
| `config set` 写入 `profiles/<name>/config.yaml` | ✅ | 真机日志逐行确认（`✓ Set ... in .../config.yaml`） |
| ⚠️ `config show` **不渲染** `disabled_toolsets` 等字段 | ✅ | **真机实测**：校验权限要直接读 `config.yaml`，不能靠 `config show \| grep` |
| `approvals.mode`（manual 默认 / off 无人值守） | ✅ | **真机实测**：默认 manual 会反复弹审批中断自动化；隔离架构下设 off 安全（见 §G） |
| `toolsets`（**附加列表，非白名单**）| ✅ | **真机实测修正**：内置 `code_execution/terminal/file` 始终可用，必须用 `agent.disabled_toolsets` 显式禁用；值用 JSON 数组（NEW-K/L）|

## D. Hooks / 插件
| 用法 | 状态 | 来源 / 备注 |
|---|---|---|
| `pre_tool_call` 可在工具执行前 block | ✅ | hooks.md（需 `HERMES_ACCEPT_HOOKS=1` 在非交互/gateway 路径接受 hook）|
| 插件复制到 `plugins/<name>/` 后**必须** `hermes plugins enable <name>` 才加载 | ✅ | **真机实测（NEW-O）**：仅复制不 enable → hook 整条流水线缺席 |
| ⚠️ 校验插件勿用 `hermes plugins list \| grep -q`（pipefail+SIGPIPE 误判） | ✅ | **真机 P0**：grep -q 提前关管道致 hermes 退 141，pipefail 下误判未启用→建项目失败。改查 `plugins enable` 自身输出 |
| Python 插件 `register(ctx)` + `ctx.register_hook(...)` | ✅ | hooks.md（Python 先注册，优先于 shell hook） |
| **pre_tool_call 在 kanban-worker 路径触发** | ✅ | **真机已证实**（round2 §5a）：dev-worker 在 Docker + yolo 下写码被设计闸门拦截 → Python 插件 hook **确实触发**（#25204 对本平台不成立）。hook_canary.sh 持续兜底监测 |
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

## F. 已知设计限制
- **全流程编排**：✅ 已用 `orchestrator.py` 状态机闭合——产品→架构→dev→QA→release 按 文件信号 + Kanban 状态幂等推进（部署装 `autocode-orchestrator.timer` 每分钟 tick；状态存 `workspace/.autocode/state.json`）。起 release 需本轮 `qa_started`，挡残留旧 `status.json` 误触发（评审 E）；手动 `architecture-swarm` 端点与状态机共享 `arch_started` 标记，幂等不双触发（评审 D）。watchdog 退回只管异常续跑，且限流暂停期内不起新续跑卡（评审 C）。完整**事件驱动**（监听卡 done 事件而非轮询）仍属后续（需 Redis 事件总线）。
- **dev-worker 并行真隔离（worktree）**：✅ 三重保障——① 沙箱镜像装 git（`docker/python-sandbox.Dockerfile`，否则容器内 `git worktree/commit/merge` 物理不可用）；② workspace git 提交身份持久化到 repo config + 镜像内 `git config` 兜底身份 + `safe.directory '*'`（否则容器内 commit 报"who are you"/dubious ownership 失败）；③ SOUL.dev-lead 强约束"每张编码卡必须 `--workspace worktree:${WORKTREE_ROOT}/<短名>` + allowed_paths"，dev-worker 完工 `git commit`，`terminal.env` 透传 WORKTREE_ROOT/GIT_REPO。monitor `check_dev_commits` 兜底观测"卡 done 但无提交"。
- **交付完整性闸门**：✅ `qa_integrity.py` + orchestrator 起 release 前独立硬闸 `min_release_ok`（dev 卡 done 却只有 init 提交/无源码落地 → 拦，建复验卡，不信任 agent 汇报）+ policy QA gate 校验 `status.json.integrity`（git 脏/缺文件/留 TODO 占位 → 拦）。挡"看板 done 但代码没落地"（DEV-4 安全码丢失类）。
- **无人值守模式**：✅ 部署按 `AUTOCODE_MODE`（production/unattended/demo）写 `.platform_runtime.env`，timers 读取；unattended 默认 `AUTOCODE_AUTO_APPROVE_REVIEW=1`，安全靠 QA gate + 设计闸门 + 完整性闸门三重兜底而非人工。
- **限流暂停**：⚠️ 单一 `.provider_pause` 全局熔断，时长可调（`PROVIDER_PAUSE_SECONDS`）。按供应商分目录暂停 / 探测恢复属后续（避免探测自身触发限流 + 隔离复杂度）。
- **systemd `gateway run`**：✅ 真机实测，`Type=simple` 正确（见 §E）。
- **SSE `/events` 为一次性快照**：持续推送属阶段 13（Redis 事件总线）。

## G. 审批与无人值守安全模型
平台目标是**无人值守全自动**——没人盯着审批。Hermes 的命令审批 `approvals.mode` 默认 `manual`，会反复弹"command approval required"打断流程。本平台用**一个显式开关 `AUTOCODE_UNATTENDED`**（默认 1）统一控制：为 1 时本项目 `approvals.mode=off` + gateway 单元 `HERMES_YOLO_MODE=1`；为 0 时保留人工审批。审批配置只写**本项目 HERMES_HOME**，不动用户主配置。

**为何不降低实际安全**（真机报告分析，Hermes 三层安全模型）：
- **第 1 层 HARDLINE（12 条，不可绕过）**：`rm -rf /`、`mkfs`、`dd` 写裸设备、fork bomb、`shutdown` 等 + sudo-stdin guard——**off/yolo 也拦得住**。
- 第 2 层 DANGEROUS（61 条）、第 3 层 Tirith（~80 条）：off 跳过。
- 但**触发面已被架构消除**：CEO 无终端（`disabled_toolsets` 含 code_execution/terminal）；dev-worker 在 Docker（源码级豁免 approval.py，approval 本就不作用于容器内）；gateway 只跑 `hermes kanban`。
> 结论：安全靠**架构隔离 + 设计闸门 + 监测告警**实现，不靠人工审批拦截。生产若需更严，把 `HERMES_APPROVALS_MODE=smart` 并确保 worker 全在 Docker。
