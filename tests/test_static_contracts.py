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


# --- NEW-K/M：列表/卷配置必须 JSON 数组形式 ----------------------------------
def test_launcher_config_values_are_json_arrays():
    text = read("platform/launch_project.sh")
    assert 'docker_volumes "[\\"' in text          # JSON 数组，非 bare string
    assert "disabled_toolsets '[\"code_execution" in text  # 含 code_execution
