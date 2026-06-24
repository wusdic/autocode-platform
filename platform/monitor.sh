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

check_gateway() {   # ① gateway 存活（可选自恢复，默认只告警）
  local pid="$1"
  if ! systemctl --user is-active --quiet "autocode-gw-${pid}.service" 2>/dev/null; then
    notify CRIT "project ${pid}: gateway 'autocode-gw-${pid}.service' 未运行"
    # AUTOCODE_AUTO_RESTART_GATEWAY=1 时尝试拉起（默认关，避免掩盖反复崩溃的真问题）。
    if [ "${AUTOCODE_AUTO_RESTART_GATEWAY:-0}" = "1" ]; then
      systemctl --user restart "autocode-gw-${pid}.service" 2>/dev/null \
        && notify WARN "project ${pid}: gateway 已尝试自动重启" \
        || notify CRIT "project ${pid}: gateway 自动重启失败，需人工介入"
    fi
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
  # 关键修复（D12 无限暂停循环）：用时间窗 `--since` 而非 `-n 500`。`-n 500` 取最近 500 行
  # 与时间无关，旧的 1305/429 日志会被每轮反复匹配，每次重写 .provider_pause →“恢复了还在暂停”。
  jlog=$(journalctl --user -u "autocode-gw-${pid}.service" --since "${JOURNAL_SINCE:-10 min ago}" --no-pager 2>/dev/null || echo "")
  grep -Eq "Traceback|CRITICAL|panic" <<<"${jlog}" \
    && notify WARN "project ${pid}: gateway journal 近期出现 Traceback/CRITICAL"
  grep -Eq "1113|Insufficient balance|no resource package" <<<"${jlog}" \
    && notify CRIT "project ${pid}: 模型供应商余额耗尽，相关角色将无法工作（需充值）"
  if grep -Eq "1305|temporarily overloaded" <<<"${jlog}"; then
    # 去重：同一条限流日志只触发一次暂停。取窗口内最新一行 1305 的指纹（行内容哈希），
    # 与上次已处理的指纹比对；相同则不重写（避免“清了又写”把暂停无限续命）。
    local newest fp_file last_fp
    newest=$(grep -E "1305|temporarily overloaded" <<<"${jlog}" | tail -1)
    fp_file="${home}/.last_1305_fp"
    last_fp=$(cat "${fp_file}" 2>/dev/null || echo "")
    local cur_fp; cur_fp=$(printf '%s' "${newest}" | cksum | awk '{print $1}')
    if [ "${cur_fp}" != "${last_fp}" ]; then
      notify WARN "project ${pid}: 模型供应商临时过载，暂停起新 swarm ${PROVIDER_PAUSE_SECONDS:-600}s"
      echo "$(( $(date +%s) + ${PROVIDER_PAUSE_SECONDS:-600} ))" > "${PLATFORM_DATA_ROOT}/.provider_pause" 2>/dev/null || true
      printf '%s' "${cur_fp}" > "${fp_file}" 2>/dev/null || true
    fi
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

check_dev_commits() {  # ⑧ 交付完整性：dev 卡 done 但 git 几乎无提交 → 疑似产物未落地/worktree 未生效
  local pid="$1" ws="${2}workspace"
  [ -d "${ws}/.git" ] || return 0
  local done_dev commits
  done_dev=$(HERMES_HOME="${2}.hermes" hermes kanban --board "${pid}" list --json 2>/dev/null \
    | jq '[.[] | select((.assignee // "")|startswith("dev-worker")) | select(.status=="done")] | length' 2>/dev/null)
  commits=$(git -C "${ws}" rev-list --all --count 2>/dev/null || echo 0)
  # 有 >=2 个 dev 卡完成，却只有 init 提交 → 高度疑似"声称完成但产物没落地/被覆盖"。
  if [ "${done_dev:-0}" -ge 2 ] && [ "${commits:-0}" -le 1 ]; then
    notify WARN "project ${pid}: ${done_dev} 个 dev 卡 done，但 git 仅 ${commits} 次提交——疑似产物未提交/worktree 未生效（查 ${ws}/.worktrees/）"
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
    check_dev_commits "${pid}" "${proj_dir}"
  done
}

main "$@"
