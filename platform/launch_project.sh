#!/usr/bin/env bash
# 项目启动器 —— 整个平台的核心运维脚本。
# 对应《02-从零开始操作手册.md》阶段 2。
#
# 用法：launch_project.sh <project_id> <base_port>
#
# 路径约定（可用环境变量覆盖，默认与操作手册一致）：
#   PLATFORM_HOME       存放本脚本与 policy_plugin.py（默认 ~/platform）
#   PLATFORM_BASE       存放 templates/ 与 skills/（默认 ~/platform-base）
#   PLATFORM_DATA_ROOT  项目数据根目录（默认 /data/projects）
set -euo pipefail

PROJECT_ID="${1:?usage: launch_project.sh <project_id> <base_port>}"
BASE_PORT="${2:?need base_port, e.g. 8650}"

PLATFORM_HOME="${PLATFORM_HOME:-$HOME/platform}"
PLATFORM_BASE="${PLATFORM_BASE:-$HOME/platform-base}"
PLATFORM_DATA_ROOT="${PLATFORM_DATA_ROOT:-/data/projects}"

PROOT="${PLATFORM_DATA_ROOT}/${PROJECT_ID}"
export HERMES_HOME="${PROOT}/.hermes"
WORKSPACE="${PROOT}/workspace"

mkdir -p "${WORKSPACE}/design" "${WORKSPACE}/src"

echo "==> [1/6] 创建独立 Hermes 实例目录 ${HERMES_HOME}"
# 用独立 HERMES_HOME 跑后续所有 hermes 命令（已 export，子命令自动继承）

echo "==> [2/6] 初始化 Kanban board（该项目专属）"
hermes kanban boards create "${PROJECT_ID}" \
  --name "${PROJECT_ID}" --description "auto-coding project ${PROJECT_ID}" --switch

echo "==> [3/6] 创建角色 profiles + 模型/描述"
# 不同角色配不同模型，实现多模型质疑。模型字符串按你的供应商替换。
create_role () {
  local name="$1" model="$2" desc="$3"
  hermes profile create "${name}" --description "${desc}" || true
  hermes -p "${name}" config set model.default "${model}"
  hermes -p "${name}" config set agent.max_turns 200
  hermes -p "${name}" config set terminal.cwd "${WORKSPACE}"
}

# CEO：只沟通（toolset 在第 4 步裁剪）
create_role ceo               "anthropic/claude-opus-4.6"        "Talks to user, splits requirements, routes tasks. Never codes."
# 产品委员会（不同模型）
create_role pm-lead           "anthropic/claude-sonnet-4.6"      "Orchestrates product council."
create_role pm-research-a     "openai/gpt-5.1"                   "Researches market, competitors, user scenarios."
create_role pm-research-b     "google/gemini-3-flash-preview"   "Hunts for counter-examples and risks."
create_role pm-critic         "anthropic/claude-opus-4.6"        "Challenges requirement gaps and over-design."
create_role pm-synthesizer    "anthropic/claude-sonnet-4.6"      "Synthesizes PRD."
# 架构委员会
create_role arch-lead         "anthropic/claude-sonnet-4.6"      "Orchestrates architecture council."
create_role arch-simple       "anthropic/claude-sonnet-4.6"      "Proposes lightest viable design."
create_role arch-scale        "openai/gpt-5.1"                   "Proposes scalable design."
create_role arch-security     "anthropic/claude-opus-4.6"        "Reviews security, isolation, permissions."
create_role arch-critic       "google/gemini-3-flash-preview"   "Finds coupling and ripple-effect risks."
create_role arch-synthesizer  "anthropic/claude-sonnet-4.6"      "Produces ADR + interface spec + TODO."
# 研发与质控
create_role dev-lead          "anthropic/claude-sonnet-4.6"      "Splits and links coding tasks. Does not code."
create_role dev-worker-1      "anthropic/claude-sonnet-4.6"      "Implements assigned task in its worktree only."
create_role dev-worker-2      "anthropic/claude-sonnet-4.6"      "Implements assigned task in its worktree only."
create_role qa                "anthropic/claude-opus-4.6"        "Writes/runs tests, blocks release, files defects."
create_role release           "anthropic/claude-sonnet-4.6"      "Merges/packages/deploys after QA gate."
create_role change-guardian   "anthropic/claude-opus-4.6"        "Change impact analysis and design gate."

echo "==> [4/6] 裁剪 toolset（第一层权限）+ 注入 SOUL/AGENTS"
# CEO：只能沟通+看板，无 terminal/file/patch
hermes -p ceo config set toolsets "clarify,kanban,memory,messaging"
# 注入各角色 SOUL（模板存在才复制）
inject_soul () {
  local role="$1"
  local tpl="${PLATFORM_BASE}/templates/SOUL.${role}.md"
  [ -f "${tpl}" ] && cp "${tpl}" "${HERMES_HOME}/profiles/${role}/SOUL.md" 2>/dev/null || true
}
for r in ceo change-guardian pm-lead pm-research-a pm-research-b pm-critic pm-synthesizer \
         arch-lead arch-simple arch-scale arch-security arch-critic arch-synthesizer \
         dev-lead qa release; do
  inject_soul "$r"
done
# dev-worker-1/2 共用 dev-worker 模板
for r in dev-worker-1 dev-worker-2; do
  tpl="${PLATFORM_BASE}/templates/SOUL.dev-worker.md"
  [ -f "${tpl}" ] && cp "${tpl}" "${HERMES_HOME}/profiles/${r}/SOUL.md" 2>/dev/null || true
done
# 设计角色：看板+只写 design，无 terminal/patch
for r in pm-lead pm-critic arch-lead arch-critic change-guardian; do
  hermes -p "$r" config set toolsets "kanban,file,web,memory"
done
for r in pm-research-a pm-research-b arch-simple arch-scale arch-security pm-synthesizer arch-synthesizer; do
  hermes -p "$r" config set toolsets "web,file,memory"
done
# 研发总监：看板+读文件，不写业务码
hermes -p dev-lead config set toolsets "kanban,file,memory"
# 工程师/质控：Docker backend + worktree（真沙箱），只挂本项目 workspace
for r in dev-worker-1 dev-worker-2 qa release; do
  hermes -p "$r" config set toolsets "kanban,terminal,file,memory"
  hermes -p "$r" config set terminal.backend docker
  hermes -p "$r" config set terminal.docker_image "python:3.11-slim"
  # cwd 限定为本项目 workspace：Docker backend 默认只挂载 cwd，worker 看不到
  # /data/projects 下的其它项目目录。
  hermes -p "$r" config set terminal.cwd "${WORKSPACE}"
  # 显式只挂 workspace（防止默认把更大的目录挂进容器）。不同 Hermes 版本配置键
  # 可能不同，以 `hermes config --help` 为准；写不进去也不影响上面的 cwd 限定，
  # 故以 || true 兜底，避免键名差异中断启动。
  hermes -p "$r" config set terminal.docker_mounts "${WORKSPACE}:${WORKSPACE}" 2>/dev/null \
    || echo "   （提示：terminal.docker_mounts 键名因版本而异，请按 hermes config --help 核对挂载收窄）"
done
# 复制 base skills 快照到各 profile（只读模板）
for d in "${HERMES_HOME}"/profiles/*/; do
  mkdir -p "${d}skills"
  cp -rn "${PLATFORM_BASE}"/skills/* "${d}skills/" 2>/dev/null || true
done
# 注入全局 AGENTS.md 到 workspace（worker 在 workdir 内会被加载）
cp "${PLATFORM_BASE}/templates/AGENTS.md" "${WORKSPACE}/AGENTS.md"

echo "==> [5/6] 安装 policy-plugin（第二层权限 hook）"
mkdir -p "${HERMES_HOME}/plugins/policy"
cp "${PLATFORM_HOME}/policy_plugin.py" "${HERMES_HOME}/plugins/policy/plugin.py"
cat > "${HERMES_HOME}/plugins/policy/plugin.yaml" <<EOF
name: policy
description: Role permission and design-gate enforcement
EOF

echo "==> [6/6] 配置 Kanban + API server，启动 gateway（内嵌 dispatcher）"
hermes config set kanban.dispatch_in_gateway true
hermes config set kanban.max_in_progress 3
hermes config set kanban.failure_limit 2
# 该项目 CEO 的 API 端口（供你的网关转发）
cat >> "${HERMES_HOME}/profiles/ceo/.env" <<EOF
API_SERVER_ENABLED=true
API_SERVER_PORT=${BASE_PORT}
API_SERVER_KEY=$(openssl rand -hex 16)
EOF
# 不用 `hermes -p ceo gateway install`：它的 systemd 单元名只按 profile（都叫 ceo）
# 生成，多项目会互相覆盖。改为每项目一个唯一命名的 user 级 systemd 单元，
# 名字带 PROJECT_ID，彻底避免冲突。
SERVICE="autocode-gw-${PROJECT_ID}"
UNIT_DIR="${HOME}/.config/systemd/user"
mkdir -p "${UNIT_DIR}"
cat > "${UNIT_DIR}/${SERVICE}.service" <<EOF
[Unit]
Description=Autocode Hermes gateway for project ${PROJECT_ID}
After=network.target

[Service]
Type=simple
Environment=HERMES_HOME=${HERMES_HOME}
ExecStart=$(command -v hermes) -p ceo gateway start
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF
# 让 user service 在用户未登录时也能运行（开机自启）。
loginctl enable-linger "$USER" 2>/dev/null || true
systemctl --user daemon-reload
systemctl --user enable --now "${SERVICE}.service"
# 注：若你的 Hermes 版本 `gateway start` 会自我后台化并退出，把上面 Type 改为
# forking，或换成对应的前台运行命令（hermes gateway --help 核对）。

echo "✅ 项目 ${PROJECT_ID} 就绪。CEO API 端口=${BASE_PORT}，HERMES_HOME=${HERMES_HOME}"
echo "   systemd 单元=${SERVICE}.service（systemctl --user status ${SERVICE}）"
echo "   API_SERVER_KEY 见 ${HERMES_HOME}/profiles/ceo/.env"
