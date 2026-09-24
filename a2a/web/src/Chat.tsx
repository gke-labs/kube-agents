/**
 * The chat pane: the transcript, an input box for the console user, and a
 * footer with who we are connected as and the verify button.
 *
 * The input only renders for a user that can publish a turn. As `web` the
 * pane is read-only, and the footer's verify button is where that stops
 * being an assertion: it publishes one probe and surfaces the server's
 * refusal. As `console` the same button proves the credential can't write
 * anywhere on `a2a.>`.
 *
 * Lines are classified before anything touches the bus (commands.ts). A
 * slash line is never published.
 */
import { useState, type KeyboardEvent } from "react";
import type { ChatEntry, ProbeResult } from "./model.ts";
import { READ_ONLY_USER } from "./config.ts";
import { classifyInput, tooBigText, type Command } from "./commands.ts";
import Transcript from "./Transcript.tsx";

const EMPTY_READ_ONLY = "watching the bus - ask the agent something in chat";
const EMPTY_CONSOLE = "watching the bus - type below to ask the agent something, or /help";
const INPUT_ROWS = 2;
/**
 * Some WebKit versions report `isComposing: false` on the Enter that commits
 * an IME composition, but still carry the legacy `keyCode` for it - this is
 * the fallback check for those.
 */
const IME_COMMIT_KEYCODE = 229;

interface ChatProps {
  entries: ChatEntry[];
  /** The connected NATS user - the read-only badge only vouches for `web`. */
  user: string;
  /** The console conversation this tab speaks in; absent for the read-only view. */
  conversation?: string;
  probe?: ProbeResult;
  probePending?: boolean;
  onProbe: () => void;
  /** True if the turn was accepted; the box is cleared only then. */
  onSend?: (text: string) => boolean;
  onCommand?: (command: Command) => void;
}

function probeText(probe: ProbeResult): string {
  switch (probe.outcome) {
    case "refused":
      return `server refused the publish: ${probe.detail}`;
    case "sent":
      return `PUBLISH WENT THROUGH - ${probe.detail}`;
    case "error":
      return `probe failed before the server saw it: ${probe.detail}`;
  }
}

export default function Chat({
  entries,
  user,
  conversation,
  probe,
  probePending,
  onProbe,
  onSend,
  onCommand,
}: ChatProps) {
  const [draft, setDraft] = useState("");
  const canSend = onSend !== undefined && user !== READ_ONLY_USER;

  const submit = () => {
    const c = classifyInput(draft);
    switch (c.kind) {
      case "empty":
        return;
      case "tooBig":
        // Keep the text so it can be cut down.
        onCommand?.({ name: "error", text: tooBigText(c.bytes) });
        return;
      case "command":
        onCommand?.(c.command);
        setDraft("");
        return;
      case "send":
        // Only clear the box once the turn was actually accepted - refused
        // (the link is down) or not, the caller decides, and a refused turn
        // must stay in the box rather than vanish.
        if (onSend?.(c.text) === true) setDraft("");
        return;
    }
  };

  const handleKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    // An Enter during IME composition picks a candidate; it is not a submit.
    // Some WebKit versions clear isComposing early but still report the
    // legacy keyCode for it.
    if (e.key !== "Enter" || e.shiftKey || e.nativeEvent.isComposing || e.keyCode === IME_COMMIT_KEYCODE) return;
    e.preventDefault();
    submit();
  };

  return (
    <div className="chat-pane">
      <Transcript entries={entries} empty={canSend ? EMPTY_CONSOLE : EMPTY_READ_ONLY} />
      {canSend && (
        <textarea
          className="chat-input"
          rows={INPUT_ROWS}
          value={draft}
          placeholder="ask the agent, or /help"
          aria-label="message"
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={handleKey}
        />
      )}
      <div className="probe-bar">
        <span className="probe-label">
          connected as <code>{user}</code>
          {user === READ_ONLY_USER ? " · read-only" : ""}
          {conversation && (
            <>
              {" · conversation "}
              <code>{conversation}</code>
            </>
          )}
        </span>
        <button type="button" className="probe-button" onClick={onProbe} disabled={probePending}>
          {probePending ? "verifying…" : "verify"}
        </button>
        {probe && <span className={`probe-result probe-${probe.outcome}`}>{probeText(probe)}</span>}
      </div>
    </div>
  );
}
