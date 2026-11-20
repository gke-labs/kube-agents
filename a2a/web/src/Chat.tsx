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
 * Lines are classified before anything touches the bus (commands.ts). The
 * page's own command words are never published; any other line, `/session`
 * included, is sent to the gateway.
 */
import { useRef, useState, type KeyboardEvent } from "react";
import type { ChatEntry, ProbeResult } from "./model.ts";
import { READ_ONLY_USER } from "./config.ts";
import { classifyInput, tooBigText, type Command } from "./commands.ts";
import Transcript, { replyLabel } from "./Transcript.tsx";
import { turnFate } from "./console.ts";

const EMPTY_READ_ONLY = "watching the bus - ask the agent something in chat";
const EMPTY_CONSOLE = "watching the bus - type below to ask the agent something, or /help";
const INPUT_ROWS = 2;
/**
 * The one turn the footer sends on a click: the gateway answers it with the
 * install's usage and feedback block (a2a/gateway/feedback_summary.go), the
 * third surface of #3428 after the script and the chat command. It goes
 * through onSend like a typed line, so the link rules and the settled
 * classification (console.ts) apply to it unchanged.
 */
const USAGE_SUMMARY_COMMAND = "/feedback summary";
const USAGE_SUMMARY_LABEL = "usage summary";
const USAGE_SUMMARY_TITLE =
  "send /feedback summary: the gateway posts the install's usage and feedback block, with a copy button";
/**
 * Some WebKit versions report `isComposing: false` on the Enter that commits
 * an IME composition, but still carry the legacy `keyCode` for it - this is
 * the fallback check for those.
 */
const IME_COMMIT_KEYCODE = 229;
/**
 * The chip over the box while a reply is aimed under a card's report. It names
 * the report by its first line (replyLabel), never by the task id the frame
 * carries: the id is plumbing. A button turn (the usage summary) neither
 * carries the reply nor consumes it: the reply is for the next typed turn.
 */
const REPLY_CHIP_TEXT = "replying under the report";
const REPLY_CLEAR_GLYPH = "×";
const REPLY_CLEAR_LABEL = "stop replying under the report";

/**
 * Where the pane's next typed turn goes: under the report entry `entryId` of
 * `conversation`. The task and the label are read off the entry at render, so
 * an in-place edit of the report shows through and a report that left the
 * transcript (/clear) or a conversation that changed (/new) disarms it.
 */
interface ReplyTarget {
  entryId: string;
  conversation: string;
}

interface ChatProps {
  entries: ChatEntry[];
  /** The connected NATS user - the read-only badge only vouches for `web`. */
  user: string;
  /** The console conversation this tab speaks in; absent for the read-only view. */
  conversation?: string;
  probe?: ProbeResult;
  probePending?: boolean;
  onProbe: () => void;
  /**
   * True if the turn was accepted; the box is cleared only then. `replyTo` is
   * the root task of the card's report the turn answers, when the user aimed
   * it there with the report's reply button.
   */
  onSend?: (text: string, replyTo?: string) => boolean;
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
  const [reply, setReply] = useState<ReplyTarget | null>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const canSend = onSend !== undefined && user !== READ_ONLY_USER;
  const target =
    reply !== null && reply.conversation === conversation ? entries.find((e) => e.id === reply.entryId) : undefined;
  const armed = target?.replyTo !== undefined ? { replyTo: target.replyTo, label: replyLabel(target.text) } : null;

  const armReply = (entry: ChatEntry) => {
    setReply({ entryId: entry.id, conversation: entry.correlationId });
    inputRef.current?.focus();
  };
  const clearReply = () => {
    setReply(null);
    inputRef.current?.focus();
  };

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
        // A usage error keeps its text so it can be fixed.
        if (c.command.name !== "error") setDraft("");
        // A cleared transcript forgets the reply too: the report's next edit
        // would otherwise bring the entry, and the chip, back.
        if (c.command.name === "clear") setReply(null);
        return;
      case "send": {
        // A settled line (stop, a bare /session or /session off, a bare
        // /feedback or its summary) opens no turn, so it can carry no reply:
        // it goes out without the report and the chip waits for the next
        // typed turn, as it does across the usage summary button.
        const settled = turnFate(c.text).kind === "settled";
        // Only clear the box once the turn was actually accepted - refused
        // (the link is down) or not, the caller decides, and a refused turn
        // must stay in the box rather than vanish.
        if (onSend?.(c.text, settled ? undefined : armed?.replyTo) === true) {
          setDraft("");
          if (!settled) setReply(null);
        }
        return;
      }
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
      <Transcript
        entries={entries}
        empty={canSend ? EMPTY_CONSOLE : EMPTY_READ_ONLY}
        reply={canSend && conversation !== undefined ? { conversation, onReply: armReply } : undefined}
      />
      {canSend && armed !== null && (
        <div className="chat-reply-chip" role="status">
          <span className="chat-reply-text">
            {REPLY_CHIP_TEXT}
            {armed.label !== "" ? `: ${armed.label}` : ""}
          </span>
          <button
            type="button"
            className="probe-button chat-reply-clear"
            aria-label={REPLY_CLEAR_LABEL}
            title={REPLY_CLEAR_LABEL}
            onClick={clearReply}
          >
            {REPLY_CLEAR_GLYPH}
          </button>
        </div>
      )}
      {canSend && (
        <textarea
          className="chat-input"
          rows={INPUT_ROWS}
          value={draft}
          placeholder="ask the agent, or /help"
          aria-label="message"
          ref={inputRef}
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
        {canSend && (
          <button
            type="button"
            className="probe-button usage-summary-button"
            title={USAGE_SUMMARY_TITLE}
            onClick={() => onSend?.(USAGE_SUMMARY_COMMAND)}
          >
            {USAGE_SUMMARY_LABEL}
          </button>
        )}
        {probe && <span className={`probe-result probe-${probe.outcome}`}>{probeText(probe)}</span>}
      </div>
    </div>
  );
}
