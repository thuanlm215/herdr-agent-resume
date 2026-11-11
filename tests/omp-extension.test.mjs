import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../scripts/omp_extension.js", import.meta.url), "utf8")
  .replace('import { spawnSync } from "node:child_process";', "")
  .replace("export default function", "globalThis.factory = function");

function harness() {
  const calls = [];
  let now = 0;
  let response = { status: 0, stdout: "saved\n" };
  const sandbox = {
    __SCRIPT__: "/synthetic/agent_resume.py",
    process: { env: { HC_TERMINAL_ID: "t7" } },
    Date: { now: () => now },
    spawnSync(_program, _args, options) {
      calls.push(JSON.parse(options.input));
      if (response instanceof Error) throw response;
      return response;
    },
  };
  vm.runInNewContext(source, sandbox);
  return {
    calls,
    tick() { now += 5001; },
    respond(value) { response = value; },
    session(kind = "main", mode = "tui") {
      const state = { id: "synthetic-first", file: "/synthetic/first.jsonl", cwd: "/work" };
      const context = {
        agent: { kind }, mode,
        sessionManager: {
          getSessionId: () => state.id,
          getSessionFile: () => state.file,
          getCwd: () => state.cwd,
        },
      };
      const handlers = new Map();
      sandbox.factory({ on: (event, handler) => handlers.set(event, handler) });
      return { state, emit: event => handlers.get(event)({}, context) };
    },
  };
}

test("records allocated first-turn path before completion, then follows switches and forks", () => {
  const h = harness();
  const session = h.session();
  session.emit("session_start");
  session.emit("agent_start");
  session.emit("message_end");
  assert.deepEqual(h.calls, [{
    hook_event_name: "SessionStart", session_id: "synthetic-first",
    transcript_path: "/synthetic/first.jsonl", cwd: "/work",
  }]);
  session.state.id = "synthetic-second";
  session.state.file = "/synthetic/second.jsonl";
  session.emit("session_switch");
  session.state.id = "synthetic-branch";
  session.state.file = "/synthetic/branch.jsonl";
  session.emit("session_branch");
  session.state.file = "/moved/branch.jsonl";
  session.state.cwd = "/moved";
  session.emit("message_end");
  assert.deepEqual(h.calls.slice(1).map(call => [call.session_id, call.transcript_path, call.cwd]), [
    ["synthetic-second", "/synthetic/second.jsonl", "/work"],
    ["synthetic-branch", "/synthetic/branch.jsonl", "/work"],
    ["synthetic-branch", "/moved/branch.jsonl", "/moved"],
  ]);
});

test("empty /new invalidates old session and the first new message can be recorded", () => {
  const h = harness();
  const session = h.session();
  session.emit("session_start");
  session.state.id = "synthetic-new";
  session.state.file = "";
  session.emit("session_switch");
  assert.deepEqual(h.calls.at(-1), { hook_event_name: "SessionClear" });
  session.state.file = "/synthetic/new.jsonl";
  session.emit("agent_start");
  assert.equal(h.calls.at(-1).session_id, "synthetic-new");
  assert.equal(h.calls.at(-1).transcript_path, "/synthetic/new.jsonl");
});

test("only main TUI sessions write labels, and factories never share acknowledgement state", () => {
  const h = harness();
  for (const [kind, mode] of [["sub", "tui"], ["main", "rpc"], ["main", "print"], ["main", "json"]]) {
    const excluded = h.session(kind, mode);
    excluded.emit("session_start");
    excluded.emit("message_end");
  }
  assert.deepEqual(h.calls, []);
  h.session().emit("session_start");
  h.session().emit("session_start");
  assert.deepEqual(h.calls.map(call => call.session_id), ["synthetic-first", "synthetic-first"]);
});

test("failure text and thrown helpers are nonfatal, rate-limited, and never acknowledged as saved", () => {
  const h = harness();
  const session = h.session();
  for (const response of [{ status: 0, stdout: "agent process not found\n" }, new Error("missing helper")]) {
    h.respond(response);
    session.emit("message_end");
    const attempts = h.calls.length;
    session.emit("message_end");
    assert.equal(h.calls.length, attempts);
    h.tick();
  }
  h.respond({ status: 0, stdout: "saved\n" });
  session.emit("message_end");
  assert.deepEqual(h.calls.map(call => call.session_id), ["synthetic-first", "synthetic-first", "synthetic-first"]);
  h.tick();
  session.emit("message_end");
  assert.equal(h.calls.length, 3);
});
