"""静态契约测试 —— 在没有真实 Hermes 的 CI 里挡住本次真机部署踩到的几类硬错误。

`FakeGateway` 把 CLI 调用都 mock 掉了，CLI 漂移零覆盖（这正是"单测全绿却部署崩溃"
的根因）。这些断言直接读源码文本，确保危险写法不再回归。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# --- NEW-E：swarm 必须用单数 --worker，绝不复数 --workers ----------------------
def test_control_plane_uses_singular_worker_flag():
    text = read("platform/control_plane.py")
    assert '"--worker"' in text
    assert '"--workers"' not in text


def test_manuals_have_no_plural_workers_flag():
    for path in ["01-最终设计方案.md", "02-从零开始操作手册.md",
                 "03-本地全流程部署与验证手册.md"]:
        assert "--workers" not in read(path), path


# --- 模型配置：必须设 provider/base_url，且不得有占位模型名 --------------------
def test_launcher_sets_provider_and_base_url():
    text = read("platform/launch_project.sh")
    assert "model.provider" in text and "model.base_url" in text


def test_launcher_has_no_placeholder_models():
    for path in ["platform/launch_project.sh", "02-从零开始操作手册.md"]:
        text = read(path)
        for bad in ("anthropic/claude", "openai/gpt-5.1", "google/gemini"):
            assert bad not in text, f"{path}: {bad}"


# --- NEW-O / hooks：必须 enable 插件并接受 hook ------------------------------
def test_launcher_enables_policy_and_accepts_hooks():
    text = read("platform/launch_project.sh")
    assert "plugins enable policy" in text
    assert "HERMES_ACCEPT_HOOKS" in text


# --- NEW-I：systemd 用 gateway run，不用 gateway start ------------------------
def test_launcher_uses_gateway_run():
    text = read("platform/launch_project.sh")
    assert "-p ceo gateway run" in text
    assert "-p ceo gateway start" not in text   # 实际命令，不含解释性注释里的提及


# --- NEW-F：monitor 不得用不存在的 config get -------------------------------
def test_monitor_does_not_use_config_get():
    assert "config get" not in read("platform/monitor.sh")


# --- 真机 P0：插件校验不得用 `plugins list | grep -q`（pipefail+SIGPIPE 误判）---
def test_launcher_no_plugins_list_grep_q_antipattern():
    text = read("platform/launch_project.sh")
    assert "plugins list | grep" not in text
    assert "plugins list 2>/dev/null | grep" not in text


# --- 无人值守：受控开关 AUTOCODE_UNATTENDED 控制 approvals + YOLO -------------
def test_launcher_sets_approvals_and_yolo():
    text = read("platform/launch_project.sh")
    assert "approvals.mode" in text
    # 命令审批两个维度都要设：cron_mode 默认 deny，只设 mode 仍会卡非交互/定时派发的 worker（D25）
    assert "approvals.cron_mode" in text and "approve" in text
    assert "HERMES_YOLO_MODE" in text
    assert "AUTOCODE_UNATTENDED" in text


# --- 沙箱镜像不得静默回退公共 root 镜像（破坏隔离安全模型）-------------------
def test_launcher_sandbox_fallback_is_gated():
    text = read("platform/launch_project.sh")
    assert "ALLOW_PUBLIC_SANDBOX_FALLBACK" in text


def test_deploy_fails_on_sandbox_build_failure():
    text = read("scripts/01-deploy-platform.sh")
    assert "ALLOW_PUBLIC_SANDBOX_FALLBACK" in text


# --- 自动化加固：续跑熔断 / 供应商暂停 / 磁盘硬阈值 / 设计闸门降级 --------------
def test_watchdog_has_continuation_cap():
    assert "MAX_CONTINUATIONS" in read("platform/watchdog.sh")


def test_orchestrator_respects_provider_pause():
    assert "provider_paused" in read("platform/orchestrator.py")


def test_launcher_has_disk_hard_threshold():
    text = read("platform/launch_project.sh")
    assert "AUTOCODE_ALLOW_LOW_DISK" in text
    # 阈值用 MB 粒度，默认 100MB（df -BG 无法表达百兆级）；保留 GB 兼容
    assert "AUTOCODE_MIN_DISK_MB" in text and "df -BM" in text
    assert 'AUTOCODE_MIN_DISK_MB:-100' in text


def test_policy_has_taskless_fallback_switch():
    text = read("platform/policy_plugin.py")
    assert "POLICY_REQUIRE_TASK_ID" in text


def test_watchdog_has_optional_review_auto_approve():
    text = read("platform/watchdog.sh")
    assert "AUTOCODE_AUTO_APPROVE_REVIEW" in text


# --- 权限校验读 config.yaml，不靠不渲染该字段的 config show -------------------
def test_monitor_reads_config_yaml_for_permission_check():
    text = read("platform/monitor.sh")
    assert "config.yaml" in text


# --- 配置写法：disabled_toolsets 用 JSON 数组；docker_volumes 用 YAML 列表写入 -----
def test_launcher_config_values_correct():
    text = read("platform/launch_project.sh")
    assert "disabled_toolsets '[\"code_execution" in text  # 含 code_execution
    # Bug-1：docker_volumes 不能用 config set（会存成字符串标量被丢弃），改 YAML 列表写入
    assert "set_docker_volumes" in text
    assert 'config set terminal.docker_volumes' not in text


def test_launcher_git_inits_workspace():
    assert "git -C" in read("platform/launch_project.sh") and "init" in read("platform/launch_project.sh")


# --- 评审 C：watchdog 也要在供应商限流暂停期内停起新续跑卡（与 orchestrator 一致）------
def test_watchdog_respects_provider_pause():
    text = read("platform/watchdog.sh")
    assert "provider_paused" in text and ".provider_pause" in text


# --- 评审 B：monitor 监测策略闸门 taskless 降级（细粒度隔离退化可观测）------------
def test_monitor_checks_policy_fallback():
    text = read("platform/monitor.sh")
    assert "check_policy_fallback" in text and "policy_fallback.jsonl" in text


# --- 评审 B：policy_plugin 走兜底时落 JSONL，且尝试从 worktree cwd 取 task_id -------
def test_policy_logs_fallback_and_reads_worktree_taskid():
    text = read("platform/policy_plugin.py")
    assert "policy_fallback.jsonl" in text
    assert "missing_task_allowed_paths" in text


# --- 评审 E：orchestrator 起 release 需本轮 qa_started，挡残留旧 status.json 误触发 ---
def test_orchestrator_release_requires_qa_started():
    text = read("platform/orchestrator.py")
    assert 'state.get("qa_started") and qa_status.get("release_allowed") is True' in text


# --- 评审 D：architecture-swarm 端点幂等（共享 arch_started）-----------------------
def test_architecture_swarm_endpoint_is_idempotent():
    text = read("platform/control_plane.py")
    assert "architecture-swarm-already-started" in text
    assert 'state.get("arch_started")' in text


# --- 评审 G：部署脚本要把自动化循环装成 systemd 定时器（否则状态机不跑）------------
def test_deploy_installs_automation_timers():
    text = read("scripts/01-deploy-platform.sh")
    for unit in ("autocode-orchestrator", "autocode-watchdog", "autocode-monitor"):
        assert unit in text, unit
    assert ".timer" in text


# --- 评审 I：CI 跑 shellcheck，并允许手动触发 ------------------------------------
def test_ci_runs_shellcheck_and_dispatch():
    text = read(".github/workflows/ci.yml")
    assert "shellcheck" in text and "workflow_dispatch" in text


# --- 第四轮 P0：沙箱镜像必须装 git（否则 worktree/分支合并全失效）-----------------
def test_python_sandbox_installs_git():
    text = read("docker/python-sandbox.Dockerfile")
    assert "apt-get install" in text and "git" in text
    # 容器内 commit 需身份 + 信任挂载目录
    assert "user.email" in text and "safe.directory" in text


# --- 第四轮 P0：workspace git 提交身份必须持久化到 repo config（否则容器内 commit 失败）---
def test_launcher_persists_git_identity():
    text = read("platform/launch_project.sh")
    assert "config user.email" in text and "config user.name" in text


# --- 第四轮 P0：部署构建后断言镜像内有 git ----------------------------------------
def test_deploy_asserts_sandbox_git():
    text = read("scripts/01-deploy-platform.sh")
    assert "git --version" in text


# --- 第四轮 P0：dev-worker 配 worktree 根 / 仓库路径透传进容器 ----------------------
def test_launcher_sets_worktree_env():
    text = read("platform/launch_project.sh")
    assert "WORKTREE_ROOT" in text and "GIT_REPO" in text


# --- 第四轮 P0：交付完整性闸门（orchestrator 起 release 前 + QA gate 双校验）---------
def test_orchestrator_has_integrity_gate():
    text = read("platform/orchestrator.py")
    assert "min_release_ok" in text and "qa_integrity" in text


def test_policy_qa_gate_checks_integrity_block():
    text = read("platform/policy_plugin.py")
    assert "integrity" in text and "todo_markers" in text


# --- 第四轮 P1：monitor 监测 dev 卡 done 但无提交（产物未落地可观测）---------------
def test_monitor_checks_dev_commits():
    text = read("platform/monitor.sh")
    assert "check_dev_commits" in text


# --- 第四轮 P1：部署按 AUTOCODE_MODE 写运行时 env（无人值守自动放行 review）---------
def test_deploy_writes_runtime_mode_env():
    text = read("scripts/01-deploy-platform.sh")
    assert "AUTOCODE_MODE" in text and "AUTOCODE_AUTO_APPROVE_REVIEW" in text
    assert ".platform_runtime.env" in text


# --- 第四轮 P1：host-setup 校验 Hermes installer 是脚本（拒绝 HTML 错页）-----------
def test_host_setup_validates_installer():
    text = read("scripts/00-host-setup.sh")
    assert "doctype html" in text.lower() and "HERMES_INSTALL_URL" in text


# --- 第四轮 P1：不推荐 chmod 666 docker.sock，改 newgrp/早失败 --------------------
def test_deploy_no_chmod_666_docker_sock():
    text = read("scripts/01-deploy-platform.sh")
    # 部署脚本应做 docker info 早失败 + newgrp 指引，而非建议 chmod 666
    assert "docker info" in text and "newgrp docker" in text


# --- 第五轮 P0-1：monitor 用时间窗，不用 -n 500（D12 无限暂停循环根因）+ 去重 ------
def test_monitor_journal_uses_time_window_not_tail():
    text = read("platform/monitor.sh")
    assert "--since" in text
    assert "-n 500 --no-pager" not in text   # journal 不再用 -n 500 扫历史
    assert ".last_1305_fp" in text           # 同一条限流日志只触发一次暂停（去重）


def test_watchdog_clears_expired_pause():
    text = read("platform/watchdog.sh")
    assert "clear_expired_pause" in text


# --- 第五轮 P0-2：qa_integrity 在沙箱内可达（复制进 .autocode/tools，SOUL 路径对齐）---
def test_qa_integrity_reachable_in_sandbox():
    launcher = read("platform/launch_project.sh")
    assert ".autocode/tools" in launcher and "qa_integrity.py" in launcher
    soul = read("platform-base/templates/SOUL.qa.md")
    assert ".autocode/tools/qa_integrity.py" in soul
    assert "${GIT_REPO}/../qa_integrity.py" not in soul   # 旧的不可达路径已移除


# --- 第五轮 P0-3：提交级范围审计堵 terminal 绕过 allowed_paths --------------------
def test_scope_guard_wired_into_integrity():
    assert "def scan" in read("platform/scope_guard.py")
    qi = read("platform/qa_integrity.py")
    assert "scope_guard" in qi and "scope_violations" in qi


# --- 第五轮 P0-4：task_id 用 .autocode_task_id 标记可靠绑定 worktree ----------------
def test_policy_reads_task_id_marker():
    assert ".autocode_task_id" in read("platform/policy_plugin.py")
    assert ".autocode_task_id" in read("platform-base/templates/SOUL.dev-lead.md")


# --- 第五轮 P0-2：workspace 平台内部目录 gitignore（不污染 dev 提交/scope 审计）-----
def test_launcher_gitignores_internal_dirs():
    text = read("platform/launch_project.sh")
    assert ".gitignore" in text and ".autocode/" in text


# --- 第五轮 P1：Web UI 只读端点 + 路径穿越用 is_relative_to + 白名单 ----------------
def test_control_plane_readonly_endpoints_safe():
    t = read("platform/control_plane.py")
    assert "def list_projects" in t and "def project_state" in t
    assert "def artifact_content" in t and "is_relative_to" in t
    assert "def all(" in t              # registry 公开接口，不碰私有 _projects
    assert "registry._projects" not in t.split("class ProjectRegistry")[1].split("def create_app")[0] \
        or "def list_projects" in t     # list_projects 用 registry.all()


def test_control_plane_conversation_no_hermes_sqlite():
    t = read("platform/control_plane.py")
    # 对话历史读平台自有 JSONL，不猜 Hermes 内部 response_store.db
    assert "conversations" in t and "response_store.db" not in t


# --- 第五轮 P1：webui.html 安全（转义渲染 / sessionStorage / 不 innerHTML 产物原文）---
def test_webui_is_xss_safe():
    html = read("platform/webui.html")
    # token 存 sessionStorage，不用 localStorage 持久化（按实际 API 调用判定，不看注释）
    assert "sessionStorage.setItem" in html
    assert "localStorage.setItem" not in html and "localStorage.getItem" not in html
    assert "textContent" in html
    # 不得把 API/产物内容塞进 innerHTML（产物用 textContent 原样展示）
    assert ".innerHTML = d.content" not in html and "innerHTML=md" not in html


def test_webui_can_send_to_ceo():
    # CEO 对话页必须能【发消息】，不只是只读（设计目标：用户经网关对接 CEO）
    html = read("platform/webui.html")
    assert "function sendMessage" in html
    assert '"POST"' in html and "/messages" in html


def test_control_plane_validates_session_id():
    t = read("platform/control_plane.py")
    assert "validate_session_id" in t and "SESSION_ID_RE" in t
    # 消息端点对 CEO 网关失败要回 502 而非泛化 500
    assert "CEO 网关无响应" in t


def test_ceo_soul_forbids_coding_and_routes_pipeline():
    soul = read("platform-base/templates/SOUL.ceo.md")
    # CEO 绝不自己交付代码、一切走流水线
    assert "绝不" in soul and "流水线" in soul
    # 新业务 → 新建独立项目（一个项目=一个核心需求）
    assert "新建一个独立项目" in soul and "core_need" in soul


def test_ceo_soul_finalizes_requirements_and_prompts_button():
    # 确认理解后：CEO 给出"定版需求"四块 + 引导用户点击启动按钮（与 webui 按钮文案一致）
    soul = read("platform-base/templates/SOUL.ceo.md")
    assert "定版需求" in soul
    assert "确认需求并启动设计" in soul          # 与 webui 按钮文案一致
    for k in ("core_need", "extended_need", "non_goals", "acceptance"):
        assert k in soul, k
    # 硬性顺序：点击按钮确认前，CEO 处于纯沟通阶段，绝不分发工作/建卡/起 swarm
    assert "绝不" in soul and "kanban" in soul and "swarm" in soul
    assert "纯沟通阶段" in soul
    # 落盘的 requirements.yaml 与定版四块口径一致（acceptance_core 也要被推导）
    cp = read("platform/control_plane.py")
    assert "acceptance_core" in cp
    # 按钮文案确实存在于 webui（防两边漂移）
    assert "确认需求并启动设计" in read("platform/webui.html")


def test_webui_guards_new_requirement_and_can_create_project():
    html = read("platform/webui.html")
    assert "NEW_REQ_RE" in html and "confirm(" in html        # 新需求二次确认
    assert "function createProject" in html and "newproj" in html  # 可新建项目


# --- 第七轮 P0：execute_code 全面封堵（两层 + 监测 + 验收）------------------------
def test_launcher_disables_execute_code():
    t = read("platform/launch_project.sh")
    # CEO + no-code + executor 三处 disabled_toolsets 都要含 execute_code
    assert t.count("execute_code") >= 3
    assert '"code_execution","execute_code","terminal","file"' in t  # CEO


def test_monitor_checks_execute_code_and_mount_isolation():
    t = read("platform/monitor.sh")
    assert "execute_code" in t
    assert "check_docker_mount_isolation" in t and "AUTOCODE_PROJECT_ID" in t


def test_launcher_injects_project_id_env():
    t = read("platform/launch_project.sh")
    assert "terminal.env.AUTOCODE_PROJECT_ID" in t


# --- 第七轮 P0：YOLO 默认 0（不依赖 yolo 绕过 hook）------------------------------
def test_launcher_yolo_defaults_off():
    t = read("platform/launch_project.sh")
    assert 'YOLO="${HERMES_YOLO_MODE:-0}"' in t
    assert "HERMES_YOLO_MODE:-1" not in t   # 不再默认开 yolo


# --- 第七轮 P1：orchestrator 跨进程锁 + 控制平面内嵌（systemd timer 失效兜底）------
def test_orchestrator_has_tick_lock():
    t = read("platform/orchestrator.py")
    assert "def tick_lock" in t and ".orchestrator.lock" in t


def test_control_plane_embedded_orchestrator():
    t = read("platform/control_plane.py")
    assert "AUTOCODE_EMBEDDED_ORCHESTRATOR" in t and "asyncio.to_thread" in t
    assert "lifespan" in t   # 用 lifespan 而非已弃用的 on_event


# --- 第七轮 P1：自愈 repair 卡 + 不放宽 approved 闸 + manifest 收紧 complete ------
def test_orchestrator_self_heal_and_manifest():
    t = read("platform/orchestrator.py")
    assert "approval_repair_started" in t and "qa_repair_started" in t
    assert "_release_manifest_ok" in t and "completion_mode" in t
    assert 'approved_versions.txt' in t   # canonical，不放宽


# --- 第七轮：confirm-plan 幂等 + /deliverable 端点 -------------------------------
def test_confirm_plan_idempotent_and_deliverable():
    t = read("platform/control_plane.py")
    assert "already-started" in t and "product_started" in t
    assert "def deliverable" in t and "is_done" in t


# --- 第七轮：Web UI 确认需求按钮 ------------------------------------------------
def test_webui_has_confirm_plan_button():
    html = read("platform/webui.html")
    assert "function confirmPlan" in html and "confirm-plan" in html


def test_webui_tab_switching_unified():
    # Tab 切换只有一个入口 switchTab（避免程序切 tab 但高亮不同步）
    html = read("platform/webui.html")
    assert "function switchTab" in html
    assert "state.tab = \"state\"; render()" not in html   # 旧的散落写法已统一
    assert "state.tab = \"chat\"" not in html


# --- 进度看板 / 交付视图 / 变更请求：把已有但未露出的端点接进 UI（不新建冗余页）------
def test_webui_surfaces_board_and_deliverable():
    html = read("platform/webui.html")
    # 进度看板：拉 /tasks，按卡渲染负责人+状态，5 秒自动刷新（仅页面可见）
    assert "function renderBoard" in html and "function loadBoard" in html
    assert "/tasks" in html
    assert "function startAutoRefresh" in html and "function stopAutoRefresh" in html
    assert "visibilityState" in html        # 只在可见时轮询，省资源
    # 状态/交付视图：拉 /deliverable，展示是否真正交付 + 运行方式
    assert "function renderState" in html and "/deliverable" in html
    # 变更请求入口：已交付后追加/改需求 → 建 change-request 卡
    assert "function submitChangeRequest" in html and "change-requests" in html


def test_audit_trail_wired_end_to_end():
    # 统一审计事件流：控制平面写关键动作/错误 + 编排器写阶段跃迁 + /audit 端点 + Web UI 事件页。
    cp = read("platform/control_plane.py")
    assert "audit.jsonl" in cp and "def get_audit" in cp
    for act in ("project_created", "plan_confirmed", "change_request", "architecture_swarm"):
        assert act in cp, act
    orch = read("platform/orchestrator.py")
    assert "def audit_append" in orch and "stage_transition" in orch
    html = read("platform/webui.html")
    assert "function renderAudit" in html and "/audit" in html and 'data-tab="audit"' in html


def test_ops_events_and_diagnostics_wired():
    # watchdog/monitor 告警落 audit.jsonl（共享 audit_lib）+ 一键诊断包（脚本 + 端点 + UI 下载）。
    lib = read("platform/audit_lib.sh")
    assert "audit_event()" in lib and "audit.jsonl" in lib
    wd = read("platform/watchdog.sh")
    assert "audit_lib.sh" in wd and "audit_event " in wd
    mon = read("platform/monitor.sh")
    assert "audit_lib.sh" in mon and "BASH_REMATCH" in mon   # notify 从 "project <pid>:" 提取 pid
    diag = read("platform/export-diagnostics.sh")
    assert "诊断包" in diag and "audit.jsonl" in diag and "journalctl" in diag
    cp = read("platform/control_plane.py")
    assert "def diagnostics" in cp and "export-diagnostics.sh" in cp and "PlainTextResponse" in cp
    html = read("platform/webui.html")
    assert "function downloadDiagnostics" in html and "/diagnostics" in html


def test_webui_autorefresh_is_torn_down_on_render():
    # 切 tab/换项目必须先 stopAutoRefresh，避免多个看板轮询叠加泄漏
    html = read("platform/webui.html")
    assert "stopAutoRefresh();" in html.split("async function render(")[1].split("}")[0] \
        or "stopAutoRefresh();   //" in html


def test_webui_guards_stale_async_render():
    # 异步渲染竞态：切走 tab 后，过期请求的返回不得覆盖新页面，看板也不得起悬挂定时器。
    # 用 nav 序号守卫（render 自增 _nav，子渲染 await 后比对）。
    html = read("platform/webui.html")
    assert "_nav" in html and "++state._nav" in html
    assert "state._nav !== nav" in html


def test_confirm_plan_persists_requirements_when_empty():
    # 后端：UI 不带 requirements 时也要落盘 requirements.yaml（产品委员会输入），从 CEO 对话推导
    t = read("platform/control_plane.py")
    assert "requirements.yaml" in t and "core_need" in t


# --- 第八轮 P0：watchdog/monitor 按 status 检测异常（last_event 可能为空）---------
def test_watchdog_monitor_detect_by_status():
    wd = read("platform/watchdog.sh")
    mon = read("platform/monitor.sh")
    assert '.status=="blocked"' in wd and '.status=="failed"' in wd
    assert '.status=="blocked"' in mon


# --- 第八轮 P0：余额耗尽(1113) 永久故障 → 不重试/不起新 swarm --------------------
def test_billing_dead_handling():
    assert ".provider_billing_dead" in read("platform/monitor.sh")
    assert ".provider_billing_dead" in read("platform/watchdog.sh")
    assert ".provider_billing_dead" in read("platform/orchestrator.py")
    assert "1113" in read("platform/check-models.sh")


# --- 第八轮 P0：D26 worker Docker 后端可靠（SupplementaryGroups 根治 + 后端选择）---
def test_docker_backend_reliability():
    t = read("platform/launch_project.sh")
    assert "SupplementaryGroups=docker" in t            # 根治：gateway 子进程继承 docker 组
    assert "AUTOCODE_EXECUTOR_BACKEND" in t and "AUTOCODE_ALLOW_LOCAL_EXECUTOR" in t
    assert "_verify_worker_profiles" in t               # D29 派发前校验
    assert "SupplementaryGroups=docker" in read("scripts/01-deploy-platform.sh")


# --- 第八轮 P1：D30 direct-to-QA 路径（无 fan-out 不死锁）------------------------
def test_orchestrator_direct_to_qa():
    t = read("platform/orchestrator.py")
    assert "_dev_complete" in t and "expected_files_present" in t


def test_control_plane_bind_host_configurable_and_guarded():
    # 局域网访问：bind host 可配 + 非本机+默认 token 拒绝启动
    t = read("platform/control_plane.py")
    assert "PLATFORM_BIND_HOST" in t and "change-me" in t
    deploy = read("scripts/01-deploy-platform.sh")
    assert "PLATFORM_BIND_HOST" in deploy and "--host ${BIND_HOST}" in deploy
    # 安全红线：项目 Hermes 网关不随之放开（文档明确仅控制平面）
    assert "Hermes" in read("README.md")


def test_control_plane_sets_csp_for_webui():
    t = read("platform/control_plane.py")
    assert "Content-Security-Policy" in t and "connect-src 'self'" in t


# --- 第六轮 D18：建项目失败回滚端口（PortAllocator.release）+ 回 502 ---------------
def test_create_project_has_rollback():
    t = read("platform/control_plane.py")
    assert "def release(" in t and "ports.release(" in t


# --- 第六轮 D20：kanban 子进程加超时，避免单项目卡死阻塞列表 ----------------------
def test_kanban_subprocess_has_timeout():
    t = read("platform/control_plane.py")
    assert "TimeoutExpired" in t and "KANBAN_TIMEOUT" in t


# --- 模型可用性预检：建项目前对每个 provider+model 发请求，早发现 key/模型名错 -------
def test_launcher_waits_for_gateway_ready():
    # 建项目要等 CEO gateway 真正能应答 /v1 再返回，否则建完立刻对话会打到未起好的
    # gateway，用户感知"新建项目后没反应"。enable --now 只保证进程 spawn，不代表已就绪。
    t = read("platform/launch_project.sh")
    assert "GATEWAY_READY_TIMEOUT" in t
    assert "/v1/models" in t and "urllib" in t          # HTTP 就绪探测
    # 就绪等待必须在 enable --now 之后（顺序正确）
    assert t.index("enable --now") < t.index("GATEWAY_READY_TIMEOUT")
    # 前端建项目要有进度反馈（建项目可能近 1 分钟，不能看起来卡住）
    html = read("platform/webui.html")
    assert "创建项目中" in html


def test_launcher_runs_model_preflight():
    launcher = read("platform/launch_project.sh")
    assert "AUTOCODE_MODEL_PREFLIGHT" in launcher and "check-models.sh" in launcher
    pf = read("platform/check-models.sh")
    # 命中真实预检逻辑：打 chat/completions，区分硬错误(鉴权/模型名)与限流(429 不阻断)
    assert "chat/completions" in pf and "429" in pf
