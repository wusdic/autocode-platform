#!/usr/bin/env bash
# Watchdog —— 处理 90 轮预算耗尽 / 崩溃 / 超时 / 卡死。
# 对应《02-从零开始操作手册.md》阶段 9。用 cron 每分钟跑：
#   * * * * * ~/platform/watchdog.sh >> ~/platform/watchdog.log 2>&1
set -euo pipefail

PLATFORM_DATA_ROOT="${PLATFORM_DATA_ROOT:-/data/projects}"

for proj_dir in "${PLATFORM_DATA_ROOT}"/*/; do
  [ -d "$proj_dir" ] || continue
  pid=$(basename "$proj_dir")
  export HERMES_HOME="${proj_dir}.hermes"
  # 找出失败/超时/卡死的任务
  hermes kanban --board "$pid" list --status blocked --json 2>/dev/null \
   | jq -r '.[] | select(.last_event=="gave_up" or .last_event=="timed_out") | .id' \
   | while read -r tid; do
       [ -z "$tid" ] && continue
       # 自动建 continuation 卡，不等用户
       hermes kanban --board "$pid" create "Continue task ${tid}" \
         --assignee dev-worker-1 --goal --goal-max-turns 30 || true
       hermes kanban --board "$pid" comment "$tid" "watchdog: spawned continuation" || true
     done
done
