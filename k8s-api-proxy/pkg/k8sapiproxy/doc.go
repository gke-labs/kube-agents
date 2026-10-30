// Package k8sapiproxy holds the Kubernetes API proxy that sits between the agent sandbox and
// cluster API servers. So far it has the request policy: Policy.Evaluate decides from the
// API server's own classification of a request (NewRequestInfoFactory) whether it may be
// forwarded, and SplitClusterPath picks the target cluster from the path. See the module
// README.
package k8sapiproxy
