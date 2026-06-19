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

# ── 模型供应商配置（全部可用环境变量覆盖；默认双供应商交叉质疑）─────────────
# 两个大模型：GLM-5.2（默认）+ DeepSeek。zai 角色用 glm-5.2，跨供应商质疑角色用 deepseek。
# 若你的 z.ai 账号无 glm-5.2，用环境变量改：ZAI_PRIMARY_MODEL / ZAI_SECONDARY_MODEL。
ZAI_PROVIDER="${ZAI_PROVIDER:-zai}"
ZAI_BASE_URL="${ZAI_BASE_URL:-https://api.z.ai/api/paas/v4}"
ZAI_PRIMARY_MODEL="${ZAI_PRIMARY_MODEL:-glm-5.2}"      # 默认模型：决策/编码
ZAI_SECONDARY_MODEL="${ZAI_SECONDARY_MODEL:-glm-5.2}"  # 研究/综合
DEEPSEEK_PROVIDER="${DEEPSEEK_PROVIDER:-deepseek}"
DEEPSEEK_BASE_URL="${DEEPSEEK_BASE_URL:-https://api.deepseek.com/v1}"
DEEPSEEK_MODEL="${DEEPSEEK_MODEL:-deepseek-chat}"      # 批判/验收（跨供应商质疑）

# 供应商 key 早校验：缺则早失败，而非跑到一半才报 401/余额。key 放 ~/.hermes/.env。
# 用其它供应商时，用 REQUIRE_PROVIDER_KEYS 覆盖（空字符串则跳过校验）。
REQUIRE_PROVIDER_KEYS="${REQUIRE_PROVIDER_KEYS-GLM_API_KEY DEEPSEEK_API_KEY}"
_has_key() { [ -n "${!1:-}" ] || grep -q "^$1=" "$HOME/.hermes/.env" 2>/dev/null; }
for _k in ${REQUIRE_PROVIDER_KEYS}; do
  _has_key "$_k" || { echo "❌ 缺少供应商 key: ${_k}（写入 ~/.hermes/.env 或导出环境变量）"; exit 1; }
done

mkdir -p "${WORKSPACE}/design" "${WORKSPACE}/src"

echo "==> [1/6] 创建独立 Hermes 实例目录 ${HERMES_HOME}"
# 用独立 HERMES_HOME 跑后续所有 hermes 命令（已 export，子命令自动继承）

echo "==> [2/6] 初始化 Kanban board（该项目专属）"
hermes kanban boards create "${PROJECT_ID}" \
  --name "${PROJECT_ID}" --description "auto-coding project ${PROJECT_ID}" --switch

echo "==> [3/6] 创建角色 profiles + 模型/描述"
# 不同角色配不同模型/不同供应商，实现跨模型质疑。create_role 显式设 provider + base_url，
# 否则错误模型名会被发给默认供应商而失败（报告 §11/§17）。
create_role () {  # name model provider base_url desc
  local name="$1" model="$2" provider="$3" base_url="$4" desc="$5"
  hermes profile create "${name}" --description "${desc}" || true
  hermes -p "${name}" config set model.default "${model}"
  hermes -p "${name}" config set model.provider "${provider}"
  hermes -p "${name}" config set model.base_url "${base_url}"
  hermes -p "${name}" config set agent.max_turns 200
  hermes -p "${name}" config set terminal.cwd "${WORKSPACE}"
}

# CEO：决策用主力模型，只沟通不写码
create_role ceo               "${ZAI_PRIMARY_MODEL}"   "${ZAI_PROVIDER}"      "${ZAI_BASE_URL}"      "Talks to user, splits requirements, routes tasks. Never codes."
# 产品委员会：zai × deepseek 交叉（critic 用另一家做跨模型质疑）
create_role pm-lead           "${ZAI_PRIMARY_MODEL}"   "${ZAI_PROVIDER}"      "${ZAI_BASE_URL}"      "Orchestrates product council."
create_role pm-research-a     "${ZAI_SECONDARY_MODEL}" "${ZAI_PROVIDER}"      "${ZAI_BASE_URL}"      "Researches market, competitors, user scenarios."
create_role pm-research-b     "${DEEPSEEK_MODEL}"      "${DEEPSEEK_PROVIDER}" "${DEEPSEEK_BASE_URL}" "Hunts for counter-examples and risks."
create_role pm-critic         "${DEEPSEEK_MODEL}"      "${DEEPSEEK_PROVIDER}" "${DEEPSEEK_BASE_URL}" "Challenges requirement gaps and over-design."
create_role pm-synthesizer    "${ZAI_SECONDARY_MODEL}" "${ZAI_PROVIDER}"      "${ZAI_BASE_URL}"      "Synthesizes PRD."
# 架构委员会：同样交叉
create_role arch-lead         "${ZAI_PRIMARY_MODEL}"   "${ZAI_PROVIDER}"      "${ZAI_BASE_URL}"      "Orchestrates architecture council."
create_role arch-simple       "${ZAI_SECONDARY_MODEL}" "${ZAI_PROVIDER}"      "${ZAI_BASE_URL}"      "Proposes lightest viable design."
create_role arch-scale        "${DEEPSEEK_MODEL}"      "${DEEPSEEK_PROVIDER}" "${DEEPSEEK_BASE_URL}" "Proposes scalable design."
create_role arch-security     "${DEEPSEEK_MODEL}"      "${DEEPSEEK_PROVIDER}" "${DEEPSEEK_BASE_URL}" "Reviews security, isolation, permissions."
create_role arch-critic       "${DEEPSEEK_MODEL}"      "${DEEPSEEK_PROVIDER}" "${DEEPSEEK_BASE_URL}" "Finds coupling and ripple-effect risks."
create_role arch-synthesizer  "${ZAI_SECONDARY_MODEL}" "${ZAI_PROVIDER}"      "${ZAI_BASE_URL}"      "Produces ADR + interface spec + TODO."
# 研发与质控
create_role dev-lead          "${ZAI_PRIMARY_MODEL}"   "${ZAI_PROVIDER}"      "${ZAI_BASE_URL}"      "Splits and links coding tasks. Does not code."
create_role dev-worker-1      "${ZAI_PRIMARY_MODEL}"   "${ZAI_PROVIDER}"      "${ZAI_BASE_URL}"      "Implements assigned task in its worktree only."
create_role dev-worker-2      "${ZAI_PRIMARY_MODEL}"   "${ZAI_PROVIDER}"      "${ZAI_BASE_URL}"      "Implements assigned task in its worktree only."
create_role qa                "${DEEPSEEK_MODEL}"      "${DEEPSEEK_PROVIDER}" "${DEEPSEEK_BASE_URL}" "Writes/runs tests, blocks release, files defects."
create_role release           "${ZAI_PRIMARY_MODEL}"   "${ZAI_PROVIDER}"      "${ZAI_BASE_URL}"      "Merges/packages/deploys after QA gate."
create_role change-guardian   "${DEEPSEEK_MODEL}"      "${DEEPSEEK_PROVIDER}" "${DEEPSEEK_BASE_URL}" "Change impact analysis and design gate."

echo "==> [4/6] 裁剪 toolset（第一层权限）+ 注入 SOUL/AGENTS"
# CEO：只能沟通+看板。v0.16 配置值用 JSON 数组；toolsets 是"附加列表"不限制内置工具，
# 真正的第一层权限是 agent.disabled_toolsets（必须含 code_execution，否则 CEO 能直接写码）。
hermes -p ceo config set toolsets '["clarify","kanban","memory","messaging"]'
hermes -p ceo config set agent.disabled_toolsets '["code_execution","terminal","file"]'
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
# 设计角色：看板+只写 design（JSON 数组格式）
for r in pm-lead pm-critic arch-lead arch-critic change-guardian; do
  hermes -p "$r" config set toolsets '["kanban","file","web","memory"]'
done
for r in pm-research-a pm-research-b arch-simple arch-scale arch-security pm-synthesizer arch-synthesizer; do
  hermes -p "$r" config set toolsets '["web","file","memory"]'
done
# 研发总监：看板+读文件，不写业务码
hermes -p dev-lead config set toolsets '["kanban","file","memory"]'
# 真正的第一层硬抑制：agent.disabled_toolsets（JSON 数组），移除 code_execution + terminal，
# 保证 no-code 角色拿不到执行/改码工具（patch 由第二层 policy 插件兜底）。
for r in pm-lead pm-critic arch-lead arch-critic change-guardian dev-lead \
         pm-research-a pm-research-b arch-simple arch-scale arch-security \
         pm-synthesizer arch-synthesizer; do
  hermes -p "$r" config set agent.disabled_toolsets '["code_execution","terminal"]'
done
# 工程师/质控：Docker backend + worktree（真沙箱），只挂本项目 workspace
# 沙箱镜像：优先用部署时构建的非 root 镜像（映射宿主 UID，产物属主正确）。
SANDBOX_IMAGE="${SANDBOX_IMAGE:-autocode-python:3.11-local}"
docker image inspect "${SANDBOX_IMAGE}" >/dev/null 2>&1 || SANDBOX_IMAGE="python:3.11-slim"
for r in dev-worker-1 dev-worker-2 qa release; do
  hermes -p "$r" config set toolsets '["kanban","terminal","file","memory"]'
  hermes -p "$r" config set terminal.backend docker
  hermes -p "$r" config set terminal.docker_image "${SANDBOX_IMAGE}"
  # cwd 限定为本项目 workspace（gateway/cron 工作目录）。
  hermes -p "$r" config set terminal.cwd "${WORKSPACE}"
  # 只挂本项目 workspace。v0.16 期望 JSON 数组（bare string 会 ValueError）。
  hermes -p "$r" config set terminal.docker_volumes "[\"${WORKSPACE}:${WORKSPACE}\"]"
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
# 关键：仅复制文件 Hermes 不会加载插件，必须显式 enable，否则第二层设计闸门全程缺席。
# HERMES_ACCEPT_HOOKS=1 让非交互（gateway）路径也接受 hook，否则 pre_tool_call 可能不触发。
export HERMES_ACCEPT_HOOKS=1
# ⚠️ 不要把 plugins-list 输出用管道接 grep -q 来校验：grep -q 命中即提前关管道，
# pipefail 下会把 hermes 的 SIGPIPE(141) 当成失败而误判"未启用"→ 建项目 100% 失败（真机 P0）。
# 改为检查 enable 命令自身输出（无管道）。
_enable_out="$(hermes plugins enable policy 2>&1 || true)"
printf '%s\n' "${_enable_out}"
case "${_enable_out}" in
  *enabled*) : ;;   # "Plugin 'policy' enabled." 或 "... already enabled."
  *) echo "❌ policy 插件启用失败，拒绝继续（第二层安全闸门缺失）：${_enable_out}"; exit 1 ;;
esac

echo "==> [6/6] 配置 Kanban + 审批 + API server，启动 gateway（内嵌 dispatcher）"
hermes config set kanban.dispatch_in_gateway true
# 无人值守：跳过 DANGEROUS(61)/Tirith(~80) 审批，否则自动化流程被 approval 反复打断。
# 安全不降级——HARDLINE(12 条: rm -rf /, mkfs, dd, fork bomb…) + sudo-stdin guard 仍不可
# 绕过；CEO 无终端、dev-worker 在 Docker（源码级豁免）。可用 HERMES_APPROVALS_MODE 覆盖。
hermes config set approvals.mode "${HERMES_APPROVALS_MODE:-off}"
# 低配机器（<4 核）默认降并发到 1，减少 429/OOM/CPU 排队（报告环境 2 核易排队）。
MAX_IN_PROGRESS="${AUTOCODE_MAX_IN_PROGRESS:-3}"
[ "$(nproc 2>/dev/null || echo 4)" -lt 4 ] && MAX_IN_PROGRESS="${AUTOCODE_MAX_IN_PROGRESS:-1}"
hermes config set kanban.max_in_progress "${MAX_IN_PROGRESS}"
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
HERMES_BIN_DIR="$(dirname "$(command -v hermes)")"
YOLO="${HERMES_YOLO_MODE:-1}"   # gateway 无人值守，无人可审批 → 默认跳过审批（见上方安全说明）
cat > "${UNIT_DIR}/${SERVICE}.service" <<EOF
[Unit]
Description=Autocode Hermes gateway for project ${PROJECT_ID}
After=network.target

[Service]
Type=simple
Environment=HERMES_HOME=${HERMES_HOME}
Environment=HERMES_ACCEPT_HOOKS=1
Environment=HERMES_YOLO_MODE=${YOLO}
Environment=XDG_RUNTIME_DIR=%t
Environment=PATH=${HERMES_BIN_DIR}:/usr/local/bin:/usr/bin:/bin
EnvironmentFile=-${HOME}/.hermes/.env
WorkingDirectory=${WORKSPACE}
ExecStart=$(command -v hermes) -p ceo gateway run
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF
# 让 user service 在用户未登录时也能运行（开机自启）。
loginctl enable-linger "$USER" 2>/dev/null || true
# user systemd 需要 XDG_RUNTIME_DIR，否则 daemon-reload 报 "No medium found"（NEW-J）。
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
systemctl --user daemon-reload
systemctl --user enable --now "${SERVICE}.service"
# 用 `gateway run`（前台模式），故 Type=simple 正确——systemd 直接托管该前台进程。
# （`gateway start` 需先 `gateway install` 的机器级服务，项目独立 HERMES_HOME 下不存在。）

echo "✅ 项目 ${PROJECT_ID} 就绪。CEO API 端口=${BASE_PORT}，HERMES_HOME=${HERMES_HOME}"
echo "   systemd 单元=${SERVICE}.service（systemctl --user status ${SERVICE}）"
echo "   API_SERVER_KEY 见 ${HERMES_HOME}/profiles/ceo/.env"
