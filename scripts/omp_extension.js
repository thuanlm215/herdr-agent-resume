// herdr-agent-resume omp session hook
// Notes the main interactive OMP session on its Herdr terminal. The installer writes the script path.
import { spawnSync } from "node:child_process";

const script = __SCRIPT__;

export default function (omp) {
  // OMP shares imported modules with child sessions, but invokes each factory separately.
  let noted = null;
  let failed = null;
  let retryAt = 0;

  function note(_event, ctx) {
    if (!process.env.HC_TERMINAL_ID || ctx.agent?.kind !== "main" || ctx.mode !== "tui") return;
    try {
      const file = ctx.sessionManager.getSessionFile() || "";
      const id = ctx.sessionManager.getSessionId() || "";
      const cwd = ctx.sessionManager.getCwd() || ctx.cwd || "";
      const key = JSON.stringify([id, file, cwd]);
      if (key === noted || (key === failed && Date.now() < retryAt)) return;
      // A lazy session already has its final path. Record it before the first turn can be interrupted.
      // An in-memory /new has no resumable path: invalidate the old conversation instead.
      const payload = file && id
        ? { hook_event_name: "SessionStart", session_id: id, transcript_path: file, cwd }
        : { hook_event_name: "SessionClear" };
      failed = key;
      retryAt = Date.now() + 5000;
      const result = spawnSync("python3", [script, "hook", "omp"], {
        input: JSON.stringify(payload),
        timeout: 8000,
        maxBuffer: 65536,
        encoding: "utf8",
      });
      if (result.status === 0 && String(result.stdout || "").trim() === "saved") {
        noted = key;
        failed = null;
      }
    } catch {
      // A missing helper, unavailable core, or session teardown must not interrupt OMP.
    }
  }

  omp.on("session_start", note);
  omp.on("session_switch", note);
  omp.on("session_branch", note);
  omp.on("agent_start", note);
  omp.on("message_end", note);
}
