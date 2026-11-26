package gateway

import "github.com/gke-labs/kube-agents/a2a/lib"

// The package's cutoff tests read many pre-cutoff terminals within a few
// seconds, each on a fresh server, so a stored-time read slot goes back as
// soon as its read returns rather than after the five-second threshold a
// live consumer needs. The bound itself is tested in lib.
func init() {
	lib.SetStoredTimeReadHoldForTest(func(release func()) { release() })
}
