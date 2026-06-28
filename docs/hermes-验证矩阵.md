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
| `approvals.cron_mode`（**默认 deny**！approve=非交互放行） | ✅ | **真机实测（D25）**：worker 由 gateway 内嵌 dispatcher 定时派发=非交互路径，**只设 mode=off 仍被 cron_mode=deny 拦**。无人值守须一并设 `approve`（launch_project 已自动设） |
| `toolsets`（**附加列表，非白名单**）| ✅ | **真机实测修正**：内置 `code_execution/execute_code/terminal/file` 始终可用，必须用 `agent.disabled_toolsets` 显式禁用；值用 JSON 数组（NEW-K/L）。**必含 `execute_code`**——真机 shi：只禁 `code_execution` 时 CEO 仍能用 `execute_code` 越权写码 |

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
- **全流程编排**：✅ 已用 `orchestrator.py` 状态机闭合——产品→架构→dev→QA→release 按 文件信号 + Kanban 状态幂等推进。**双跑保障可靠**：除 `autocode-orchestrator.timer` 外，orchestrator 还**内嵌进控制平面常驻进程**（lifespan，部署 `AUTOCODE_EMBEDDED_ORCHESTRATOR=1`），跨进程 `tick_lock`（`.orchestrator.lock`）与 timer 互斥、绝不双跑——修真机 shi"systemd timer 重启后失效、流水线全靠人工 tick"。起 release 需本轮 `qa_started`；手动 `architecture-swarm` 端点共享 `arch_started` 幂等。**自愈不放宽闸门**：ADR 出但缺 canonical `approved_versions.txt` → 建补齐卡；QA 卡 done 但缺 `status.json` → 建 QA 补齐卡；PRD/ADR 容忍非 canonical 文件名但落 `.autocode/warnings.jsonl`。**complete 收口**：要求 `reports/release/manifest.json` 存在（缺则建补齐卡），并记 `completion_mode=natural`。完整事件驱动仍属后续（Redis）。
- **跨项目 Docker 挂载隔离**：✅ executor 注入 `terminal.env.AUTOCODE_PROJECT_ID/WORKSPACE`；monitor `check_docker_mount_isolation` 核对"容器声明项目 == 实际挂载 workspace"，不一致即 CRIT（修真机 shi：Hermes 跨项目复用容器，shi 的 worker 挂到 demo2 的 P0 漏洞）。验收脚本同检。
- **dev-worker 并行真隔离（worktree）**：✅ 三重保障——① 沙箱镜像装 git（`docker/python-sandbox.Dockerfile`，否则容器内 `git worktree/commit/merge` 物理不可用）；② workspace git 提交身份持久化到 repo config + 镜像内 `git config` 兜底身份 + `safe.directory '*'`（否则容器内 commit 报"who are you"/dubious ownership 失败）；③ SOUL.dev-lead 强约束"每张编码卡必须 `--workspace worktree:${WORKTREE_ROOT}/<短名>` + allowed_paths"，dev-worker 完工 `git commit`，`terminal.env` 透传 WORKTREE_ROOT/GIT_REPO。monitor `check_dev_commits` 兜底观测"卡 done 但无提交"。
- **交付完整性闸门**：✅ `qa_integrity.py` + orchestrator 起 release 前独立硬闸 `min_release_ok`（dev 卡 done 却只有 init 提交/无源码落地 → 拦，建复验卡，不信任 agent 汇报）+ policy QA gate 校验 `status.json.integrity`（git 脏/缺文件/留 TODO 占位 → 拦）。挡"看板 done 但代码没落地"（DEV-4 安全码丢失类）。
- **无人值守模式**：✅ 部署按 `AUTOCODE_MODE`（production/unattended/demo）写 `.platform_runtime.env`，timers 读取；unattended 默认 `AUTOCODE_AUTO_APPROVE_REVIEW=1`，安全靠 QA gate + 设计闸门 + 完整性闸门三重兜底而非人工。
- **限流暂停（可自愈）**：✅ monitor 用 `journalctl --since` 时间窗 + 1305 日志指纹去重；watchdog 每分钟清过期 `.provider_pause`，限流一恢复即解除。**区分临时/永久**：1305 临时过载写 `.provider_pause`（到期自愈）；**1113 余额耗尽**写 `.provider_billing_dead`（永久，orchestrator/watchdog 不再起新任务/续跑，充值后人工 rm）——避免对永久故障无限重试（D13/D19）。`check-models.sh` 也解析 429 body 区分两者。
- **异常恢复按 status（D14）**：✅ 真机实测 `kanban list --json` 的 `last_event` 字段为空（事件只进 task_events 表）。watchdog/monitor 改按可靠的 **`status`**（blocked/failed）检测异常，并**按 reason 分类**（余额→不续跑；环境/挂载→建排查卡；其余→续跑），不盲目 unblock。修"续跑/告警因空字段静默失效"。
- **worker Docker 后端可靠（D26）**：✅ 根治——gateway/控制平面单元 `SupplementaryGroups=docker`，让 worker 子进程继承 docker 组（systemd user service 默认不带用户补充组，导致 worker 调 docker 失败全卡死）。建项目期 `AUTOCODE_EXECUTOR_BACKEND=auto` 探测运行上下文能否用 docker，不行就**拒绝建项目**（绝不静默回退 local 牺牲隔离；显式 `AUTOCODE_ALLOW_LOCAL_EXECUTOR=1` 才降级并标 degraded）。启动 gateway 前 `_verify_worker_profiles` 校验 toolsets/disabled/backend（D29，把 worker 配置问题提前到建项目期暴露）。
- **状态机 direct-to-QA（D30）**：✅ dev-worker 卡全 done（正常）或 dev-lead 卡 done + 已有真实源码（串行/预置代码）→ 进 QA，靠 `expected_files_present` 防空手放行、release 前仍走完整性硬闸。修"无 fan-out 卡时卡死 dev→qa"。
- **terminal 绕过 allowed_paths**：✅ dev-worker 有 terminal，可用 shell 绕开 write_file hook；`scope_guard.py` 对每个 worktree 的 git diff 做**提交级范围审计**（越界文件即拦），由 `qa_integrity`/orchestrator 在宿主侧独立跑，作为 release 前硬闸——隔离不只靠 pre-tool hook。
- **task_id 可靠绑定**：✅ worktree 根写 `.autocode_task_id` 标记，`resolve_task_id` 先读标记再退路径反解；真机 v0.17 设计闸门三命门（拦/放/task_id 可达）全过。
- **交付完整性在沙箱可达**：✅ `qa_integrity.py`/`scope_guard.py` 复制进 `${WORKSPACE}/.autocode/tools/`（容器挂载 WORKSPACE 即可见），SOUL.qa 调 `.autocode/tools/qa_integrity.py`；宿主侧 `min_release_ok` 不信任容器内可改写的输出，独立复核。
- **用户面 Web UI**：✅ 控制平面 `GET /`（webui.html，CSP 安全头）+ 只读端点（list/state/artifact-content 白名单防穿越/conversation 读平台 JSONL）；前端转义渲染 + sessionStorage，防存储型 XSS。
- **Hermes 版本**：核验于 v0.16；**v0.17.0 已真机验证向后兼容**（命令/配置键/API 未变）。
- **systemd `gateway run`**：✅ 真机实测，`Type=simple` 正确（见 §E）。
- **SSE `/events` 为一次性快照**：持续推送属阶段 13（Redis 事件总线）。

## G. 审批与无人值守安全模型
平台目标是**无人值守全自动**——没人盯着审批。Hermes 的命令审批 `approvals.mode` 默认 `manual`，会反复弹"command approval required"打断流程。本平台用**一个显式开关 `AUTOCODE_UNATTENDED`**（默认 1）统一控制：为 1 时本项目 `approvals.mode=off`；为 0 时保留人工审批。审批配置只写**本项目 HERMES_HOME**，不动用户主配置。
> **命令审批的两个维度（缺一就卡自动化）**：`launch_project.sh` 在建项目时**自动**写好（只写本项目 HERMES_HOME，不动用户主配置）：
> - `approvals.mode=off`（交互/普通路径放行，默认 manual 会反复弹审批）；
> - `approvals.cron_mode=approve`（**非交互/定时路径放行，默认 deny**）——本平台 worker 由 gateway 内嵌 dispatcher 每 60s 定时派发，属非交互路径，**只设 mode=off 仍会被 cron_mode 拦死**（真机 D25）。
> 二者都受 `AUTOCODE_UNATTENDED`（默认 1）控制；可用 `HERMES_APPROVALS_MODE`/`HERMES_APPROVALS_CRON_MODE` 覆盖。`AUTOCODE_UNATTENDED=0` 时退回 manual+deny（保留人工审批）。
> **不依赖 YOLO**：`HERMES_YOLO_MODE` 默认 **0**——yolo 有绕过 pre_tool_call hook（第二层设计闸门）的风险，安全策略不应依赖它；无人值守靠上面两个 approvals 即可，`HERMES_ACCEPT_HOOKS=1` 始终开。

**为何不降低实际安全**（真机报告分析，Hermes 三层安全模型）：
- **第 1 层 HARDLINE（12 条，不可绕过）**：`rm -rf /`、`mkfs`、`dd` 写裸设备、fork bomb、`shutdown` 等 + sudo-stdin guard——**off/yolo 也拦得住**。
- 第 2 层 DANGEROUS（61 条）、第 3 层 Tirith（~80 条）：off 跳过。
- 但**触发面已被架构消除**：CEO 无终端/无代码工具（`disabled_toolsets` 含 code_execution/**execute_code**/terminal/file）；dev-worker 在 Docker（源码级豁免 approval.py，approval 本就不作用于容器内）；gateway 只跑 `hermes kanban`。
> 结论：无人值守下，**普通 + DANGEROUS + Tirith 命令全部自动放行**（不再弹审批、不阻塞流水线）；**唯一不自动放行的是 HARDLINE 12 条灾难性命令**（`rm -rf /`/`mkfs`/fork bomb 等）——这是有意保留的不可绕过红线，且本平台角色（CEO 无终端、worker 在受限 Docker）**正常工作根本不会触发它**。安全靠 架构隔离 + 设计闸门 + 监测告警 实现，不靠人工审批拦截。生产若需更严，把 `HERMES_APPROVALS_MODE=smart` 并确保 worker 全在 Docker。
