// agent-mail.js — opencode 插件:agent-mail 身份注入 + 未检查信。
// 对标 codex/kimi 的 SessionStart(mail-identity-inject)+ UserPromptSubmit(mail-hook-check)。
//
// 监听:
//   session.created → 跑 mail-identity-inject opencode(注入身份告知)
//   chat.message    → 跑 mail-hook-check opencode main(查未读)
//
// 用 child_process 同步执行 shell 脚本,失败静默(opencode 不应因 mail 挂)。
import { execSync } from "node:child_process";
import path from "node:path";
import os from "node:os";

const AGENT = "opencode";
const TOOLS = path.join(os.homedir(), "agent-mail-tools");
const INJECT = path.join(TOOLS, "mail-identity-inject");
const CHECK = path.join(TOOLS, "mail-hook-check");

function runScript(cmd) {
  try {
    const out = execSync(cmd, { timeout: 12000, encoding: "utf-8", cwd: process.cwd() });
    return out.trim();
  } catch {
    return "";
  }
}

// 从 mail-hook-check 的 JSON 输出取 additionalContext(纯文本)
function extractContext(jsonOut) {
  if (!jsonOut) return "";
  try {
    const d = JSON.parse(jsonOut);
    return d?.hookSpecificOutput?.additionalContext || "";
  } catch {
    // 非 JSON(可能是纯文本),直接返回
    return jsonOut;
  }
}

export const AgentMailPlugin = async () => {
  return {
    // session.created:启动/新会话/clear 时注入身份
    event: async ({ event }) => {
      const type = event?.type;
      if (type === "session.created") {
        const out = runScript(`"${INJECT}" ${AGENT}`);
        // opencode 的 event handler 不直接返回 context,
        // 但身份注入的输出会进 stderr/stdout,opencode 可能不捕获。
        // 作为补充,身份主要靠 chat.message 时的 check 顺便带出。
      }
    },
    // chat.message:用户提交消息时查未读 + 注入身份
    "chat.message": async ({ sessionID }) => {
      // 查未读消息
      const out = runScript(`"${CHECK}" ${AGENT} main`);
      const ctx = extractContext(out);
      // opencode 插件的 chat.message 返回值可以修改消息内容
      // 但更可靠的是:如果有未读,把摘要注入(通过返回 text part)
      if (ctx) {
        return {
          message: { parts: [{ type: "text", text: ctx }] },
        };
      }
    },
  };
};
