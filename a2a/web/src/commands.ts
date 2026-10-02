/**
 * What the input box does with a line before anything touches the bus.
 * Commands are handled locally and never published. The trim and the byte
 * cap match the gateway's (a2a/gateway/console.go), so the page never shows
 * a turn as sent that the gateway would drop.
 */
import { CONSOLE_TEXT_CAP, goTrim, textBytes } from "./console.ts";
import { STREAMS } from "./protocol.ts";
import { capacities, failuresOf, recentTasks, taskTimes } from "./derive.ts";
import type { UiState } from "./model.ts";

export { goTrim };

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
  // Ordered consumers survive a reconnect and their iterators never end, so
  // an attach recorded before the drop still reads as attached: while the
  // link is down the count above would be stale, not current.
  if (state.connection !== "up") {
    return ["streams unknown - the bus link is down", ...lines].join("\n");
  }
  const up = STREAMS.filter((s) => state.streamAttach.get(s)?.error === null).length;
  return [`${up} of ${STREAMS.length} streams attached`, ...lines].join("\n");
}

/** What App does with a command. Only `new`, `replay` and `clear` touch state beyond a line. */
export type Effect =
  | { kind: "local"; text: string }
  | { kind: "new" }
  | { kind: "replay"; session: string }
  | { kind: "clear" };

export function tooBigText(bytes: number): string {
  return `not sent: ${bytes} bytes is over the gateway's ${CONSOLE_TEXT_CAP}-byte limit. Trim it and send again.`;
}

export function commandEffect(command: Command, state: UiState): Effect {
  switch (command.name) {
    case "new":
    case "clear":
      return { kind: command.name };
    case "replay":
      if (!state.agents.has(command.session)) {
        return {
          kind: "local",
          text: `no session named ${command.session} on the bus. The sessions panel lists the ones seen.`,
        };
      }
      return { kind: "replay", session: command.session };
    case "tasks":
      return { kind: "local", text: tasksText(state) };
    case "streams":
      return { kind: "local", text: streamsText(state) };
    case "help":
      return { kind: "local", text: HELP_TEXT };
    case "error":
      return { kind: "local", text: command.text };
  }
}
