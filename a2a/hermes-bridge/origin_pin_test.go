package hermesbridge

import (
	"os"
	"regexp"
	"testing"
)

// repoSessionKVServer is the Session KV server source, relative to this
// package. The origins the bridge records are checked against its ORIGINS
// tuple as written, not against a copy.
const repoSessionKVServer = "../../agents/platform/scripts/session_kv_server.py"

var (
	pyOriginsTuple = regexp.MustCompile(`(?m)^ORIGINS = \(([^)]*)\)$`)
	pyOriginWord   = regexp.MustCompile(`"([a-z]+)"`)
)

// TestBackendOriginsArePinnedToTheSessionKVServer fails when the server
// renames or drops an origin the bridge writes. The server answers such a PUT
// with 400, and the bridge only logs it.
func TestBackendOriginsArePinnedToTheSessionKVServer(t *testing.T) {
	raw, err := os.ReadFile(repoSessionKVServer)
	if err != nil {
		t.Fatal(err)
	}
	match := pyOriginsTuple.FindSubmatch(raw)
	if match == nil {
		t.Fatalf("%s: no `ORIGINS = (...)` line; update this reader", repoSessionKVServer)
	}
	known := map[string]bool{}
	for _, word := range pyOriginWord.FindAllSubmatch(match[1], -1) {
		known[string(word[1])] = true
	}
	if !known[originChat] {
		t.Fatalf("%s: ORIGINS read as %v, which lacks %q: the reader matched nothing useful", repoSessionKVServer, known, originChat)
	}
	for _, origin := range []string{originChat, originConsole, originAPI} {
		if !known[origin] {
			t.Errorf("origin %q is not in the Session KV server's ORIGINS %v", origin, known)
		}
	}
	for backend, origin := range backendOrigins {
		if !known[origin.Origin] {
			t.Errorf("backend %q records origin %q, which is not in the Session KV server's ORIGINS", backend, origin.Origin)
		}
	}
}
