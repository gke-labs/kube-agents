/**
 * One row of tiles across the top. Each tile is a click target that focuses
 * the matching dashboard panel. Values come from derive.ts, so a tile never
 * computes anything itself.
 */
import {
  SPEND_TBD_REASON,
  failuresOf,
  tasksInFlight,
  typesOf,
  worstCapacity,
} from "./derive.ts";
import type { UiState } from "./model.ts";

export type PanelKey = "sessions" | "tasks" | "conversations" | "topics" | "capacity" | "anomalies" | "spend";

/** A focus request. `seq` makes a second click on the same tile scroll again. */
export interface PanelFocus {
  key: PanelKey;
  seq: number;
}

interface TileProps {
  id: string;
  label: string;
  panel: PanelKey;
  warn?: boolean;
  title?: string;
  onFocus: (key: PanelKey) => void;
  children: React.ReactNode;
}

function Tile({ id, label, panel, warn, title, onFocus, children }: TileProps) {
  return (
    <button
      type="button"
      className={`tile${warn ? " tile-warn" : ""}`}
      data-testid={`tile-${id}`}
      title={title}
      onClick={() => onFocus(panel)}
    >
      <span className="tile-label">{label}</span>
      <span className="tile-value">{children}</span>
    </button>
  );
}

export default function StatusStrip({ state, onFocus }: { state: UiState; onFocus: (key: PanelKey) => void }) {
  const linkDown = state.connection !== "up";
  const streamsShort = state.streamsUp < state.streamsTotal;
  const worst = worstCapacity(state);
  return (
    <div className="status-strip">
      <Tile
        id="link"
        label="bus"
        panel="capacity"
        warn={linkDown || streamsShort}
        title={linkDown ? `link ${state.connection}` : undefined}
        onFocus={onFocus}
      >
        <span>{state.connection}</span>{" "}
        {/* Ordered consumers survive a reconnect and their iterators never
            end, so streamsUp does not shrink on its own while the link is
            down - showing it here would repeat a count from before the
            drop as though it were current. */}
        <span>{linkDown ? "streams unknown" : `${state.streamsUp}/${state.streamsTotal} streams`}</span>
      </Tile>
      <Tile id="types" label="agents" panel="sessions" onFocus={onFocus}>
        {typesOf(state).length === 0 && <span>none seen</span>}
        {typesOf(state).map((t) => (
          <span className="tile-type" key={t.agentType}>
            <span className="led" key={t.pulses} data-pulses={t.pulses} />
            <span>{t.agentType}</span>
            <span className="tile-count">{t.count}</span>
          </span>
        ))}
      </Tile>
      <Tile id="inflight" label="in flight" panel="tasks" onFocus={onFocus}>
        {tasksInFlight(state).length}
      </Tile>
      <Tile id="failures" label="failures" panel="tasks" warn={failuresOf(state).length > 0} onFocus={onFocus}>
        {failuresOf(state).length}
      </Tile>
      <Tile
        id="capacity"
        label="capacity"
        panel="capacity"
        warn={worst?.warn}
        title={worst?.text}
        onFocus={onFocus}
      >
        {worst === null ? "no limits set" : `${worst.stream} ${Math.round((worst.fraction as number) * 100)}%`}
      </Tile>
      <Tile id="spend" label="spend" panel="spend" title={SPEND_TBD_REASON} onFocus={onFocus}>
        tbd
      </Tile>
    </div>
  );
}
