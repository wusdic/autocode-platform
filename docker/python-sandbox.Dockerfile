# 非 root 的 dev/qa 沙箱镜像：把容器内用户映射到宿主 UID/GID，
# 避免 Docker 以 root 写产物、宿主用户读不了（报告 §26.1 PermissionError）。
# 构建（scripts/01-deploy-platform.sh 会自动做）：
#   docker build --build-arg UID=$(id -u) --build-arg GID=$(id -g) \
#     -t autocode-python:3.11-local -f docker/python-sandbox.Dockerfile .
FROM python:3.11-slim

ARG UID=1000
ARG GID=1000
# 国内网络 deb.debian.org 常不可达导致构建失败（真机 D12 高频）。默认**不改源**；
# 国内构建时传：--build-arg APT_MIRROR=mirrors.aliyun.com（deploy 脚本透传 APT_MIRROR 环境变量）。
ARG APT_MIRROR=""

# 必须在切到非 root USER 之前装系统包（USER 后无 root 权限）。
# git：dev-worker 的 worktree 工作区模式依赖它（worktree add / branch / commit / merge）。
#      不装则 worktree 失效 → 并行任务在同一 workspace 互相覆盖、交付不可追溯（真机 P0）。
# ca-certificates：git over https / pip 校验证书所需。
# --no-install-recommends 控制镜像体积（slim 基础上只增几 MB）。
RUN if [ -n "${APT_MIRROR}" ]; then \
      sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/*.sources 2>/dev/null \
      || sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list 2>/dev/null || true; \
    fi \
 && apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd -g "${GID}" autocode 2>/dev/null || true \
 && useradd -m -u "${UID}" -g "${GID}" autocode 2>/dev/null || true

USER autocode

# worktree 内 git commit 需要提交身份；容器内无身份时 commit 会报 "Please tell me who you are"
# 而直接失败（这是"git 历史只有 init / 没产物"的次要根因）。给非 root 用户设全局兜底身份。
# safe.directory '*'：容器内看到的是宿主属主的挂载目录，较新 git 会因 "dubious ownership"
# 拒绝操作，显式信任避免 worker 的 git 命令被拒。仓库自身的 user.name/email 仍会覆盖此兜底。
RUN git config --global user.email "autocode@sandbox.local" \
 && git config --global user.name  "autocode-sandbox" \
 && git config --global init.defaultBranch main \
 && git config --global --add safe.directory '*'

WORKDIR /workspace
