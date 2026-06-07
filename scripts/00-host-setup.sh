#!/usr/bin/env bash
# 宿主机准备 —— 对应《02-从零开始操作手册.md》阶段 0。
# 目标环境：Ubuntu 22.04+，≥8 核 / 32GB 内存 / 200GB 磁盘，有 sudo。
set -euo pipefail

echo "==> [0.1] 安装系统依赖"
sudo apt update && sudo apt install -y \
  git curl build-essential ripgrep ffmpeg jq \
  python3 python3-venv python3-pip \
  postgresql redis-server

echo "==> [0.1] 安装 Docker（Dev/QA worker 的真沙箱后端）"
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER"
  echo "已把 $USER 加入 docker 组，请重新登录或执行 'newgrp docker' 使其生效。"
fi
docker run --rm hello-world || echo "（若此处失败，多半是 docker 组未生效，重登后再试）"

echo "==> [0.2] 安装 Hermes 0.16"
if ! command -v hermes >/dev/null 2>&1; then
  curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
fi
# shellcheck disable=SC1090
source ~/.bashrc || true
hermes --version
hermes doctor

cat <<'NEXT'

✅ 阶段 0 完成。接下来：
  1. 配置模型供应商：hermes setup --portal   （或 hermes setup 自带 key）
     验证：hermes -z "say hi"
  2. 运行 scripts/01-deploy-platform.sh 把本仓库文件部署到 ~/platform 与 ~/platform-base
  3. 按操作手册阶段 4 启动控制平面
NEXT
