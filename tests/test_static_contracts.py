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
    assert 'state.get("qa_started") and self._qa_release_allowed' in text


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
