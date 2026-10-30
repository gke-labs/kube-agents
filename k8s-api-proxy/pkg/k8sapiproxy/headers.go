package k8sapiproxy

import (
	"net/http"
	"strings"
)

const (
	headerUpgrade    = "Upgrade"
	headerConnection = "Connection"
	upgradeToken     = "upgrade"
)

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
