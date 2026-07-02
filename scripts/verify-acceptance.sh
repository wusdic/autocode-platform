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

echo "== 验收点 2：CEO 不干活（disabled_toolsets 含 code_execution + execute_code）=="
cfg="${p1}/.hermes/profiles/ceo/config.yaml"
if [ -f "${cfg}" ]; then
  _dis=$(grep -A4 disabled_toolsets "${cfg}" 2>/dev/null || true)
  grep -q code_execution <<<"${_dis}" && grep -q execute_code <<<"${_dis}" && r_ceo="pass"
fi
echo "  ${cfg} → ${r_ceo}（须同时含 code_execution 与 execute_code）"

echo "== 验收点 2b：Docker 跨项目挂载隔离（容器声明项目 == 实际挂载 workspace）=="
r_docker="manual"
if command -v docker >/dev/null 2>&1; then
  r_docker="pass"
  for _cid in $(docker ps -q --filter 'name=hermes-' 2>/dev/null); do
    _pe=$(docker inspect "${_cid}" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | sed -n 's/^AUTOCODE_PROJECT_ID=//p' | head -1)
    [ -n "${_pe}" ] || continue
    for _m in $(docker inspect "${_cid}" --format '{{range .Mounts}}{{println .Source}}{{end}}' 2>/dev/null | sed -n 's#.*/data/projects/\([^/]*\)/workspace.*#\1#p' | sort -u); do
      [ "${_m}" = "${_pe}" ] || { echo "  ❌ 容器 ${_cid} 声明 ${_pe} 却挂了 ${_m}"; r_docker="fail"; }
    done
  done
  echo "  → ${r_docker}"
else
  echo "  docker 不可用，标 manual"
fi

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

echo "== 验收点 7（第十轮）：worker profile 校验（YAML 解析，不用 grep）=="
r_worker="fail"
_PYBIN="${PLATFORM_HOME}/venv/bin/python"; [ -x "${_PYBIN}" ] || _PYBIN="$(command -v python3)"
_wok=1
for _r in dev-worker-1 dev-worker-2 qa release; do
  "${_PYBIN}" - "${p1}/.hermes/profiles/${_r}/config.yaml" "${_r}" <<'PY' || _wok=0
import sys, yaml, pathlib
cfg, role = sys.argv[1], sys.argv[2]
d = yaml.safe_load(pathlib.Path(cfg).read_text()) if pathlib.Path(cfg).exists() else {}
ts = d.get("toolsets") or []
ok = (isinstance(ts, list) and {"kanban", "terminal", "file"}.issubset(set(ts))
      and "execute_code" in ((d.get("agent") or {}).get("disabled_toolsets") or []))
print(f"  {'✅' if ok else '❌'} {role}: toolsets/disabled_toolsets")
sys.exit(0 if ok else 1)
PY
done
[ "${_wok}" = 1 ] && r_worker="pass"
echo "  → ${r_worker}"

echo "== 验收点 8（第十轮）：executor 后端 / degraded / provider 状态 =="
r_backend="manual"; r_provider="pass"
_bj="${p1}/workspace/.autocode/executor_backend.json"
if [ -f "${_bj}" ]; then
  r_backend=$(jq -r '.backend + (if .degraded then " (degraded)" else "" end)' "${_bj}" 2>/dev/null || echo "?")
fi
if [ -f "${PLATFORM_DATA_ROOT}/.provider_billing_dead" ]; then
  r_provider="fail"
  echo "  ❌ 供应商余额耗尽标记存在（.provider_billing_dead）——先充值并 rm 该文件，再谈流程验收"
fi
echo "  backend=${r_backend} provider=${r_provider}"

echo "== 验收点 9（第十轮）：交付/审计 API（需控制平面运行 + token）=="
r_deliv="manual"; r_unattended="manual"; r_audit="manual"
_TOKEN="${PLATFORM_TOKEN:-$(cat "${PLATFORM_HOME}/.platform_token" 2>/dev/null || true)}"
if [ -n "${_TOKEN}" ] && curl -s -o /dev/null -m 5 "http://127.0.0.1:9000/api/projects" -H "X-Token: ${_TOKEN}"; then
  _dl=$(curl -s -m 10 "http://127.0.0.1:9000/api/projects/${PID1}/deliverable" -H "X-Token: ${_TOKEN}" 2>/dev/null)
  if [ -n "${_dl}" ]; then
    [ "$(jq -r '.is_done' <<<"${_dl}" 2>/dev/null)" = "true" ] && r_deliv="pass" || r_deliv="fail"
    # 项目目标是"自然无人值守完成"：is_done 但有人工干预/降级 → unattended fail（区分 operator-assisted）
    [ "$(jq -r '.is_unattended_done' <<<"${_dl}" 2>/dev/null)" = "true" ] && r_unattended="pass" || r_unattended="fail"
    echo "  deliverable: is_done=$(jq -r '.is_done' <<<"${_dl}") unattended=$(jq -r '.is_unattended_done' <<<"${_dl}") interventions=$(jq -r '.manual_interventions' <<<"${_dl}") degraded=$(jq -r '.degraded' <<<"${_dl}")"
  fi
  _au=$(curl -s -m 10 "http://127.0.0.1:9000/api/projects/${PID1}/audit" -H "X-Token: ${_TOKEN}" 2>/dev/null)
  if [ -n "${_au}" ]; then
    _acts=$(jq -r '[.events[].action] | join(",")' <<<"${_au}" 2>/dev/null || echo "")
    case "${_acts}" in
      *project_created*stage_transition*|*stage_transition*project_created*) r_audit="pass" ;;
      *) r_audit="fail" ;;
    esac
    echo "  audit: actions 含 project_created+stage_transition → ${r_audit}"
  fi
else
  echo "  控制平面不可达或无 token，标 manual（验收完整交付时应在部署机上跑）"
fi

echo "== 验收点 3 / 6：自动推进 / 跨 90 轮 =="
echo "  这两项需观察真机长跑，无法在本脚本一次性判定，标 manual："
echo "  - 自动推进：confirm-plan 后不人工干预，watch orchestrator.log 直到 stage=complete。"
echo "  - 跨 90 轮：投一个需 >90 工具调用的任务，确认 goal-mode/watchdog 平滑续跑不静默停。"

cat <<JSON

==== 验收汇总（机器可读）====
{
  "isolation": "${r_isolation}",
  "docker_mount_isolation": "${r_docker}",
  "ceo_no_code": "${r_ceo}",
  "worker_profiles": "${r_worker}",
  "executor_backend": "${r_backend}",
  "provider_status": "${r_provider}",
  "auto_progress": "${r_auto}",
  "parallel_no_conflict": "${r_parallel}",
  "design_gate": "${r_gate}",
  "deliverable": "${r_deliv}",
  "unattended_completion": "${r_unattended}",
  "audit_trail": "${r_audit}",
  "over_90_turns": "${r_long}"
}
JSON

# 任一确定性检查 fail → 退出码非 0，便于 CI/运维门禁串联。
# （deliverable/unattended/audit 为 manual 时不拦：项目未跑完时也能做部署面验收。）
for _v in "${r_isolation}" "${r_ceo}" "${r_parallel}" "${r_gate}" "${r_worker}" "${r_provider}"; do
  [ "${_v}" = "fail" ] && exit 1
done
[ "${r_docker}" = "fail" ] && exit 1
[ "${r_deliv}" = "fail" ] && exit 1
[ "${r_unattended}" = "fail" ] && exit 1
[ "${r_audit}" = "fail" ] && exit 1
exit 0
