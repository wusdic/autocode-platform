#!/usr/bin/env bash
# 平台健康监测 + 告警 —— 盯住"我们改不了、只能监测"的 Hermes 运行时行为。
# 建议 cron 每 5 分钟跑：
#   */5 * * * * ALERT_WEBHOOK_URL=... ~/platform/monitor.sh >> ~/platform/monitor.log 2>&1
#
# 覆盖：① 每项目 gateway 是否存活；② 看板是否有卡死/失败任务堆积；
#       ③ 第一层权限是否被意外改动（CEO 是否仍禁用 terminal）；
#       ④ Hermes 日志是否出现崩溃；⑤ 数据盘是否将满。
# 注：pre_tool_call hook 在 kanban-worker 路径是否真生效（issue #25204）属"重型探针"，
#     由 hook_canary.sh 单独按需/每小时跑，不放进这个 5 分钟轻量循环。
#
# 告警通道（任一配置即生效，可都配）：
#   ALERT_WEBHOOK_URL  POST {"text": "..."}（Slack/飞书/钉钉自定义机器人通用）
#   ALERT_EMAIL        需主机装有 mail 命令
set -uo pipefail   # 故意不加 -e：监测脚本要尽量把所有检查跑完

PLATFORM_DATA_ROOT="${PLATFORM_DATA_ROOT:-/data/projects}"
ALERT_WEBHOOK_URL="${ALERT_WEBHOOK_URL:-}"
ALERT_EMAIL="${ALERT_EMAIL:-}"
DISK_MIN_GB="${DISK_MIN_GB:-10}"
HOSTN="$(hostname)"

notify() {
  local level="$1" msg="$2"
  local text="[autocode-monitor][${level}][${HOSTN}] ${msg}"
  echo "$(date -Is) ${text}"
  if [ -n "${ALERT_WEBHOOK_URL}" ]; then
    curl -fsS -m 10 -X POST -H 'Content-Type: application/json' \
      -d "$(jq -nc --arg t "${text}" '{text:$t}')" "${ALERT_WEBHOOK_URL}" >/dev/null 2>&1 \
      || echo "$(date -Is) [warn] webhook 投递失败"
  fi
  if [ -n "${ALERT_EMAIL}" ] && command -v mail >/dev/null 2>&1; then
    echo "${text}" | mail -s "autocode alert: ${level}" "${ALERT_EMAIL}" || true
  fi
}

check_gateway() {   # ① gateway 存活
  local pid="$1"
  if ! systemctl --user is-active --quiet "autocode-gw-${pid}.service" 2>/dev/null; then
    notify CRIT "project ${pid}: gateway 'autocode-gw-${pid}.service' 未运行"
  fi
}

check_stuck() {     # ② 卡死/失败任务堆积
  local pid="$1" home="$2" n
  n=$(HERMES_HOME="${home}" hermes kanban --board "${pid}" list --json 2>/dev/null \
      | jq '[.[] | select(.last_event=="gave_up" or .last_event=="timed_out"
                          or .last_event=="stale" or .last_event=="protocol_violation")] | length' 2>/dev/null)
  if [ "${n:-0}" -gt 0 ]; then
    notify WARN "project ${pid}: ${n} 个卡死/失败任务（watchdog 应已续跑；若持续堆积需人工介入）"
  fi
}

check_first_layer() {   # ③ 第一层权限漂移：CEO 必须仍禁用 terminal
  local pid="$1" home="$2" dis
  dis=$(HERMES_HOME="${home}" hermes -p ceo config get agent.disabled_toolsets 2>/dev/null || echo "")
  if ! printf '%s' "${dis}" | grep -q "terminal"; then
    notify CRIT "project ${pid}: CEO 的 agent.disabled_toolsets 不含 terminal——第一层权限被改动！"
  fi
}

check_logs() {      # ④ Hermes 崩溃迹象
  local pid="$1" home="$2" logf="${home}/gateway.log"
  if [ -f "${logf}" ] && tail -n 500 "${logf}" 2>/dev/null | grep -Eq "Traceback|CRITICAL|panic"; then
    notify WARN "project ${pid}: gateway.log 近期出现 Traceback/CRITICAL，请排查"
  fi
}

check_disk() {      # ⑤ 数据盘
  local avail_gb
  avail_gb=$(df -BG --output=avail "${PLATFORM_DATA_ROOT}" 2>/dev/null | tail -1 | tr -dc '0-9')
  if [ -n "${avail_gb}" ] && [ "${avail_gb}" -lt "${DISK_MIN_GB}" ]; then
    notify WARN "${PLATFORM_DATA_ROOT} 仅剩 ${avail_gb}GB（阈值 ${DISK_MIN_GB}GB）"
  fi
}

main() {
  [ -d "${PLATFORM_DATA_ROOT}" ] || { echo "no data root ${PLATFORM_DATA_ROOT}"; exit 0; }
  check_disk
  for proj_dir in "${PLATFORM_DATA_ROOT}"/*/; do
    [ -d "${proj_dir}" ] || continue
    pid="$(basename "${proj_dir}")"
    home="${proj_dir}.hermes"
    check_gateway "${pid}"
    check_stuck "${pid}" "${home}"
    check_first_layer "${pid}" "${home}"
    check_logs "${pid}" "${home}"
  done
}

main "$@"
