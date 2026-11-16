package k8sapiproxy

import (
	"net/http"
	"strings"
)

const (
	headerUpgrade     = "Upgrade"
	headerConnection  = "Connection"
	upgradeToken      = "upgrade"
	connectionUpgrade = "Upgrade"

	headerWebSocketProtocol = "Sec-WebSocket-Protocol"
	// subprotocolAuthPrefix starts a WebSocket subprotocol the API server reads as a bearer
	// token, for clients that cannot set Authorization.
	subprotocolAuthPrefix = "base64url.bearer.authorization.k8s.io."
)

// callerHeaders never reach the API server: the caller's own credentials, Audit-ID, which the
// API server would record as the request's audit ID, and X-Real-Ip, which it would record as a
// source IP. The reverse proxy's Rewrite drops Forwarded and X-Forwarded-*.
var callerHeaders = []string{"Authorization", "Proxy-Authorization", "Cookie", "Audit-ID", "X-Real-Ip"}

// identityHeaderPrefixes start the headers an API server reads as the requesting identity:
// impersonation, and the X-Remote-* headers of an authenticating front proxy.
var identityHeaderPrefixes = []string{"impersonate-", "x-remote-"}

// HasUpgradeHeader reports whether r asks for a protocol upgrade (SPDY or WebSocket), through
// either an Upgrade header or an `upgrade` token in Connection.
func HasUpgradeHeader(r *http.Request) bool {
	if strings.TrimSpace(r.Header.Get(headerUpgrade)) != "" {
		return true
	}
	for _, v := range r.Header.Values(headerConnection) {
		for _, token := range strings.Split(v, ",") {
			if strings.EqualFold(strings.TrimSpace(token), upgradeToken) {
				return true
			}
		}
	}
	return false
}

// sanitizeRequestHeaders removes from h what must not reach the API server: the caller's
// credentials, a bearer token in Sec-WebSocket-Protocol, Audit-ID, identity headers, the headers the caller named in Connection, Connection
// itself, and Upgrade unless preserveUpgrade. It runs before the handler sets any header of
// its own: the reverse proxy drops every header Connection names, so a caller listing one
// the handler sets could otherwise make it disappear.
func sanitizeRequestHeaders(h http.Header, preserveUpgrade bool) {
	for _, v := range h.Values(headerConnection) {
		for _, token := range strings.Split(v, ",") {
			name := strings.TrimSpace(token)
			if name == "" || (preserveUpgrade && strings.EqualFold(name, upgradeToken)) {
				continue
			}
			h.Del(name)
		}
	}
	h.Del(headerConnection)
	for _, name := range callerHeaders {
		h.Del(name)
	}
	stripBearerProtocols(h)
	for key := range h {
		lower := strings.ToLower(key)
		for _, prefix := range identityHeaderPrefixes {
			if strings.HasPrefix(lower, prefix) {
				delete(h, key)
				break
			}
		}
	}
	if preserveUpgrade && strings.TrimSpace(h.Get(headerUpgrade)) != "" {
		h.Set(headerConnection, connectionUpgrade)
	} else {
		h.Del(headerUpgrade)
	}
}

// stripBearerProtocols drops the Sec-WebSocket-Protocol entries that carry a bearer token and
// keeps the rest, which name the stream protocol.
// It matches the key in any case, so a map built without canonical keys cannot slip one past.
func stripBearerProtocols(h http.Header) {
	var kept []string
	for key, values := range h {
		if !strings.EqualFold(key, headerWebSocketProtocol) {
			continue
		}
		for _, v := range values {
			for _, entry := range strings.Split(v, ",") {
				entry = strings.TrimSpace(entry)
				if entry == "" || strings.HasPrefix(strings.ToLower(entry), subprotocolAuthPrefix) {
					continue
				}
				kept = append(kept, entry)
			}
		}
		delete(h, key)
	}
	if len(kept) > 0 {
		h.Set(headerWebSocketProtocol, strings.Join(kept, ", "))
	}
}
