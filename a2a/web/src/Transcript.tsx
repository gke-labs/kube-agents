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

export default function Transcript({ entries, empty }: { entries: ChatEntry[]; empty: string }) {
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
          <span className="chat-text">{entry.text}</span>
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
