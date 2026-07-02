#!/usr/bin/env bash
# Watchdog —— 异常续跑/熔断/限流暂停/review 放行（**不做正常业务编排**，那归 orchestrator.py）。
# 对应《02-从零开始操作手册.md》阶段 9。用 cron 每分钟跑：
#   * * * * * ~/platform/watchdog.sh >> ~/platform/watchdog.log 2>&1
set -euo pipefail

PLATFORM_DATA_ROOT="${PLATFORM_DATA_ROOT:-/data/projects}"
MAX_CONTINUATIONS="${MAX_CONTINUATIONS:-20}"   # 每项目续跑卡总上限，防永久失败任务无限续跑（#5）

# 审计写入器（续跑/熔断/放行落项目 audit.jsonl，Web UI 事件页可查）；缺文件则退化为 no-op。
# shellcheck source=/dev/null
if ! . "$(dirname "$0")/audit_lib.sh" 2>/dev/null; then audit_event() { :; }; fi

# 供应商限流暂停：monitor.sh 检测到 1305/额度耗尽时写 ${PLATFORM_DATA_ROOT}/.provider_pause
# （内含 until-epoch）。暂停期内不再起新续跑卡，与 orchestrator.py 的 provider_paused 一致——
# 否则限流期间狂建续跑卡只会把额度耗得更快、刷满看板。
provider_paused() {
  local f="${PLATFORM_DATA_ROOT}/.provider_pause"
  [ -f "$f" ] || return 1
  local until_epoch
  until_epoch=$(tr -dc '0-9' < "$f"); until_epoch="${until_epoch:-0}"
  [ "$(date +%s)" -lt "$until_epoch" ]
}

# 清理已过期的暂停标记。watchdog 每分钟跑，比 monitor（5min）/旧 glm_monitor（30min）更及时——
# 限流一恢复，下一轮 tick 即清掉 .provider_pause，无需任何人工 rm（替代上传的 glm 脚本职责）。
clear_expired_pause() {
  local f="${PLATFORM_DATA_ROOT}/.provider_pause" until_epoch
  [ -f "$f" ] || return 0
  until_epoch=$(tr -dc '0-9' < "$f" 2>/dev/null); until_epoch="${until_epoch:-0}"
  if [ "$(date +%s)" -ge "$until_epoch" ]; then
    rm -f "$f" 2>/dev/null && echo "$(date -Is) [info] watchdog: .provider_pause 已过期，清理（恢复起新任务）"
  fi
}
clear_expired_pause

# 余额耗尽（1113）是永久性故障，续跑/重试无意义（D13/D19）。monitor 检测到会写此标记；
# 充值后人工 rm 即恢复。billing dead 时不起新续跑卡（避免无效重试刷爆看板与额度）。
if [ -f "${PLATFORM_DATA_ROOT}/.provider_billing_dead" ]; then
  echo "$(date -Is) [warn] watchdog: 供应商余额耗尽（永久），跳过本轮续跑——需充值后 rm .provider_billing_dead"
  exit 0
fi

if provider_paused; then
  echo "$(date -Is) [info] watchdog: 供应商限流暂停期内，跳过本轮续跑（.provider_pause 生效）"
  exit 0
fi

for proj_dir in "${PLATFORM_DATA_ROOT}"/*/; do
  [ -d "$proj_dir" ] || continue
  pid=$(basename "$proj_dir")
  export HERMES_HOME="${proj_dir}.hermes"
  cnt_file="${proj_dir}workspace/.autocode/continuation_count"
  mkdir -p "${proj_dir}workspace/.autocode" 2>/dev/null || true

  # 全量看板快照（一次拉取，供去重与读取原任务上下文）
  all_json=$(hermes kanban --board "$pid" list --json 2>/dev/null || echo '[]')

  # D14 真机实测：tasks 的 last_event 字段在 list JSON 里**为空**（事件只进 task_events 表），
  # 只按 last_event 筛 → 匹配 0 条、续跑机制彻底失效。改为按可靠的 **status** 字段捞异常任务，
  # 并连同各 reason 字段一起取出做分类（环境/挂载、余额不足等"重试无意义"的不续跑）。
  echo "$all_json" \
   | jq -r '.[] | select(.status=="blocked" or .status=="failed" or .status=="gave_up"
                         or .status=="stale" or .status=="timed_out" or .status=="protocol_violation"
                         or .last_event=="gave_up" or .last_event=="timed_out"
                         or .last_event=="stale" or .last_event=="protocol_violation")
            | "\(.id)\t\(.assignee // "dev-worker-1")\t\(.workspace // "")\t\([.block_reason,.reason,.blocked_reason,.error,.message]|map(.//"")|join(" ")|ascii_downcase)"' \
   | while IFS=$'\t' read -r tid assignee workspace reason; do
       [ -z "$tid" ] && continue
       [ -z "$assignee" ] && assignee="dev-worker-1"
       # 分类：重试无意义的故障不续跑（避免 D18"反复 unblock 余额不足任务"的无效循环）。
       case "$reason" in
         *insufficient\ balance*|*1113*|*billing*|*no\ resource\ package*|*余额*)
           continue ;;  # 供应商余额耗尽：靠 monitor 的 .provider_billing_dead + 充值，续跑无意义
         *environment*|*mount*|*wrong\ workspace*|*foreign\ workspace*)
           # 环境/挂载错误：续跑必再失败 → 建一张排查卡（幂等）交人工/触发 docker guard，不盲目续跑
           hermes kanban --board "$pid" create "环境/挂载异常需排查：${tid}" \
             --assignee change-guardian --idempotency-key "wd-env-${tid}" \
             --body "任务 ${tid} 因环境/挂载异常 blocked（${reason}）。续跑无用，请排查 Docker 后端/跨项目挂载。" 2>/dev/null || true
           audit_event "$pid" watchdog env_anomaly "任务 ${tid} 环境/挂载异常，建排查卡（续跑无意义）：${reason}"
           continue ;;
       esac
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
           audit_event "$pid" watchdog continuation_cap "续跑达上限 ${MAX_CONTINUATIONS}（熔断），停止自动续跑，已建人工 review 卡"
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
       audit_event "$pid" watchdog continuation "任务 ${tid} 卡住 → 建续跑卡 ${new_tid}（第 $(( cnt + 1 ))/${MAX_CONTINUATIONS} 次）"
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
         if hermes kanban --board "$pid" complete "$rid"; then
           echo "$(date -Is) [info] project ${pid}: 自动放行 review-required 卡 ${rid}"
           audit_event "$pid" watchdog auto_approve_review "自动放行 review-required 卡 ${rid}（AUTOCODE_AUTO_APPROVE_REVIEW=1）"
         else
           echo "$(date -Is) [warn] project ${pid}: 自动放行卡 ${rid} 失败"
         fi
       done
  fi
  # 正常业务编排（产品→架构→dev→QA→release）已交给 orchestrator.py 状态机；
  # watchdog 只负责异常续跑/熔断/限流暂停/review 放行。
done
