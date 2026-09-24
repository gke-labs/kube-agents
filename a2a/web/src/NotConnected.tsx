/**
 * What the page shows when it has no bus to talk to: nothing served it a
 * credential, or the connect failed. The names are the default install's
 * (namespace kubeagents-system, PlatformAgent platform-agent). The server
 * answers only on local port 8080, because that is the origin the bus
 * allows.
 */

const PORT_FORWARD = "kubectl -n kubeagents-system port-forward svc/platform-agent-a2a-console 8080:8080";
const CONSOLE_URL = "http://localhost:8080";
const LOCAL_DEV_QUERY = "?ws=ws://localhost:9222&user=console&pass=dev-console";

export default function NotConnected({ error, onRetry }: { error: string | null; onRetry: () => void }) {
  return (
    <div className="connect-form">
      <h1>a2a console</h1>
      {error !== null ? (
        <p className="connect-error" role="alert">
          {error}
        </p>
      ) : (
        <p className="connect-hint">No console server answered, so there's no bus credential to connect with.</p>
      )}
      <p className="connect-hint">
        Against the install, forward the console server:
        <br />
        <code>{PORT_FORWARD}</code>
        <br />
        and open <code>{CONSOLE_URL}</code>. It only answers on local port 8080.
      </p>
      <p className="connect-hint">
        Local dev against <code>dev/nats.conf</code>: add <code>{LOCAL_DEV_QUERY}</code> to this page's URL.
      </p>
      <button type="button" onClick={onRetry}>
        Retry
      </button>
    </div>
  );
}
