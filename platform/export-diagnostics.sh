#!/usr/bin/env bash
# 一键诊断包 —— 把一个项目"什么时候什么地方发生了什么"汇总成【单个文本文件】，
# 可直接复制/发给研发小组做针对性排查。输出到 stdout（重定向到文件即可）。
#
# 用法：
#   bash ~/platform/export-diagnostics.sh <project_id> > diag-<pid>.txt
# 也被控制平面 GET /api/projects/<id>/diagnostics 复用（Web UI 事件页有下载按钮）。
#
# 汇总：事件时间线(audit) + 当前状态 + QA/发布 + 警告 + 策略降级 + 对话尾部 + 近期 journald + 环境。
set -uo pipefail   # 不加 -e：某段缺失不应中断整份报告

PID="${1:?usage: export-diagnostics.sh <project_id>}"
PLATFORM_DATA_ROOT="${PLATFORM_DATA_ROOT:-/data/projects}"
PLATFORM_HOME="${PLATFORM_HOME:-$HOME/platform}"
WS="${PLATFORM_DATA_ROOT}/${PID}/workspace"
AC="${WS}/.autocode"

_have() { command -v "$1" >/dev/null 2>&1; }
_section() { printf '\n\n===== %s =====\n' "$1"; }
# 把 JSONL 逐行按 jq 模板格式化；缺文件/空则打印占位。
_jsonl() {  # file jq_template
  local f="$1" tmpl="$2"
  if [ -s "$f" ]; then
    if _have jq; then jq -r "$tmpl" "$f" 2>/dev/null || cat "$f"; else cat "$f"; fi
  else
    echo "（无）"
  fi
}
_file() {  # file  —— 原样打印（pretty json 若可）
  local f="$1"
  if [ -s "$f" ]; then
    if _have jq && jq -e . "$f" >/dev/null 2>&1; then jq . "$f"; else cat "$f"; fi
  else
    echo "（无）"
  fi
}
_journal() {  # svc  —— 近期日志，突出 pid / 错误行
  local svc="$1"
  if _have journalctl; then
    journalctl --user -u "$svc" --no-pager -n 400 2>/dev/null \
      | grep -iE "${PID}|error|traceback|❌|crit|warn|failed|exception" \
      | tail -60 || echo "（无匹配）"
  elif [ -f "${PLATFORM_HOME}/${svc#autocode-}.log" ]; then
    tail -60 "${PLATFORM_HOME}/${svc#autocode-}.log"
  else
    echo "（journalctl 不可用，且无 ${PLATFORM_HOME}/*.log；cron 模式请看对应 .log）"
  fi
}

printf '# Autocode 诊断包\n'
printf 'project      : %s\n' "$PID"
printf 'generated_at : %s\n' "$(date -u -Is)"
printf 'host         : %s\n' "$(hostname 2>/dev/null || echo '?')"
printf 'workspace    : %s\n' "$WS"
if [ ! -d "$WS" ]; then
  printf '\n⚠️ 该项目 workspace 不存在（%s）。项目未建成功？先看下面的控制平面日志。\n' "$WS"
fi

_section "① 事件时间线（audit.jsonl，时间正序：动作/阶段跃迁/告警/错误）"
_jsonl "${AC}/audit.jsonl" '"[\(.ts)] \(.actor) · \(.action) · \(.detail.msg // (.detail|tostring)) \(if .result=="error" then "  <<ERROR>>" else "" end)"'

_section "② 当前流水线状态（state.json 快照）"
_file "${AC}/state.json"

_section "③ QA 结论（reports/qa/status.json）"
_file "${WS}/reports/qa/status.json"

_section "④ 发布清单（reports/release/manifest.json）"
_file "${WS}/reports/release/manifest.json"

_section "⑤ 非阻断警告（warnings.jsonl）"
_jsonl "${AC}/warnings.jsonl" '"[\(.ts // "?")] \(. | del(.ts) | tostring)"'

_section "⑥ 策略闸降级（reports/security/policy_fallback.jsonl）"
_jsonl "${WS}/reports/security/policy_fallback.jsonl" '"[\(.ts)] role=\(.role) tool=\(.tool) target=\(.target) task_id=\(.task_id)"'

_section "⑦ 与 CEO 对话（最后 20 轮，conversations/main.jsonl）"
if [ -s "${AC}/conversations/main.jsonl" ]; then
  if _have jq; then tail -20 "${AC}/conversations/main.jsonl" | jq -r '"[\(.ts)] \(.role): \(.content)"' 2>/dev/null || tail -20 "${AC}/conversations/main.jsonl"; else tail -20 "${AC}/conversations/main.jsonl"; fi
else echo "（无）"; fi

_section "⑧ 近期进程日志（journald / .log，突出 ${PID} 与错误行）"
for svc in autocode-control-plane autocode-orchestrator autocode-watchdog autocode-monitor "autocode-gw-${PID}"; do
  printf '\n--- %s ---\n' "$svc"; _journal "$svc"
done

_section "⑨ 环境"
printf 'hermes  : %s\n' "$(hermes --version 2>/dev/null || echo '不可用')"
printf 'docker  : %s\n' "$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo '不可用/无权限')"
printf 'disk    : %s\n' "$(df -h "$PLATFORM_DATA_ROOT" 2>/dev/null | tail -1 || echo '?')"
printf 'gw svc  : %s\n' "$(systemctl --user is-active "autocode-gw-${PID}.service" 2>/dev/null || echo '?')"
printf '\n（诊断包结束）\n'
