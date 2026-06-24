#!/usr/bin/env bash
# 宿主机准备 —— 对应《02-从零开始操作手册.md》阶段 0。
# 目标环境：Ubuntu 22.04+，≥8 核 / 32GB 内存 / 200GB 磁盘，有 sudo。
set -euo pipefail

echo "==> [0.0] 资源预检（不强制失败，仅提示；低配会很慢甚至 OOM）"
cpu="$(nproc 2>/dev/null || echo 0)"
mem_gb="$(free -g 2>/dev/null | awk '/Mem:/ {print $2}')"
disk_gb="$(df -BG --output=avail / 2>/dev/null | tail -1 | tr -dc '0-9')"
[ "${cpu:-0}" -lt 4 ] && echo "  ⚠️ CPU ${cpu} 核 < 4：建议 AUTOCODE_MAX_IN_PROGRESS=1（launcher 会自动降并发）"
[ "${mem_gb:-0}" -lt 8 ] && echo "  ⚠️ 内存 ${mem_gb}GB < 8：17 角色并发可能 OOM"
[ "${disk_gb:-0}" -lt 20 ] && echo "  ⚠️ 根分区可用 ${disk_gb}GB < 20：端到端运行很可能磁盘不足"

echo "==> [0.1] 安装系统依赖"
sudo apt update && sudo apt install -y \
  git curl build-essential ripgrep ffmpeg jq \
  python3 python3-venv python3-pip \
  postgresql redis-server

echo "==> [0.1] 安装 Docker（Dev/QA worker 的真沙箱后端）"
if ! command -v docker >/dev/null 2>&1; then
  if curl -fsSL --connect-timeout 15 https://get.docker.com | sudo sh; then
    :
  else
    echo "  get.docker.com 不可达，回退安装 Ubuntu docker.io 包"
    sudo apt update && sudo apt install -y docker.io
    sudo systemctl enable --now docker || true
  fi
  sudo usermod -aG docker "$USER"
  echo "已把 $USER 加入 docker 组，请重新登录或执行 'newgrp docker' 使其生效。"
fi
# 国内网络可设 DOCKER_REGISTRY_MIRROR（如 https://docker.1ms.run）解决 Docker Hub 超时。
if [ -n "${DOCKER_REGISTRY_MIRROR:-}" ]; then
  echo "  配置 Docker 镜像源 ${DOCKER_REGISTRY_MIRROR}"
  echo "{\"registry-mirrors\":[\"${DOCKER_REGISTRY_MIRROR}\"]}" | sudo tee /etc/docker/daemon.json >/dev/null
  sudo systemctl restart docker || true
fi
docker run --rm hello-world || echo "（若此处失败，多半是 docker 组未生效或 Hub 不可达，重登/配镜像源后再试）"

echo "==> [0.2] 安装/校验 Hermes（已验证 v0.16 / v0.17.0 兼容）"
# 不直接 pipe-to-bash：installer URL 若返回官网 HTML / CDN 错页 / 登录页，bash 会执行垃圾输入，
# 报错难排查（上传的 install_hermes.sh 就是 hermes-agent.org 的首页 HTML，正是这个坑）。
# 改为先下载到文件、校验"确实是 shell 脚本"再执行。可用 HERMES_INSTALL_URL 覆盖来源。
install_hermes() {
  local url="${HERMES_INSTALL_URL:-https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh}"
  local tmp; tmp="$(mktemp)"
  echo "  下载 Hermes installer：${url}"
  curl -fL --retry 3 --connect-timeout 20 --max-time 180 \
    -H 'Accept: text/plain, text/x-shellscript, */*' -o "${tmp}" "${url}" || {
      echo "❌ 下载 installer 失败：${url}" >&2; rm -f "${tmp}"; return 1; }
  if head -n 5 "${tmp}" | grep -qiE '<!doctype html|<html|<head|Hermes Agent —'; then
    echo "❌ 下载到的是 HTML 页面而非 shell 安装脚本：${url}" >&2
    head -n 5 "${tmp}" >&2; rm -f "${tmp}"; return 1
  fi
  if ! head -n 20 "${tmp}" | grep -Eq 'bash|/bin/sh|set -e|install'; then
    echo "❌ installer 内容不像 shell 脚本，拒绝执行：${url}" >&2
    head -n 20 "${tmp}" >&2; rm -f "${tmp}"; return 1
  fi
  bash "${tmp}"; local rc=$?; rm -f "${tmp}"; return $rc
}
if ! command -v hermes >/dev/null 2>&1; then
  install_hermes
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
