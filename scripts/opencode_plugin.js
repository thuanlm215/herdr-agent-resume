// herdr-agent-resume opencode session hook
// Notes this OpenCode session on its Herdr terminal. The installer writes the script path.
import { spawnSync } from "node:child_process";

const script = __SCRIPT__;
let noted = "";

// session.created covers new sessions; session.updated/idle cover resumed ones,
// whose "created" event fired before this plugin could see them.
const EVENTS = new Set(["session.created", "session.updated", "session.idle"]);

function sessionId(p) {
  return (p && p.info && p.info.id) || (p && p.sessionID) || "";
}

function note(ctx) {
  return async ({ event }) => {
    try {
      const p = event && event.properties;
      if (!EVENTS.has(event && event.type) || !p) return;
      const id = sessionId(p);
      if (!id) return;
      const cwd = (p.info && p.info.directory) || ctx.directory || "";
      const key = id + "\0" + cwd;
      if (key === noted) return;
      const result = spawnSync("python3", [script, "hook", "opencode"], {
        input: JSON.stringify({
          hook_event_name: "SessionStart",
          session_id: id,
          cwd: cwd || undefined,
        }),
        timeout: 8000,
        encoding: "utf8",
      });
      if (result && result.status === 0 && String(result.stdout || "").trim()) noted = key;
    } catch {
      // Never let resume tracking get in the agent's way.
    }
  };
}

export const HerdrAgentResume = async (ctx) => ({
  event: note(ctx),
});
