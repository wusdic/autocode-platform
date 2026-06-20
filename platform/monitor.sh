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

check_first_layer() {   # ③ 第一层权限漂移：CEO 必须仍禁用 code_execution
  local pid="$1" home="$2" cfg
  # 直接读 config.yaml——`hermes config show` 的格式化输出不含 disabled_toolsets 字段
  # （真机实测会 grep 空匹配而误报 CRIT）。config set 已把它写进 config.yaml。
  cfg="${home}/profiles/ceo/config.yaml"
  [ -f "${cfg}" ] || return 0
  local dis
  dis=$(grep -A3 disabled_toolsets "${cfg}" 2>/dev/null || true)
  if ! grep -q "code_execution" <<<"${dis}"; then
    notify CRIT "project ${pid}: CEO 的 disabled_toolsets 不含 code_execution——第一层权限被改动！"
  fi
}

check_logs() {      # ④ Hermes 崩溃迹象（文件日志 + journald，因 gateway 由 systemd 托管）
  local pid="$1" home="$2" jlog
  local logf="${home}/gateway.log"
  if [ -f "${logf}" ] && grep -Eq "Traceback|CRITICAL|panic" <(tail -n 500 "${logf}" 2>/dev/null); then
    notify WARN "project ${pid}: gateway.log 近期出现 Traceback/CRITICAL，请排查"
  fi
  # 用 here-string（非 `printf | grep -q`）：避免 grep -q 提前关管道致 pipefail 下漏报。
  jlog=$(journalctl --user -u "autocode-gw-${pid}.service" -n 500 --no-pager 2>/dev/null || echo "")
  grep -Eq "Traceback|CRITICAL|panic" <<<"${jlog}" \
    && notify WARN "project ${pid}: gateway journal 近期出现 Traceback/CRITICAL"
  grep -Eq "1113|Insufficient balance|no resource package" <<<"${jlog}" \
    && notify CRIT "project ${pid}: 模型供应商余额耗尽，相关角色将无法工作（需充值）"
  if grep -Eq "1305|temporarily overloaded" <<<"${jlog}"; then
    notify WARN "project ${pid}: 模型供应商临时过载，已暂停起新 swarm ${PROVIDER_PAUSE_SECONDS:-600}s"
    # #4：写暂停标记（until epoch），watchdog 据此暂停起新 swarm，避免持续打满。
    echo "$(( $(date +%s) + ${PROVIDER_PAUSE_SECONDS:-600} ))" > "${PLATFORM_DATA_ROOT}/.provider_pause" 2>/dev/null || true
  fi
  return 0
}

check_root_files() {  # ⑥ Docker 以 root 写的产物宿主用户读不了 → 归还属主并告警
  local pid="$1" ws="${2}workspace"
  if [ -d "${ws}" ] && find "${ws}" ! -user "$(id -un)" -print -quit 2>/dev/null | grep -q .; then
    chown -R "$(id -un)":"$(id -gn)" "${ws}" 2>/dev/null \
      && notify WARN "project ${pid}: workspace 出现非当前用户文件（Docker root 写入），已尝试归还属主" \
      || notify WARN "project ${pid}: workspace 有 root 文件且 chown 失败，需手动处理"
  fi
}

check_policy_fallback() {  # ⑦ 策略闸门降级可观测：policy_plugin 走 taskless 兜底说明拿不到 task_id，
                           #    per-task allowed_paths 细粒度隔离已退化为按角色粗粒度，需排查。
  local pid="$1" ws="${2}workspace" jf
  jf="${ws}/reports/security/policy_fallback.jsonl"
  [ -f "${jf}" ] || return 0
  local n
  # 只看最近 200 行，避免历史累积长期误报；非空即提示一次。
  n=$(tail -n 200 "${jf}" 2>/dev/null | grep -c 'missing_task_allowed_paths' 2>/dev/null || echo 0)
  if [ "${n:-0}" -gt 0 ]; then
    notify WARN "project ${pid}: 策略闸门走了 ${n} 次 taskless 兜底（拿不到 task_id，细粒度隔离退化为按角色）；查 ${jf}"
  fi
}

check_disk() {      # ⑤ 数据盘：<DISK_MIN_GB(默认10) WARN；<DISK_CRIT_GB(默认2) CRIT（#7）
  local avail_gb
  avail_gb=$(df -BG --output=avail "${PLATFORM_DATA_ROOT}" 2>/dev/null | tail -1 | tr -dc '0-9')
  [ -n "${avail_gb}" ] || return 0
  if [ "${avail_gb}" -lt "${DISK_CRIT_GB:-2}" ]; then
    notify CRIT "${PLATFORM_DATA_ROOT} 仅剩 ${avail_gb}GB（<${DISK_CRIT_GB:-2}GB 危险，任务很可能失败）"
  elif [ "${avail_gb}" -lt "${DISK_MIN_GB}" ]; then
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
    check_root_files "${pid}" "${proj_dir}"
    check_policy_fallback "${pid}" "${proj_dir}"
  done
}

main "$@"
