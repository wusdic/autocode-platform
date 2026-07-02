#!/usr/bin/env bash
# 共享审计写入器 —— 供 watchdog.sh / monitor.sh 向项目审计流追加事件，
# 与 control_plane._audit / orchestrator.audit_append **同格式**：
#   {ts, actor, action, detail:{...}, result}
# 落到 <PLATFORM_DATA_ROOT>/<pid>/workspace/.autocode/audit.jsonl，
# 让 Web UI【事件】页与研发排查一站式看到运维层事件（续跑/熔断/告警）。
#
# 用法：audit_event <pid> <actor> <action> <message>
# 依赖 jq（watchdog/monitor 本就用 jq）；无 jq 或写盘失败一律静默（审计不影响主流程）。

audit_event() {
  local pid="$1" actor="$2" action="$3" msg="${4:-}"
  [ -n "${pid}" ] || return 0
  command -v jq >/dev/null 2>&1 || return 0
  local dir="${PLATFORM_DATA_ROOT:-/data/projects}/${pid}/workspace/.autocode"
  mkdir -p "${dir}" 2>/dev/null || return 0
  # 轮转（与 Python 侧同口径 5MB 单代）：防长期运行无界膨胀
  local f="${dir}/audit.jsonl" sz
  sz=$(stat -c%s "$f" 2>/dev/null || echo 0)
  [ "${sz:-0}" -gt 5000000 ] && mv -f "$f" "$f.1" 2>/dev/null
  jq -nc --arg ts "$(date -u -Is)" --arg a "${actor}" --arg ac "${action}" --arg m "${msg}" \
     '{ts:$ts, actor:$a, action:$ac, detail:{msg:$m}, result:"ok"}' \
     >> "${dir}/audit.jsonl" 2>/dev/null || true
}
