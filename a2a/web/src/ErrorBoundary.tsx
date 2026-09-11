/**
 * The page renders content published by anyone on the bus. A malformed
 * envelope that gets past `parseEnvelope` must not be able to blank the
 * screen — and because the stream is durable, a crash would repeat on every
 * reload until the message aged out. So the tree is wrapped: the failure
 * shows as a line the operator can read and reload past, not a white page.
 */
import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("UI crashed while rendering bus content:", error, info.componentStack);
  }

  render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <div className="crash-pane">
        <h1>the view crashed</h1>
        <p>
          Something on the bus rendered badly. The bus itself is fine — this is the
          browser. Reload to rebuild from the stream.
        </p>
        <pre>{String(error.message || error)}</pre>
        <button type="button" onClick={() => this.setState({ error: null })}>
          try again
        </button>
      </div>
    );
  }
}
