/**
 * The transcript list, shared by the chat pane and the session view.
 *
 * Entries render by kind: `user` is the ask the gateway echoed onto the bus,
 * `steer` a follow-up into a running task, `answer` a `result` or `turn`
 * artifact streaming in (a finished turn's answer ahead of a follow-up's,
 * or the task's deliverable), `progress`/`status`/`topic`/`cancel` the
 * quieter lines,
 * `pending` a console turn not yet seen on the bus, `sent` a console turn
 * the gateway never makes a task of (a stop word, a bare `/session`), so it
 * settles at send, `notice` a gateway line
 * from the console door, `local` the page's own output, `anomaly` an
 * envelope that broke the protocol, reported rather than folded in. Each
 * exchange group gets one correlation chip colored by corrColor.
 */
import { useEffect, useRef, useState } from "react";
import { fmtUsage } from "./derive.ts";
import type { ChatEntry } from "./model.ts";
import { corrColor } from "./model.ts";

/** Within this many px of the bottom counts as "following" the transcript. */
const FOLLOW_SLACK_PX = 10;
/**
 * A gateway notice that carries a fenced block (the usage and feedback
 * summary is one) gets a copy button: the block, fences stripped, goes to the
 * clipboard in one click, which is what a person on a restricted install
 * pastes into a report. The clipboard needs a user gesture, so the copy is
 * the click, never the arrival of the notice.
 */
// A fence opens on a line that starts with three backticks after at most three spaces (a language tag may follow) and
// closes on a line that is three backticks and nothing else, the CommonMark shape; an inline triple backtick is a
// code span, as the gateway's own Slack renderer reads it.
const LINE_BREAK_RE = /\r?\n/;
const FENCE_OPEN_RE = /^ {0,3}```/;
const FENCE_CLOSE_RE = /^ {0,3}```\s*$/;
const COPY_LABEL = "copy";
const COPIED_LABEL = "copied";
const COPY_FAILED_LABEL = "copy failed";
const COPY_TITLE = "copy the block, without its fences, to the clipboard";
const COPIED_RESET_MS = 2000;
/**
 * A card's report (a notice whose frame carries the report mark and names
 * the turn that filed the card) gets a reply button under its text: the pane's next typed turn then
 * carries that task as the frame's `replyTo`. Only a pane that can send, and
 * only for the conversation it is on, offers it; the session view renders the
 * same list read-only. The button's accessible name carries the report's
 * first line, cut as the chip cuts it. It renders inside the entry's body
 * with the text, as a small lowercase action on its own line: an entry is a
 * flex row, and a button placed as the text's sibling lands in the meta
 * column at the right, stretched to the row's height.
 */
const REPLY_LABEL = "reply";
/** A quiet affordance ahead of the label, so the action reads apart from the report's body. */
const REPLY_GLYPH = "\u21a9";
const REPLY_TITLE = "reply under this report";
const REPLY_NAME_PREFIX = "reply under: ";
const REPLY_NAME_FALLBACK = "reply under this report";
const REPLY_LABEL_CHARS = 60;
const REPLY_LABEL_ELLIPSIS = "…";
// The label is read, not rendered, so Markdown's marks come off it: a leading
// heading, quote or list marker, a thematic break, and emphasis or code marks
// anywhere. An underscore is emphasis only at a word's edge; inside a name
// (payments_api, kanban_notifier.py) it is part of the name and stays. The
// entry's text stays as written.
const MD_LINE_MARKER_RE = /^(?:#{1,6}\s+|>\s*|[-*+]\s+|\d+[.)]\s+)+/;
const MD_RULE_RE = /^(?:[-*_]\s*){3,}$/;
const MD_UNDERSCORE_MARK_RE = /(^|[\s(])_+(?=\S)|(?<=\S)_+(?=[\s.,;:!?)]|$)/g;
const MD_INLINE_MARK_RE = /\*\*|[*`]/g;

/**
 * The content of the first fenced block in text, fences stripped, or null when
 * there is none. The opening fence may carry a language tag; the closing fence
 * must start a line, so a triple backtick inside the body does not end it.
 */
export function fencedBlock(text: string): string | null {
  const lines = text.split(LINE_BREAK_RE);
  const open = lines.findIndex((line) => FENCE_OPEN_RE.test(line));
  if (open < 0) return null;
  const close = lines.findIndex((line, i) => i > open && FENCE_CLOSE_RE.test(line));
  if (close < 0) return null;
  const body = lines.slice(open + 1, close).join("\n");
  return body.trim() === "" ? null : body;
}

/**
 * A line with its Markdown marks removed; empty when it was only marks. A fence
 * line is blank whatever its language tag, so a report that opens its output
 * with ```yaml is not named "yaml".
 */
function strippedLine(line: string): string {
  const trimmed = line.trim();
  if (FENCE_OPEN_RE.test(trimmed) || MD_RULE_RE.test(trimmed)) return "";
  return trimmed
    .replace(MD_LINE_MARKER_RE, "")
    .replace(MD_UNDERSCORE_MARK_RE, "$1")
    .replace(MD_INLINE_MARK_RE, "")
    .trim();
}

/**
 * The report's first line that has text once its marks are off, cut for the
 * chip and the button's name. A line that is only marks (a fence, a rule, a
 * bare quote) is skipped, so a report that opens with one is still named.
 */
export function replyLabel(text: string): string {
  const line = text.split(LINE_BREAK_RE).map(strippedLine).find((l) => l !== "") ?? "";
  const chars = Array.from(line);
  return chars.length > REPLY_LABEL_CHARS ? chars.slice(0, REPLY_LABEL_CHARS).join("") + REPLY_LABEL_ELLIPSIS : line;
}

function replyName(text: string): string {
  const label = replyLabel(text);
  return label === "" ? REPLY_NAME_FALLBACK : REPLY_NAME_PREFIX + label;
}

/** Where a pane takes replies: the conversation it is on, and what to do with the report picked. */
export interface ReplyOffer {
  conversation: string;
  onReply: (entry: ChatEntry) => void;
}

const GLYPH: Record<ChatEntry["kind"], string> = {
  user: "ask>",
  steer: "steer>",
  answer: "",
  progress: "⏳",
  status: "⋯",
  topic: "⊙",
  cancel: "✕",
  anomaly: "⚠",
  pending: "ask>",
  sent: "ask>",
  notice: "gw",
  local: "›",
};

const TAGGED: ChatEntry["kind"][] = ["progress", "topic", "anomaly"];

interface TranscriptProps {
  entries: ChatEntry[];
  empty: string;
  /** Absent in a read-only view: no report takes a reply there. */
  reply?: ReplyOffer;
}

export default function Transcript({ entries, empty, reply }: TranscriptProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [shouldAutoScroll, setShouldAutoScroll] = useState(true);
  const [copyState, setCopyState] = useState<{ id: string; label: string } | null>(null);
  const canCopy = typeof navigator !== "undefined" && navigator.clipboard !== undefined;

  const copyBlock = async (entry: ChatEntry, block: string) => {
    try {
      await navigator.clipboard.writeText(block);
      setCopyState({ id: entry.id, label: COPIED_LABEL });
    } catch {
      setCopyState({ id: entry.id, label: COPY_FAILED_LABEL });
    }
    setTimeout(() => setCopyState((s) => (s?.id === entry.id ? null : s)), COPIED_RESET_MS);
  };

  const handleScroll = () => {
    if (containerRef.current) {
      const { scrollTop, scrollHeight, clientHeight } = containerRef.current;
      setShouldAutoScroll(scrollHeight - scrollTop - clientHeight < FOLLOW_SLACK_PX);
    }
  };

  useEffect(() => {
    if (shouldAutoScroll && containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight;
    }
  }, [entries, shouldAutoScroll]);

  const rendered = entries.map((entry, idx) => {
    const isFirstInGroup = idx === 0 || entries[idx - 1].correlationId !== entry.correlationId;
    const glyph = GLYPH[entry.kind];
    const replies =
      entry.kind === "notice" &&
      entry.replyTo !== undefined &&
      reply !== undefined &&
      entry.correlationId === reply.conversation;
    return (
      <div key={entry.id} className="chat-entry-group">
        {isFirstInGroup && (
          <div
            className="corr-chip"
            style={{ backgroundColor: corrColor(entry.correlationId) }}
            title={`Correlation: ${entry.correlationId.slice(0, 8)}...`}
            data-corr={entry.correlationId}
          />
        )}
        <div className={`chat-entry chat-${entry.kind}`}>
          {glyph !== "" && <span className="chat-glyph">{glyph}</span>}
          {TAGGED.includes(entry.kind) && entry.session && <span className="chat-session">[{entry.session}]</span>}
          {replies ? (
            <span className="chat-body">
              <span className="chat-text">{entry.text}</span>
              <button
                type="button"
                className="chat-reply"
                title={REPLY_TITLE}
                aria-label={replyName(entry.text)}
                onClick={() => reply.onReply(entry)}
              >
                {REPLY_GLYPH} {REPLY_LABEL}
              </button>
            </span>
          ) : (
            <span className="chat-text">{entry.text}</span>
          )}
          {entry.kind === "notice" &&
            canCopy &&
            (() => {
              const block = fencedBlock(entry.text);
              if (block === null) return null;
              const label = copyState?.id === entry.id ? copyState.label : COPY_LABEL;
              return (
                <button
                  type="button"
                  className="probe-button chat-copy"
                  title={COPY_TITLE}
                  onClick={() => void copyBlock(entry, block)}
                >
                  {label}
                </button>
              );
            })()}
          {entry.note && <span className="chat-note">{entry.note}</span>}
          {entry.kind === "answer" && entry.usage && (
            <span className="chat-usage" title="what this turn cost, as its executor reported it">
              {fmtUsage(entry.usage)}
            </span>
          )}
        </div>
      </div>
    );
  });

  return (
    <div className="chat-transcript" ref={containerRef} onScroll={handleScroll}>
      {rendered.length > 0 ? rendered : <div className="chat-empty">{empty}</div>}
    </div>
  );
}
