/**
 * What the page shows when it has no bus to talk to: nothing served it a
 * credential, the URL named a user with no password for it, or the connect
 * failed. The names are the default install's (namespace kubeagents-system,
 * PlatformAgent platform-agent). The console server answers only on local
 * port 8080, because that is the origin the bus allows; the bus itself
 * answers on 9222.
 */

const PORT_FORWARD = "kubectl -n kubeagents-system port-forward svc/platform-agent-a2a-console 8080:8080";
const CONSOLE_URL = "http://localhost:8080";
const LOCAL_DEV_QUERY = "?ws=ws://localhost:9222&user=console&pass=dev-console";
const READ_ONLY_PORT_FORWARD = "kubectl -n kubeagents-system port-forward svc/platform-agent-a2a-nats 9222:9222";
/** The prefix a2a/console sends when the credential Secret is missing or empty. */
const MISSING_CREDENTIAL_PREFIX = "503:";

export default function NotConnected({
  error,
  namedUser = null,
  onRetry,
}: {
  error: string | null;
  namedUser?: string | null;
  onRetry: () => void;
}) {
  const missingCredential = error !== null && error.startsWith(MISSING_CREDENTIAL_PREFIX);

  return (
    <div className="connect-form">
      <h1>a2a console</h1>
      {error !== null ? (
        <p className="connect-error" role="alert">
          {error}
        </p>
      ) : namedUser !== null ? (
        <p className="connect-hint">
          The URL asks for user <code>{namedUser}</code> with no password, so this page didn&apos;t ask the console
          server for its own credential - that would connect as a different user than the one named.
        </p>
      ) : (
        <p className="connect-hint">No console server answered, so there's no bus credential to connect with.</p>
      )}
      {namedUser !== null && error === null ? (
        <p className="connect-hint">
          Port-forward the bus itself:
          <br />
          <code>{READ_ONLY_PORT_FORWARD}</code>
          <br />
          and open this page with <code>{`?ws=ws://localhost:9222&user=${namedUser}&pass=<password>`}</code>.
        </p>
      ) : missingCredential ? (
        <p className="connect-hint">
          The console server answered but has no credential yet. Check that the platform agent is in next mode
          and that its credential Secret exists.
        </p>
      ) : (
        <p className="connect-hint">
          Against the install, forward the console server:
          <br />
          <code>{PORT_FORWARD}</code>
          <br />
          and open <code>{CONSOLE_URL}</code>. It only answers on local port 8080.
        </p>
      )}
      {namedUser === null && !missingCredential && (
        <p className="connect-hint">
          Local dev against <code>dev/nats.conf</code>: add <code>{LOCAL_DEV_QUERY}</code> to this page's URL.
        </p>
      )}
      <button type="button" onClick={onRetry}>
        Retry
      </button>
    </div>
  );
}
