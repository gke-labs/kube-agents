package k8sapiproxy

import (
	"net/http"
	"strings"
)

// HasUpgradeHeader returns true when r requests a protocol upgrade (such as SPDY or WebSocket).
func HasUpgradeHeader(r *http.Request) bool {
	if strings.TrimSpace(r.Header.Get("Upgrade")) != "" {
		return true
	}
	for _, connVal := range r.Header.Values("Connection") {
		for _, token := range strings.Split(connVal, ",") {
			if strings.EqualFold(strings.TrimSpace(token), "upgrade") {
				return true
			}
		}
	}
	return false
}

// SanitizeRequestHeaders removes caller credentials, protocol upgrades, and Kubernetes impersonation headers from h.
func SanitizeRequestHeaders(h http.Header) {
	SanitizeRequestHeadersWithOptions(h, false)
}

// SanitizeRequestHeadersWithOptions removes caller credentials, hop-by-hop Connection tokens, and Kubernetes impersonation headers from h,
// and removes Upgrade unless preserveUpgrade is true.
func SanitizeRequestHeadersWithOptions(h http.Header, preserveUpgrade bool) {
	h.Del("Authorization")
	h.Del("Proxy-Authorization")
	h.Del("Cookie")
	h.Del("Connection")
	if !preserveUpgrade {
		h.Del("Upgrade")
	} else if strings.TrimSpace(h.Get("Upgrade")) != "" {
		h.Set("Connection", "Upgrade")
	}
	for key := range h {
		lower := strings.ToLower(key)
		if strings.HasPrefix(lower, "impersonate-") || strings.HasPrefix(lower, "x-remote-") {
			h.Del(key)
		}
	}
}

// AppendCallerUserAgent appends the kproxy/<caller> provenance token to the outbound User-Agent header.
func AppendCallerUserAgent(h http.Header, caller *CallerIdentity) {
	if h == nil || caller == nil {
		return
	}
	username := strings.TrimSpace(caller.Username)
	if username == "" {
		return
	}
	suffix := "kproxy/" + username
	current := strings.TrimSpace(h.Get("User-Agent"))
	if current == "" {
		h.Set("User-Agent", suffix)
		return
	}
	if !strings.Contains(current, suffix) {
		h.Set("User-Agent", current+" "+suffix)
	}
}
