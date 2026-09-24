/**
 * One session's transcript, swapped into the dashboard body from the sessions
 * panel or /replay. It is the rail's replay without the ghost strip.
 */
import { sessionEntries } from "./derive.ts";
import type { UiState } from "./model.ts";
import Transcript from "./Transcript.tsx";

export default function SessionTranscript({
  state,
  session,
  onBack,
}: {
  state: UiState;
  session: string;
  onBack: () => void;
}) {
  const agent = state.agents.get(session);
  return (
    <div className="session-view">
      <div className="session-header">
        <button type="button" className="session-back" onClick={onBack}>
          ← dashboard
        </button>
        <span className="session-name">{session}</span>
        {agent && <span className="session-type">{agent.agentType}</span>}
        {agent && <span className="session-status">{agent.status}</span>}
      </div>
      <Transcript entries={sessionEntries(state, session)} empty={`nothing from ${session} in the retention window`} />
    </div>
  );
}
