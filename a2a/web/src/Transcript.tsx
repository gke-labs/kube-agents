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
import type { ChatEntry } from "./model.ts";
import { corrColor } from "./model.ts";

/** Within this many px of the bottom counts as "following" the transcript. */
const FOLLOW_SLACK_PX = 10;

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
          {entry.note && <span className="chat-note">{entry.note}</span>}
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
