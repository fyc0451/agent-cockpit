import { execFileSync } from "node:child_process";
import path from "node:path";
import os from "node:os";

const TOOLS = path.join(os.homedir(), ".local", "bin");
const INJECT = path.join(TOOLS, "mail-identity-inject");
const CHECK = path.join(TOOLS, "mail-hook-check");

function contextFrom(command) {
  let raw;
  try {
    raw = execFileSync(command, [], {
      timeout: 12000, encoding: "utf-8", cwd: process.cwd(), shell: false,
    });
  } catch {
    return "";
  }
  if (!raw || raw.length > 8192) return "";
  let value;
  try {
    value = JSON.parse(raw);
  } catch {
    return "";
  }
  const output = value?.hookSpecificOutput;
  const context = output?.additionalContext;
  if (
    value === null || typeof value !== "object" || Array.isArray(value)
    || output === null || typeof output !== "object" || Array.isArray(output)
    || typeof context !== "string" || context.length === 0 || context.length > 4096
  ) return "";
  return context;
}

export const AgentMailPlugin = async ({ client }) => ({
  event: async ({ event }) => {
    if (event?.type !== "session.created") return;
    const info = event?.properties?.info;
    if (!info?.id || info.parentID) return;
    const context = contextFrom(INJECT);
    if (!context) return;
    try {
      await client.session.prompt({
        path: { id: info.id },
        body: { noReply: true, parts: [{ type: "text", text: context }] },
      });
    } catch {
      // Agent Mail failure must not prevent OpenCode from creating the session.
    }
  },
  "chat.message": async () => {
    const context = contextFrom(CHECK);
    if (context) {
      return { message: { parts: [{ type: "text", text: context }] } };
    }
  },
});
