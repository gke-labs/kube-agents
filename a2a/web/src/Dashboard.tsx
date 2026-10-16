/**
 * The dashboard body: seven panels in a grid that reflows with the window.
 * Every panel renders a sentence when it has nothing, and every failure
 * renders what failed.
 */
import { useEffect, useRef } from "react";
import {
  KV_TBD_REASON,
  SPEND_TBD_REASON,
  capacities,
  conversationsByBackend,
  currentTaskOf,
  fmtAgo,
  livenessOf,
  recentTasks,
  taskTimes,
} from "./derive.ts";
import { corrColor, type UiState } from "./model.ts";
import type { PanelFocus, PanelKey } from "./StatusStrip.tsx";

const RECENT_TASKS = 20;
const FAILED = ["failed", "rejected"];

function Panel({
  id,
  title,
  focused,
  children,
}: {
  id: PanelKey;
  title: string;
  focused: boolean;
  children: React.ReactNode;
}) {
  return (
    <section className={`panel${focused ? " panel-focused" : ""}`} data-panel={id}>
      <h2 className="panel-title">{title}</h2>
      {children}
    </section>
  );
}

function Empty({ text }: { text: string }) {
  return <p className="panel-empty">{text}</p>;
}

export default function Dashboard({
  state,
  focus,
  onSession,
}: {
  state: UiState;
  focus: PanelFocus | null;
  onSession: (session: string) => void;
}) {
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (focus === null) return;
    root.current?.querySelector(`[data-panel="${focus.key}"]`)?.scrollIntoView?.({ block: "nearest" });
  }, [focus]);
  const is = (key: PanelKey) => focus?.key === key;
  const agents = [...state.agents.values()].sort((a, b) => a.session.localeCompare(b.session));
  const tasks = recentTasks(state, RECENT_TASKS);
  const groups = conversationsByBackend(state);
  const topics = [...state.topics.values()].sort((a, b) => a.key.localeCompare(b.key));

  return (
    <div className="dashboard" ref={root}>
      <Panel id="sessions" title="sessions" focused={is("sessions")}>
        {agents.length === 0 ? (
          <Empty text="no sessions seen on the bus yet" />
        ) : (
          <table>
            <thead>
              <tr>
                <th>session</th>
                <th>type</th>
                <th>status</th>
                <th>liveness</th>
                <th>task</th>
                <th>last activity</th>
              </tr>
            </thead>
            <tbody>
              {agents.map((a) => {
                const current = currentTaskOf(state, a.session);
                const live = livenessOf(state.liveness.get(a.session), state.now, current !== undefined, a.status);
                return (
                  <tr
                    key={a.session}
                    className="row-click"
                    tabIndex={0}
                    onClick={() => onSession(a.session)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        if (e.key === " ") e.preventDefault();
                        onSession(a.session);
                      }
                    }}
                  >
                    <td>{a.session}</td>
                    <td>{a.agentType}{a.profile ? `/${a.profile}` : ""}</td>
                    <td>{a.status}</td>
                    <td className={`live-${live.kind}`} title={live.title}>{live.text}</td>
                    <td className="cell-nowrap">{current ? current.taskId : ""}</td>
                    <td className="cell-nowrap">{a.lastActivity ? fmtAgo(a.lastActivity, state.now) : ""}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </Panel>

      <Panel id="tasks" title="recent tasks" focused={is("tasks")}>
        {tasks.length === 0 ? (
          <Empty text="no tasks on the bus yet" />
        ) : (
          <table>
            <thead>
              <tr>
                <th>task</th>
                <th>to</th>
                <th>state</th>
                <th>time</th>
                <th>cost</th>
                <th>reason</th>
              </tr>
            </thead>
            <tbody>
              {tasks.map((t) => (
                <tr key={t.taskId} className={FAILED.includes(t.state) ? "row-failed" : undefined}>
                  <td>
                    <span className="corr-dot" style={{ backgroundColor: corrColor(t.correlationId) }} />
                    {t.taskId}
                  </td>
                  <td>{t.addressee}</td>
                  <td>{t.state}</td>
                  <td>{taskTimes(t)}</td>
                  <td title={SPEND_TBD_REASON}>tbd</td>
                  <td>{t.reason ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>

      <Panel id="conversations" title="conversations" focused={is("conversations")}>
        {groups.length === 0 ? (
          <Empty text="no conversations on the bus yet" />
        ) : (
          groups.map((g) => (
            <div key={g.backend} className="conv-group">
              <h3>{g.backend}</h3>
              {g.conversations.map((c) => (
                <div key={c.conversation} className="conv-row">
                  <span>{c.conversation}</span>
                  <span>{`${c.turns} turns`}</span>
                  <span>{c.lastSeen ? fmtAgo(c.lastSeen, state.now) : ""}</span>
                </div>
              ))}
            </div>
          ))
        )}
      </Panel>

      <Panel id="topics" title="blackboard topics" focused={is("topics")}>
        {topics.length === 0 ? (
          <Empty text="no topics published yet" />
        ) : (
          topics.map((t) => (
            <div key={t.key} className="topic-row">
              <span>{t.key}</span>
              <span>{t.summary}</span>
              <span>{t.at ? `${fmtAgo(t.at, state.now)} by ${t.publisher}` : `by ${t.publisher}`}</span>
            </div>
          ))
        )}
      </Panel>

      <Panel id="capacity" title="bus capacity" focused={is("capacity")}>
        {capacities(state).map((c) => (
          <div key={c.stream} className={`cap-row${c.warn ? " cap-warn" : ""}`}>
            <span className="cap-bar">
              <span style={{ width: `${Math.round((c.fraction ?? 0) * 100)}%` }} />
            </span>
            <span>{c.text}</span>
          </div>
        ))}
        <div className="cap-row cap-tbd" title={KV_TBD_REASON}>
          <span className="cap-bar" />
          <span>{`KV buckets: ${KV_TBD_REASON}`}</span>
        </div>
      </Panel>

      <Panel id="anomalies" title="anomalies" focused={is("anomalies")}>
        <div className="anomaly-row">
          <span>post-final events</span>
          <span>{state.anomalies.postFinal}</span>
        </div>
        <div className="anomaly-row">
          <span>addressee disagreement</span>
          <span>{state.anomalies.addressee}</span>
        </div>
        <div className="anomaly-row">
          <span>missing submitted</span>
          <span>{state.anomalies.missingSubmitted}</span>
        </div>
      </Panel>

      <Panel id="spend" title="spend" focused={is("spend")}>
        {["tokens", "cost", "model", "harness duration"].map((f) => (
          <div key={f} className="anomaly-row">
            <span>{f}</span>
            <span>tbd</span>
          </div>
        ))}
        <p className="panel-empty">{SPEND_TBD_REASON}</p>
      </Panel>
    </div>
  );
}
