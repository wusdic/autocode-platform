#!/usr/bin/env bash
# 模型可用性预检 —— 建项目前对每个 provider+model 发一次最小请求，早发现 key 错/模型名错/限流，
# 而不是跑到流水线中途某个角色才 401/模型不存在（"双模型交叉质疑"被静默破坏）。
#
# 退出码：
#   硬错误（401/403 鉴权失败、400/404 模型名错）→ 非 0（应拒绝建项目）。
#   429 限流 / 网络超时 / 无 key → 警告但返回 0（限流会自愈，不应挡建项目）。
#   AUTOCODE_MODEL_PREFLIGHT_STRICT=1 时警告也算失败。
#
# 用法：bash check-models.sh   （供 launch_project.sh 调用，也可单独跑）
set -uo pipefail

ENV_FILE="${HERMES_ENV_FILE:-$HOME/.hermes/.env}"
_key() {  # 取 API key：先环境变量，再 ~/.hermes/.env
  local v="${!1:-}"
  if [ -n "$v" ]; then printf '%s' "$v"; return; fi
  [ -f "$ENV_FILE" ] && sed -n "s/^$1=//p" "$ENV_FILE" | head -1
}

ZAI_BASE_URL="${ZAI_BASE_URL:-https://api.z.ai/api/coding/paas/v4}"
ZAI_PRIMARY_MODEL="${ZAI_PRIMARY_MODEL:-glm-5.2}"
ZAI_SECONDARY_MODEL="${ZAI_SECONDARY_MODEL:-glm-5.2}"
DEEPSEEK_BASE_URL="${DEEPSEEK_BASE_URL:-https://api.deepseek.com/v1}"
DEEPSEEK_MODEL="${DEEPSEEK_MODEL:-deepseek-chat}"
STRICT="${AUTOCODE_MODEL_PREFLIGHT_STRICT:-0}"
TIMEOUT="${MODEL_PREFLIGHT_TIMEOUT:-15}"

hard=0; warn=0
check() {  # label base_url model key
  local label="$1" base="$2" model="$3" key="$4" code
  if [ -z "$key" ]; then echo "⚠️  ${label}: 无 API key，跳过（限流自愈/网络问题不阻断）"; warn=$((warn+1)); return; fi
  local body_file; body_file="$(mktemp)"
  code=$(curl -s -o "$body_file" -w '%{http_code}' --max-time "$TIMEOUT" \
    -X POST "${base%/}/chat/completions" \
    -H "Authorization: Bearer ${key}" -H "Content-Type: application/json" \
    -d "{\"model\":\"${model}\",\"messages\":[{\"role\":\"user\",\"content\":\"1\"}],\"max_tokens\":1}" 2>/dev/null) || code=000
  local body; body="$(cat "$body_file" 2>/dev/null)"; rm -f "$body_file"
  case "$code" in
    200) echo "✅ ${label}: ${model} 可用" ;;
    401|403) echo "❌ ${label}: 鉴权失败(${code})，API key 错误/无权限"; hard=$((hard+1)) ;;
    400|404) echo "❌ ${label}: 模型不可用(${code})，模型名 '${model}' 可能错误"; hard=$((hard+1)) ;;
    429)
      # 解析 body 区分：1113 余额不足（永久，硬错误 fail-fast）vs 1305 临时过载（可自愈，警告）。
      if grep -Eiq '"code"[[:space:]]*:[[:space:]]*"?1113|insufficient balance|no resource package' <<<"$body"; then
        echo "❌ ${label}: 余额不足(1113)。注意：这是项目 gateway/worker 用的 key，不是你当前聊天会话的 key。"
        hard=$((hard+1))
      else
        echo "⚠️  ${label}: 临时过载/限流(429/1305)——会自愈，不算硬错误"; warn=$((warn+1))
      fi ;;
    *) echo "⚠️  ${label}: 请求未成功(${code})，网络/超时？暂不阻断"; warn=$((warn+1)) ;;
  esac
}

check "ZAI/primary"   "$ZAI_BASE_URL" "$ZAI_PRIMARY_MODEL" "$(_key GLM_API_KEY)"
[ "$ZAI_SECONDARY_MODEL" != "$ZAI_PRIMARY_MODEL" ] && \
  check "ZAI/secondary" "$ZAI_BASE_URL" "$ZAI_SECONDARY_MODEL" "$(_key GLM_API_KEY)"
check "DeepSeek"      "$DEEPSEEK_BASE_URL" "$DEEPSEEK_MODEL" "$(_key DEEPSEEK_API_KEY)"

if [ "$hard" -gt 0 ]; then echo "❌ 模型预检失败：${hard} 个硬错误（key/模型名）"; exit 1; fi
if [ "$STRICT" = "1" ] && [ "$warn" -gt 0 ]; then echo "❌ 严格模式：${warn} 个警告视为失败"; exit 1; fi
echo "✅ 模型预检通过（warn=${warn}）"
exit 0
