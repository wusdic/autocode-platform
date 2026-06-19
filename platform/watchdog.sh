#!/usr/bin/env bash
# Watchdog —— 异常续跑/熔断/限流暂停/review 放行（**不做正常业务编排**，那归 orchestrator.py）。
# 对应《02-从零开始操作手册.md》阶段 9。用 cron 每分钟跑：
#   * * * * * ~/platform/watchdog.sh >> ~/platform/watchdog.log 2>&1
set -euo pipefail

PLATFORM_DATA_ROOT="${PLATFORM_DATA_ROOT:-/data/projects}"
MAX_CONTINUATIONS="${MAX_CONTINUATIONS:-20}"   # 每项目续跑卡总上限，防永久失败任务无限续跑（#5）

for proj_dir in "${PLATFORM_DATA_ROOT}"/*/; do
  [ -d "$proj_dir" ] || continue
  pid=$(basename "$proj_dir")
  export HERMES_HOME="${proj_dir}.hermes"
  cnt_file="${proj_dir}workspace/.autocode/continuation_count"
  mkdir -p "${proj_dir}workspace/.autocode" 2>/dev/null || true

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
                  '[.[] | select(.title==$t)] | length' 2>/dev/null || echo 0)
       [ "${exists:-0}" -gt 0 ] && continue
       # 熔断（#5）：续跑卡超过上限就停自动续跑，建一张人工 review 卡而非无限续。
       cnt=$(cat "$cnt_file" 2>/dev/null | tr -dc '0-9'); cnt="${cnt:-0}"
       if [ "$cnt" -ge "$MAX_CONTINUATIONS" ]; then
         if [ ! -f "${proj_dir}workspace/.autocode/.continuation_capped" ]; then
           hermes kanban --board "$pid" create "NEEDS HUMAN REVIEW: ${MAX_CONTINUATIONS}+ continuations" \
             --assignee dev-lead --idempotency-key "watchdog-capped-${pid}" \
             --body "Watchdog 续跑已达上限 ${MAX_CONTINUATIONS}，疑似永久失败，停止自动续跑，请人工介入。" 2>/dev/null || true
           touch "${proj_dir}workspace/.autocode/.continuation_capped"
           echo "$(date -Is) [warn] project ${pid}: 续跑达上限 ${MAX_CONTINUATIONS}，停止自动续跑（建人工 review 卡）"
         fi
         continue
       fi
       # 续跑卡：沿用原 assignee 与 workspace，并在正文指明续接的原任务
       extra=(--assignee "$assignee" --idempotency-key "watchdog-continue-${tid}"
              --goal --goal-max-turns 30 --json
              --body "Continuation of task ${tid} (watchdog). Inherit its context, workspace and remaining scope.")
       [ -n "$workspace" ] && extra+=(--workspace "$workspace")
       # 不吞 stderr：创建失败时把错误留在日志里（问题D 可观测性）。
       # 关键：必须 `|| true`，否则 set -e 会在 create 失败时中止整个 while 循环，
       # 连下面的容错日志都执行不到，且跳过其余卡死任务。
       create_out=$(hermes kanban --board "$pid" create "$title" "${extra[@]}" 2>&1) || true
       new_tid=$(printf '%s' "$create_out" | jq -r '.id // empty' 2>/dev/null || true)
       if [ -z "${new_tid}" ]; then
         echo "$(date -Is) [warn] project ${pid}: 续跑卡创建未返回 id（下个 tick 会因 idempotency-key 重试）；输出：${create_out}"
         continue
       fi
       # 关键：续跑卡是新 task id，没有自己的 allowed_paths 文件会被 fail-closed 设计闸门
       # 拦死。把原任务的 allowed_paths 复制给新 id，避免 watchdog 触发新死锁。
       dsg="${proj_dir}workspace/design"
       if [ -f "${dsg}/allowed_paths.${tid}.txt" ]; then
         cp "${dsg}/allowed_paths.${tid}.txt" "${dsg}/allowed_paths.${new_tid}.txt" 2>/dev/null || true
       fi
       hermes kanban --board "$pid" comment "$tid" "watchdog: spawned continuation ${new_tid}" || true
       echo "$(( cnt + 1 ))" > "$cnt_file"   # 续跑计数 +1（#5 熔断用）
     done

  # #1 可选：自动放行 dev-worker 的 review-required 自我阻断（默认关，保留人工把关）。
  # 开启后由 watchdog 直接 kanban complete，实现真正零人工无人值守——但这会跳过人工代码
  # 评审，请确保信任 QA gate + 设计闸门。设 AUTOCODE_AUTO_APPROVE_REVIEW=1 开启。
  if [ "${AUTOCODE_AUTO_APPROVE_REVIEW:-0}" = "1" ]; then
    echo "$all_json" \
     | jq -r '.[] | select(([.block_reason, .reason, .blocked_reason, .last_event, .status]
                            | map(. // "") | join(" ") | ascii_downcase) | test("review"))
              | .id' 2>/dev/null \
     | while read -r rid; do
         [ -z "$rid" ] && continue
         hermes kanban --board "$pid" complete "$rid" \
           && echo "$(date -Is) [info] project ${pid}: 自动放行 review-required 卡 ${rid}" \
           || echo "$(date -Is) [warn] project ${pid}: 自动放行卡 ${rid} 失败"
       done
  fi
  # 正常业务编排（产品→架构→dev→QA→release）已交给 orchestrator.py 状态机；
  # watchdog 只负责异常续跑/熔断/限流暂停/review 放行。
done
