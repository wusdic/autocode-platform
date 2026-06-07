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

echo "==> 创建控制平面 venv 并安装依赖"
python3 -m venv "${PLATFORM_HOME}/venv"
"${PLATFORM_HOME}/venv/bin/pip" install -r "${REPO_ROOT}/requirements.txt"

cat <<NEXT

✅ 部署完成。启动控制平面：
  PLATFORM_TOKEN="\$(openssl rand -hex 16)" \\
  ${PLATFORM_HOME}/venv/bin/uvicorn control_plane:app \\
    --app-dir ${PLATFORM_HOME} --host 127.0.0.1 --port 9000

然后按操作手册阶段 5 创建第一个项目。
NEXT
