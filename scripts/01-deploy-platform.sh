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

echo "==> 创建控制平面 venv 并安装依赖（带超时/重试，可选 PIP_INDEX_URL 镜像）"
python3 -m venv "${PLATFORM_HOME}/venv"
PIP_INDEX_ARGS=()
[ -n "${PIP_INDEX_URL:-}" ] && PIP_INDEX_ARGS=(--index-url "${PIP_INDEX_URL}")
"${PLATFORM_HOME}/venv/bin/pip" install \
  --timeout "${PIP_DEFAULT_TIMEOUT:-120}" --retries "${PIP_RETRIES:-5}" \
  "${PIP_INDEX_ARGS[@]}" -r "${REPO_ROOT}/requirements.txt"

echo "==> 构建非 root 沙箱镜像（产物属主正确，避免 Docker root 写文件）"
if ! command -v docker >/dev/null 2>&1; then
  echo "❌ 未找到 docker，无法构建 dev-worker 沙箱镜像（安全模型依赖它）" >&2
  [ "${ALLOW_PUBLIC_SANDBOX_FALLBACK:-0}" = "1" ] || exit 1
elif ! docker build --build-arg UID="$(id -u)" --build-arg GID="$(id -g)" \
       -t "${SANDBOX_IMAGE:-autocode-python:3.11-local}" \
       -f "${REPO_ROOT}/docker/python-sandbox.Dockerfile" "${REPO_ROOT}"; then
  echo "❌ 沙箱镜像构建失败。修复 Docker 后重试；仅本地调试可设 ALLOW_PUBLIC_SANDBOX_FALLBACK=1。" >&2
  [ "${ALLOW_PUBLIC_SANDBOX_FALLBACK:-0}" = "1" ] || exit 1
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
