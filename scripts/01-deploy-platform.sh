#!/usr/bin/env bash
# 把本仓库的平台文件部署到操作手册约定的运行时布局：
#   ~/platform/        ← platform/ 下的脚本与插件
#   ~/platform-base/   ← platform-base/ 下的模板与 skills
#   /data/projects/    ← 项目数据根目录
# 之后即可按《02-从零开始操作手册.md》阶段 4 起控制平面、阶段 5 建项目。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLATFORM_HOME="${PLATFORM_HOME:-$HOME/platform}"
PLATFORM_BASE="${PLATFORM_BASE:-$HOME/platform-base}"
PLATFORM_DATA_ROOT="${PLATFORM_DATA_ROOT:-/data/projects}"

echo "==> 部署 platform/ → ${PLATFORM_HOME}"
mkdir -p "${PLATFORM_HOME}"
cp "${REPO_ROOT}/platform/"*.py "${PLATFORM_HOME}/"
cp "${REPO_ROOT}/platform/"*.sh "${PLATFORM_HOME}/"
chmod +x "${PLATFORM_HOME}/"*.sh

echo "==> 部署 platform-base/ → ${PLATFORM_BASE}"
mkdir -p "${PLATFORM_BASE}"
cp -r "${REPO_ROOT}/platform-base/." "${PLATFORM_BASE}/"

echo "==> 准备数据根目录 ${PLATFORM_DATA_ROOT}"
if [ ! -d "${PLATFORM_DATA_ROOT}" ]; then
  sudo mkdir -p "${PLATFORM_DATA_ROOT}"
  sudo chown "$USER":"$USER" "${PLATFORM_DATA_ROOT}"
fi

echo "⚠️ 首次运行需 3-8 分钟（venv + pip + docker build），期间部分步骤静默属正常，非卡死。"
echo "==> 创建控制平面 venv（约 10-30s，静默正常）"
python3 -m venv "${PLATFORM_HOME}/venv"
echo "==> 安装依赖（首次 2-5 分钟；加进度条；VERBOSE=1 可看详细日志）"
PIP_INDEX_ARGS=()
[ -n "${PIP_INDEX_URL:-}" ] && PIP_INDEX_ARGS=(--index-url "${PIP_INDEX_URL}")
PIP_VERBOSITY=()
[ "${VERBOSE:-0}" = "1" ] && PIP_VERBOSITY=(-v)
"${PLATFORM_HOME}/venv/bin/pip" install --progress-bar on \
  --timeout "${PIP_DEFAULT_TIMEOUT:-120}" --retries "${PIP_RETRIES:-5}" \
  "${PIP_INDEX_ARGS[@]}" "${PIP_VERBOSITY[@]}" -r "${REPO_ROOT}/requirements.txt"

echo "==> 构建非 root 沙箱镜像（产物属主正确，避免 Docker root 写文件）"
SANDBOX_IMAGE="${SANDBOX_IMAGE:-autocode-python:3.11-local}"
if ! command -v docker >/dev/null 2>&1; then
  echo "❌ 未找到 docker，无法构建 dev-worker 沙箱镜像（安全模型依赖它）" >&2
  [ "${ALLOW_PUBLIC_SANDBOX_FALLBACK:-0}" = "1" ] || exit 1
# 早失败：当前用户访问不了 Docker 时，给出 newgrp 指引而非跑到 build 才挂。
# 不建议 chmod 666 docker.sock（等于把 root 权限暴露给所有本机用户）。
elif ! docker info >/dev/null 2>&1; then
  echo "❌ 当前用户无法访问 Docker。请执行 'newgrp docker' 或重新登录使组权限生效；" >&2
  echo "   不要用 'chmod 666 /var/run/docker.sock'（Docker root 权限暴露给全机用户）。" >&2
  [ "${ALLOW_PUBLIC_SANDBOX_FALLBACK:-0}" = "1" ] || exit 1
elif ! docker build --build-arg UID="$(id -u)" --build-arg GID="$(id -g)" \
       -t "${SANDBOX_IMAGE}" \
       -f "${REPO_ROOT}/docker/python-sandbox.Dockerfile" "${REPO_ROOT}"; then
  echo "❌ 沙箱镜像构建失败。修复 Docker 后重试；仅本地调试可设 ALLOW_PUBLIC_SANDBOX_FALLBACK=1。" >&2
  [ "${ALLOW_PUBLIC_SANDBOX_FALLBACK:-0}" = "1" ] || exit 1
# 防回归断言：镜像内必须有 git CLI（缺 git → worktree/分支合并全失效，第四轮 P0）。
elif ! docker run --rm "${SANDBOX_IMAGE}" git --version >/dev/null 2>&1; then
  echo "❌ 沙箱镜像内无 git（worktree/分支合并依赖它），拒绝继续。请检查 Dockerfile。" >&2
  [ "${ALLOW_PUBLIC_SANDBOX_FALLBACK:-0}" = "1" ] || exit 1
else
  echo "✅ 沙箱镜像就绪，git CLI 可用"
fi

echo "==> 安装控制平面 systemd 服务（持久 + Restart + 固定 token/PATH/XDG）"
CP_TOKEN="$(openssl rand -hex 16)"
printf '%s\n' "${CP_TOKEN}" > "${PLATFORM_HOME}/.platform_token"
printf 'PLATFORM_TOKEN=%s\nPLATFORM_DATA_ROOT=%s\n' "${CP_TOKEN}" "${PLATFORM_DATA_ROOT}" \
  > "${PLATFORM_HOME}/.platform_token.env"
chmod 600 "${PLATFORM_HOME}/.platform_token" "${PLATFORM_HOME}/.platform_token.env"
HERMES_BIN_DIR="$(dirname "$(command -v hermes 2>/dev/null || echo /usr/bin/hermes)")"
UNIT_DIR="${HOME}/.config/systemd/user"; mkdir -p "${UNIT_DIR}"
cat > "${UNIT_DIR}/autocode-control-plane.service" <<UNIT
[Unit]
Description=Autocode control plane (FastAPI)
After=network.target

[Service]
Type=simple
WorkingDirectory=${PLATFORM_HOME}
EnvironmentFile=${PLATFORM_HOME}/.platform_token.env
Environment=XDG_RUNTIME_DIR=%t
Environment=PATH=${HERMES_BIN_DIR}:${PLATFORM_HOME}/venv/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=${PLATFORM_HOME}/venv/bin/uvicorn control_plane:app --app-dir ${PLATFORM_HOME} --host 127.0.0.1 --port 9000
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
UNIT
# 运行时模式 env：把"无人值守 vs 生产"的差异显式落成一个文件，timers 统一读取。
# 否则"无人值守"要用户去记隐藏变量手动配 watchdog。AUTOCODE_MODE 驱动默认值：
#   production（默认）：保留人工 review（AUTO_APPROVE_REVIEW=0）。
#   unattended：自动放行 review（=1），安全靠 QA gate + 设计闸门 + 产物完整性闸门兜底。
#   demo：同 unattended，但应标记不可生产。
AUTOCODE_MODE="${AUTOCODE_MODE:-production}"
case "${AUTOCODE_MODE}" in
  unattended|demo) _DEF_AUTO_APPROVE=1 ;;
  *)               _DEF_AUTO_APPROVE=0 ;;
esac
cat > "${PLATFORM_HOME}/.platform_runtime.env" <<RTENV
AUTOCODE_MODE=${AUTOCODE_MODE}
AUTOCODE_AUTO_APPROVE_REVIEW=${AUTOCODE_AUTO_APPROVE_REVIEW:-${_DEF_AUTO_APPROVE}}
PROVIDER_PAUSE_SECONDS=${PROVIDER_PAUSE_SECONDS:-600}
ALERT_WEBHOOK_URL=${ALERT_WEBHOOK_URL:-}
RTENV
chmod 600 "${PLATFORM_HOME}/.platform_runtime.env"
echo "==> 运行时模式 AUTOCODE_MODE=${AUTOCODE_MODE}（auto-approve-review=${AUTOCODE_AUTO_APPROVE_REVIEW:-${_DEF_AUTO_APPROVE}}）"

echo "==> 安装自动化循环 systemd 定时器（orchestrator 编排 / watchdog 续跑 / monitor 监测）"
# 全自动闭环依赖这三个周期任务真的被装上——否则 orchestrator 状态机不跑，
# 项目停在"建好但不推进"。用 systemd --user timer（而非 crontab）与控制平面一致：
# 同享 EnvironmentFile、PATH/XDG，开机自启，失败有日志（journalctl --user -u …）。
#   orchestrator-tick  每 1 分钟：推进 产品→架构→dev→QA→release 状态机
#   watchdog           每 1 分钟：异常续跑/熔断/限流暂停/review 放行
#   monitor            每 5 分钟：健康监测 + 告警
install_timer() {  # name  exec  oncalendar
  local name="$1" exec_cmd="$2" oncal="$3"
  cat > "${UNIT_DIR}/${name}.service" <<SVC
[Unit]
Description=Autocode ${name}

[Service]
Type=oneshot
WorkingDirectory=${PLATFORM_HOME}
EnvironmentFile=${PLATFORM_HOME}/.platform_token.env
EnvironmentFile=-${PLATFORM_HOME}/.platform_runtime.env
Environment=XDG_RUNTIME_DIR=%t
Environment=PATH=${HERMES_BIN_DIR}:${PLATFORM_HOME}/venv/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=${exec_cmd}
SVC
  cat > "${UNIT_DIR}/${name}.timer" <<TMR
[Unit]
Description=Autocode ${name} timer

[Timer]
OnBootSec=1min
OnCalendar=${oncal}
Persistent=true

[Install]
WantedBy=timers.target
TMR
}
install_timer autocode-orchestrator \
  "${PLATFORM_HOME}/venv/bin/python ${PLATFORM_HOME}/orchestrator.py tick --all" "*:0/1"
install_timer autocode-watchdog "/usr/bin/env bash ${PLATFORM_HOME}/watchdog.sh" "*:0/1"
install_timer autocode-monitor  "/usr/bin/env bash ${PLATFORM_HOME}/monitor.sh"  "*:0/5"

loginctl enable-linger "$USER" 2>/dev/null || true
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
systemctl --user daemon-reload
systemctl --user enable --now autocode-control-plane.service || true
for t in autocode-orchestrator autocode-watchdog autocode-monitor; do
  systemctl --user enable --now "${t}.timer" || true
done

cat <<NEXT

✅ 部署完成。控制平面已作为 systemd 服务运行在 127.0.0.1:9000。
   PLATFORM_TOKEN 见 ${PLATFORM_HOME}/.platform_token
   状态：systemctl --user status autocode-control-plane.service
   自动化循环（已装并启用）：
     systemctl --user list-timers 'autocode-*'
     - autocode-orchestrator.timer  每分钟推进状态机
     - autocode-watchdog.timer       每分钟异常续跑/熔断
     - autocode-monitor.timer        每 5 分钟健康监测/告警
然后按操作手册阶段 5 创建第一个项目（请求头带 X-Token: <上面的 token>）。
NEXT
