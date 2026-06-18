# 非 root 的 dev/qa 沙箱镜像：把容器内用户映射到宿主 UID/GID，
# 避免 Docker 以 root 写产物、宿主用户读不了（报告 §26.1 PermissionError）。
# 构建（scripts/01-deploy-platform.sh 会自动做）：
#   docker build --build-arg UID=$(id -u) --build-arg GID=$(id -g) \
#     -t autocode-python:3.11-local -f docker/python-sandbox.Dockerfile .
FROM python:3.11-slim

ARG UID=1000
ARG GID=1000

RUN groupadd -g "${GID}" autocode 2>/dev/null || true \
 && useradd -m -u "${UID}" -g "${GID}" autocode 2>/dev/null || true

USER autocode
WORKDIR /workspace
