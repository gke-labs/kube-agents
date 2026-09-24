/**
 * What the input box does with a line before anything touches the bus.
 * Commands are handled locally and never published. The trim and the byte
 * cap match the gateway's (a2a/gateway/console.go), so the page never shows
 * a turn as sent that the gateway would drop.
 */
import { CONSOLE_TEXT_CAP, textBytes } from "./console.ts";
import { STREAMS } from "./protocol.ts";
import { capacities, failuresOf, recentTasks, taskTimes } from "./derive.ts";
import type { UiState } from "./model.ts";

/** Go's unicode.IsSpace set, which strings.TrimSpace strips. Not JS \s. */
const GO_SPACE = "[\\t\\n\\v\\f\\r \\u0085\\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000]";
const GO_TRIM_RE = new RegExp(`^${GO_SPACE}+|${GO_SPACE}+$`, "g");
const COMMAND_PREFIX = "/";
const TASKS_SHOWN = 10;

export type Command =
  | { name: "new" }
  | { name: "replay"; session: string }
  | { name: "tasks" }
  | { name: "streams" }
  | { name: "clear" }
  | { name: "help" }
  | { name: "error"; text: string };

export type Classified =
  | { kind: "empty" }
  | { kind: "command"; command: Command }
  | { kind: "tooBig"; bytes: number }
  | { kind: "send"; text: string };

export const HELP_TEXT = [
  "/new - start a fresh conversation (the old one stays on the bus)",
  "/replay <session> - show one session's transcript in the dashboard",
  "/tasks - recent tasks and failures",
  "/streams - stream capacity and attach state",
  "/clear - clear this transcript (the bus is untouched)",
  "/help - this list",
  "Anything else goes to the agent.",
].join("\n");

export function goTrim(s: string): string {
  return s.replace(GO_TRIM_RE, "");
}

function parseCommand(line: string): Command {
  const [head, ...rest] = line.slice(COMMAND_PREFIX.length).split(/\s+/);
  switch (head) {
    case "new":
    case "tasks":
    case "streams":
    case "clear":
    case "help":
      return { name: head };
    case "replay":
      return rest[0] ? { name: "replay", session: rest[0] } : { name: "error", text: "usage: /replay <session>" };
    default:
      return {
        name: "error",
        text: `unknown command /${head}. Nothing was sent. /help lists the commands.`,
      };
  }
}

export function classifyInput(raw: string): Classified {
  const text = goTrim(raw);
  if (text === "") return { kind: "empty" };
  if (text.startsWith(COMMAND_PREFIX)) return { kind: "command", command: parseCommand(text) };
  const bytes = textBytes(text);
  if (bytes > CONSOLE_TEXT_CAP) return { kind: "tooBig", bytes };
  return { kind: "send", text };
}

export function tasksText(state: UiState): string {
  const recent = recentTasks(state, TASKS_SHOWN);
  if (recent.length === 0) return "no tasks on the bus yet";
  const lines = recent.map((t) => {
    const times = taskTimes(t);
    return `${t.taskId} ${t.state} -> ${t.addressee}${times ? ` (${times})` : ""}${t.reason ? `: ${t.reason}` : ""}`;
  });
  const failures = failuresOf(state).length;
  return [...lines, `${failures} failed in the retention window`].join("\n");
}

export function streamsText(state: UiState): string {
  const lines = capacities(state).map((c) => c.text);
  const up = STREAMS.filter((s) => state.streamAttach.get(s)?.error === null).length;
  return [`${up} of ${STREAMS.length} streams attached`, ...lines].join("\n");
}
