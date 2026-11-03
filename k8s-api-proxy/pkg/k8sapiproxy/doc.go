// Package k8sapiproxy holds the Kubernetes API proxy that sits between the agent sandbox and
// cluster API servers. NewHandler authenticates each request through a CallerAuthenticator,
// picks the target cluster from the path (SplitClusterPath), decides from the API server's
// own classification of the request (NewRequestInfoFactory, Policy.Evaluate) whether it may
// be forwarded, strips the caller's credentials and identity headers, and forwards it through
// an UpstreamTransport. See the module README.
package k8sapiproxy
