#!/usr/bin/env bash
# mail-hook-check — 供各 agent 生命周期 hook 调用的未读邮件检查器。
# 有未读时输出一段提示（供 hook 注入 agent 上下文），无未读或任何异常都静默退出 0。
#
# 用法: mail-hook-check <agent> [instance]
#   agent    逻辑 agent 名（codex / kimi / zcode / qodercn ...）
#   instance 实例名（默认 main；远程机器建议用机器标识，如 winjd）
set -u

agent="${1:-}"
instance="${2:-main}"
[ -n "$agent" ] || exit 0

TOOLS="$HOME/agent-mail-tools"
[ -x "$TOOLS/mail-recv" ] || exit 0

project="$PWD"
out="$("$TOOLS/mail-recv" --agent "$agent" --instance "$instance" --project "$project" --unread 2>/dev/null)" || exit 0
[ -z "$out" ] && exit 0
case "$out" in
  "(no messages)"*) exit 0 ;;
esac

# 只注入摘要（主题行），正文让 agent 自己用 mail-recv 拉，避免每轮注入过多 token
summary="$(printf '%s\n' "$out" | grep '^--- #' | head -5)"
count="$(printf '%s\n' "$out" | grep -c '^--- #' || true)"

text="[agent-mail] 你有 $count 条未读消息（项目: $project）：
$summary
请先用 $TOOLS/mail-recv --agent $agent --instance $instance --project \"$project\" 查看全文并处理，处理后加 --ack 标记已读。"

# codex 的 UserPromptSubmit hook 要求 stdout 是 JSON：
# {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "<注入文本>"}}
# 纯文本会报 "hook returned invalid user prompt submit JSON output"。
MSG="$text" python3 - <<'PY' 2>/dev/null || exit 0
import json, os
print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
      "additionalContext": os.environ["MSG"]}}, ensure_ascii=False))
PY
exit 0
