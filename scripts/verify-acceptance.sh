#!/usr/bin/env bash
# 六项端到端验收复验器 —— 把《03》Step 8 的"全过才算落地"六项做成可机械执行的检查，
# 不再靠人记得手动跑。用法（需平台已部署、项目已建）：
#   scripts/verify-acceptance.sh demo1 demo2
# 输出末尾是机器可读 JSON 汇总。能自动判定的直接 pass/fail；需真机长跑的标 manual。
#
# 说明：本脚本是【运行期运维工具】，不在 CI 跑（CI 没有真实 Hermes）。它做确定性的
# 文件/配置/ git 断言 + 调 hook_canary 验设计闸门；自动推进与跨 90 轮需观察长跑，标 manual。
set -uo pipefail

PID1="${1:-demo1}"
PID2="${2:-demo2}"
PLATFORM_DATA_ROOT="${PLATFORM_DATA_ROOT:-/data/projects}"
PLATFORM_HOME="${PLATFORM_HOME:-$HOME/platform}"

r_isolation="fail"; r_ceo="fail"; r_parallel="fail"; r_gate="fail"
r_auto="manual"; r_long="manual"

p1="${PLATFORM_DATA_ROOT}/${PID1}"
p2="${PLATFORM_DATA_ROOT}/${PID2}"

echo "== 验收点 1：双项目隔离 =="
db1="${p1}/.hermes" ; db2="${p2}/.hermes"
if [ -d "${db1}" ] && [ -d "${db2}" ]; then
  # 两套 kanban.db 各自存在且不是同一文件；board 列表互不可见。
  f1=$(find "${db1}" -name 'kanban.db' 2>/dev/null | head -1)
  f2=$(find "${db2}" -name 'kanban.db' 2>/dev/null | head -1)
  if [ -n "${f1}" ] && [ -n "${f2}" ] && [ "${f1}" != "${f2}" ]; then
    seen=$(HERMES_HOME="${db1}" hermes kanban --board "${PID2}" list --json 2>/dev/null | jq 'length' 2>/dev/null || echo 0)
    [ "${seen:-0}" = "0" ] && r_isolation="pass"
  fi
  echo "  ${PID1}/${PID2} 各自 kanban.db：${f1:-none} / ${f2:-none} → ${r_isolation}"
else
  echo "  需要先创建 ${PID1} 与 ${PID2} 两个项目（缺一）→ ${r_isolation}"
fi

echo "== 验收点 2：CEO 不干活（disabled_toolsets 含 code_execution）=="
cfg="${p1}/.hermes/profiles/ceo/config.yaml"
if [ -f "${cfg}" ] && grep -A3 disabled_toolsets "${cfg}" 2>/dev/null | grep -q code_execution; then
  r_ceo="pass"
fi
echo "  ${cfg} → ${r_ceo}"

echo "== 验收点 4：并行不冲突（worktree + allowed_paths + 提交落地）=="
ws1="${p1}/workspace"
if [ -d "${ws1}/.git" ]; then
  wt=$(find "${ws1}/.worktrees" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')
  ap=$(find "${ws1}/design" -name 'allowed_paths.t_*.txt' 2>/dev/null | wc -l | tr -d ' ')
  commits=$(git -C "${ws1}" rev-list --all --count 2>/dev/null || echo 0)
  echo "  worktrees=${wt} allowed_paths=${ap} commits=${commits}"
  # 至少一个 worktree、一个 allowed_paths、且 init 之外有提交（产物真落地）。
  if [ "${wt:-0}" -ge 1 ] && [ "${ap:-0}" -ge 1 ] && [ "${commits:-0}" -gt 1 ]; then
    r_parallel="pass"
  fi
else
  echo "  ${ws1} 不是 git 仓库（先建项目并跑开发阶段）"
fi
echo "  → ${r_parallel}"

echo "== 验收点 5：设计闸门（hook_canary block 探针）=="
if [ -x "${PLATFORM_HOME}/hook_canary.sh" ]; then
  if "${PLATFORM_HOME}/hook_canary.sh" "${PID1}" 2>&1 | grep -q '\[ok\]'; then
    r_gate="pass"
  fi
else
  echo "  未找到 ${PLATFORM_HOME}/hook_canary.sh"
fi
echo "  → ${r_gate}"

echo "== 验收点 3 / 6：自动推进 / 跨 90 轮 =="
echo "  这两项需观察真机长跑，无法在本脚本一次性判定，标 manual："
echo "  - 自动推进：confirm-plan 后不人工干预，watch orchestrator.log 直到 stage=complete。"
echo "  - 跨 90 轮：投一个需 >90 工具调用的任务，确认 goal-mode/watchdog 平滑续跑不静默停。"

cat <<JSON

==== 验收汇总（机器可读）====
{
  "isolation": "${r_isolation}",
  "ceo_no_code": "${r_ceo}",
  "auto_progress": "${r_auto}",
  "parallel_no_conflict": "${r_parallel}",
  "design_gate": "${r_gate}",
  "over_90_turns": "${r_long}"
}
JSON

# 任一确定性检查 fail → 退出码非 0，便于 CI/运维门禁串联。
case "${r_isolation}${r_ceo}${r_parallel}${r_gate}" in
  passpasspasspass) exit 0 ;;
  *) exit 1 ;;
esac
