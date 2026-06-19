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
loginctl enable-linger "$USER" 2>/dev/null || true
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
systemctl --user daemon-reload
systemctl --user enable --now autocode-control-plane.service || true

cat <<NEXT

✅ 部署完成。控制平面已作为 systemd 服务运行在 127.0.0.1:9000。
   PLATFORM_TOKEN 见 ${PLATFORM_HOME}/.platform_token
   状态：systemctl --user status autocode-control-plane.service
然后按操作手册阶段 5 创建第一个项目（请求头带 X-Token: <上面的 token>）。
NEXT
