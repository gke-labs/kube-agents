// Package mcpbridge is the workstation-side kube-agents Model Context Protocol
// (MCP) bridge: a stdio JSON-RPC server that lets a local coding agent drive a
// kube-agents deployment through its in-cluster a2a-mcp server — discover and
// connect to deployments across kubeconfig contexts, send a message and wait
// for its answer, manage persistent /session workers, read and stop tasks,
// read blackboard topics, and inspect or download the GitHub pull requests an
// agent filed. It relays notifications/progress frames as live progress, but
// the in-cluster server sends none yet: a turn's result shows when it ends.
//
// It is a thin client of the in-cluster a2a-mcp server (<agent>-a2a-mcp:8089):
// it forwards tool calls over HTTP/NDJSON (a2a/mcpproto) and manages local
// kubeconfig discovery, port-forwarding, and GitHub pull request inspection.
// Optional IDE/host-specific capabilities (such as swarm UI projection,
// conversation lineage tracking, and push notification wakeups) can be injected
// via the HostIntegration interface on ServerOptions. Everything that touches
// Kubernetes — kubeconfig discovery, reading the <agent>-a2a-mcp-token Secret,
// and the in-process port-forward to the <agent>-a2a-mcp Service, whose streams
// carry the bridge's connections without a local listener — sits
// behind the ClusterAccess interface and is implemented by the sibling package
// kube (k8s.io/client-go), so this package itself has no Kubernetes dependency.
// Linux and macOS are supported; process supervision relies on POSIX signals.
// A deployment profile's directMcpUrl, set by hand in the state file, skips
// the port-forward: it is the a2a-mcp server's base URL or its /mcp endpoint
// (either names the same server), https://, or http:// on loopback only.
//
// Environment variables:
//
//	KUBE_AGENTS_MCP_CONFIG         bridge state file
//	                               (default ~/.config/kube-agents/mcp-config.json)
//	KUBE_AGENTS_MCP_TOKEN          explicit Bearer token override for the
//	                               in-cluster a2a-mcp listener: the
//	                               deployment's when only one is configured;
//	                               sent only to that deployment's saved
//	                               kube context, namespace and direct URL,
//	                               never to a connection that overrides them
//	KUBE_AGENTS_MCP_TOKEN_DEPLOYMENT
//	                               the saved key of the profile the token
//	                               belongs to, matched exactly (never by
//	                               base name or kube context); set, it
//	                               decides even with one deployment saved,
//	                               and naming no saved key sends the token
//	                               nowhere, so after a rename (<context>/<name>)
//	                               or a prune the old value names nothing
//	                               until it is updated (a different deployment
//	                               later saved under the same key does get it,
//	                               so update it when removing an install); unset, the token
//	                               goes to the only saved deployment, or
//	                               nowhere with several
//	GH_TOKEN / GITHUB_TOKEN        GitHub token for inspect_pr; when neither is
//	                               set the bridge falls back to the github.com
//	                               entry in ~/.git-credentials, if present
//
// RunCLI is the entry point: it accepts --version and --config, and otherwise
// serves MCP over stdio. Callers pass the ClusterAccess implementation
// (normally kube.New()); the binary lives in a2a/cmd/kube-agents-mcp.
package mcpbridge
