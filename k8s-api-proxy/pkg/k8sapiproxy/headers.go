package k8sapiproxy

import (
	"fmt"
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

	// headerNameSymbols are the characters, besides letters and digits, of an HTTP field
	// name (RFC 9110 section 5.6.2, tchar).
	headerNameSymbols = "!#$%&'*+-.^_`|~"
)

// callerHeaders never reach the API server: the caller's own credentials, Audit-ID, which the
// API server would record as the request's audit ID, and X-Real-Ip, which it would record as a
// source IP. The reverse proxy's Rewrite drops Forwarded and X-Forwarded-*.
var callerHeaders = []string{"Authorization", "Proxy-Authorization", "Cookie", "Audit-ID", "X-Real-Ip"}

// identityHeaderPrefixes start the headers an API server reads as the requesting identity:
// impersonation, and the X-Remote-* headers of an authenticating front proxy.
var identityHeaderPrefixes = []string{"impersonate-", "x-remote-"}

// reservedCallerHeaders cannot be named in Config.ExtraCallerHeaders. Host, Content-Length
// and Transfer-Encoding frame the request and net/http carries them outside the header
// map, so listing one would strip nothing; Connection and Upgrade are the handler's to
// drop or keep, by whether the policy admits the upgrade.
var reservedCallerHeaders = map[string]struct{}{
	"host":              {},
	"content-length":    {},
	"transfer-encoding": {},
	"connection":        {},
	"upgrade":           {},
}

// newExtraCallerHeaders validates Config.ExtraCallerHeaders and returns the names in lower
// case, the form sanitizeRequestHeaders matches a header key against; nil for no names.
func newExtraCallerHeaders(names []string) (map[string]struct{}, error) {
	if len(names) == 0 {
		return nil, nil
	}
	extra := make(map[string]struct{}, len(names))
	for _, name := range names {
		if !validHeaderName(name) {
			return nil, fmt.Errorf("k8sapiproxy: Config.ExtraCallerHeaders entry %q is not an HTTP header name", name)
		}
		lower := strings.ToLower(name)
		if _, reserved := reservedCallerHeaders[lower]; reserved {
			return nil, fmt.Errorf("k8sapiproxy: Config.ExtraCallerHeaders entry %q cannot be stripped: the proxy needs it to forward the request", name)
		}
		extra[lower] = struct{}{}
	}
	return extra, nil
}

// validHeaderName reports whether name is a non-empty HTTP field name: letters, digits and
// headerNameSymbols, so no spaces or colons.
func validHeaderName(name string) bool {
	if name == "" {
		return false
	}
	for i := 0; i < len(name); i++ {
		c := name[i]
		switch {
		case c >= 'a' && c <= 'z', c >= 'A' && c <= 'Z', c >= '0' && c <= '9':
		case strings.IndexByte(headerNameSymbols, c) >= 0:
		default:
			return false
		}
	}
	return true
}

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
// credentials, a bearer token in Sec-WebSocket-Protocol, Audit-ID, identity headers, the
// headers extra names (Config.ExtraCallerHeaders, in lower case), the headers the caller
// named in Connection, Connection
// itself, and Upgrade unless preserveUpgrade. It runs before the handler sets any header of
// its own: the reverse proxy drops every header Connection names, so a caller listing one
// the handler sets could otherwise make it disappear.
func sanitizeRequestHeaders(h http.Header, extra map[string]struct{}, preserveUpgrade bool) {
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
		if _, strip := extra[lower]; strip {
			delete(h, key)
			continue
		}
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
