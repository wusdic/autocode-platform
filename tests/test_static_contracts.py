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
    assert "AUTOCODE_MIN_DISK_GB" in text or "AUTOCODE_ALLOW_LOW_DISK" in text


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


def test_control_plane_sets_csp_for_webui():
    t = read("platform/control_plane.py")
    assert "Content-Security-Policy" in t and "connect-src 'self'" in t
