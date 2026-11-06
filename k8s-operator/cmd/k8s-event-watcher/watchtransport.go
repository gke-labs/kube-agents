package main

import (
	"net/http"

	"k8s.io/client-go/rest"
)

// watchConfig returns a twin of cfg for the Events watch alone, whose
// connections are never reused, and true; or cfg itself and false where the
// twin would not be an exact copy of what client-go builds for cfg.
//
// Why (#4825): a cluster reached through the credential broker is addressed
// by a plain-HTTP rest.Config with no TLS options
// (clusterprofiles.Broker.ClientConfig), for which client-go hands back
// http.DefaultTransport (k8s.io/client-go/transport/cache.go). So every
// profile cluster's lists and watches share the process's one pool of
// HTTP/1.1 connections to the broker. On Go 1.27, net/http can leave a
// response's reader parked on a pooled connection: when client-go stops a
// watch the server ended with a 410, its Close races the reader's EOF, the
// transport's post-close drain returns the connection to the pool, and the
// reader stays parked on the connection's end-of-body channel. The next
// response on that connection, which may be another cluster's relist, wakes
// the parked reader instead of its own and hangs until the connection closes,
// up to the pool's 90-second idle timeout. Only a watch can be closed early
// that way: a list body is read to its end before it is closed. So watches
// get a transport that keeps no connection alive, and lists stay pooled.
// gke-labs/kube-agents#4823 traced the same race in the test fake.
//
// The twin is a clone of http.DefaultTransport with keep-alives off, so it
// keeps everything the shared transport has (proxy-from-environment, dial and
// TLS-handshake timeouts, the HTTP/2 attempt) and differs in that one field;
// client-go still applies its wrappers to it (bearer token, user agent,
// impersonation). It applies to a deliberately narrow subset of the configs
// client-go puts on http.DefaultTransport: no TLS options, no dialer, no proxy,
// no custom or wrapped transport, which is the broker's plain-HTTP config.
// Everything else keeps the single client it had. A config with only a
// WrapTransport (a GKE DNS-endpoint profile reached directly) also sits on the
// default pool, but over HTTPS, where the transport negotiates HTTP/2 and the
// race, which lives in HTTP/1.1's per-connection read loop, does not apply.
// TestBrokerConfigGetsANonPooledWatchTransport fails if the broker's config
// ever stops qualifying.
//
// The watch client has its own client-go rate limiter (the default 5 QPS, burst
// 10), so the watch no longer queues behind the cluster's list pages. A
// reflector opens a watch every 5-10 minutes, so it never comes near it.
//
// It can go once net/http hands each end-of-body signal to the reader of the
// response it belongs to.
func watchConfig(cfg *rest.Config) (*rest.Config, bool) {
	if cfg.Transport != nil || cfg.WrapTransport != nil || cfg.Dial != nil || cfg.Proxy != nil {
		return cfg, false
	}
	if tlsConfig, err := rest.TLSConfigFor(cfg); err != nil || tlsConfig != nil {
		return cfg, false
	}
	base, ok := http.DefaultTransport.(*http.Transport)
	if !ok {
		return cfg, false
	}
	transport := base.Clone()
	transport.DisableKeepAlives = true
	twin := rest.CopyConfig(cfg)
	twin.Transport = transport
	return twin, true
}
