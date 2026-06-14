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

  # 全量看板快照（一次拉取，供去重与读取原任务上下文）
  all_json=$(hermes kanban --board "$pid" list --json 2>/dev/null || echo '[]')

  # 覆盖设计 §7 列出的全部异常事件：gave_up / timed_out / stale / protocol_violation。
  # 连同原任务的 assignee 与 workspace 一并取出（tab 分隔），供续跑卡继承上下文。
  echo "$all_json" \
   | jq -r '.[] | select(.last_event=="gave_up" or .last_event=="timed_out"
                         or .last_event=="stale" or .last_event=="protocol_violation")
            | "\(.id)\t\(.assignee // "dev-worker-1")\t\(.workspace // "")"' \
   | while IFS=$'\t' read -r tid assignee workspace; do
       [ -z "$tid" ] && continue
       [ -z "$assignee" ] && assignee="dev-worker-1"
       title="Continue task ${tid}"
       # 去重：用官方 --idempotency-key 保证同一卡死任务只生成一张续跑卡
       # （比按标题匹配更可靠）；title 去重作为二次保险保留。
       exists=$(echo "$all_json" | jq -r --arg t "$title" \
                  '[.[] | select(.title==$t)] | length')
       [ "${exists:-0}" -gt 0 ] && continue
       # 续跑卡：沿用原 assignee 与 workspace，并在正文指明续接的原任务
       extra=(--assignee "$assignee" --idempotency-key "watchdog-continue-${tid}"
              --goal --goal-max-turns 30 --json
              --body "Continuation of task ${tid} (watchdog). Inherit its context, workspace and remaining scope.")
       [ -n "$workspace" ] && extra+=(--workspace "$workspace")
       new_tid=$(hermes kanban --board "$pid" create "$title" "${extra[@]}" 2>/dev/null \
                   | jq -r '.id // empty')
       # 关键：续跑卡是新 task id，没有自己的 allowed_paths 文件会被 fail-closed 设计闸门
       # 拦死。把原任务的 allowed_paths 复制给新 id，避免 watchdog 触发新死锁。
       dsg="${proj_dir}workspace/design"
       if [ -n "${new_tid}" ] && [ -f "${dsg}/allowed_paths.${tid}.txt" ]; then
         cp "${dsg}/allowed_paths.${tid}.txt" "${dsg}/allowed_paths.${new_tid}.txt" 2>/dev/null || true
       fi
       hermes kanban --board "$pid" comment "$tid" "watchdog: spawned continuation ${new_tid}" || true
     done
done
