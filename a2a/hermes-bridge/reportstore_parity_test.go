package hermesbridge

import (
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// With a Hermes session, lib.ReportAddresses yields the bridge's
// incidentAddresses, pair for pair and in order, so a report row and a
// session-kv incident row for the same reply sit at the same address.
func TestReportAddressesMatchTheBridgesIncidentAddresses(t *testing.T) {
	keys := []string{
		"console:abc", "slack:C0HOME/1712.0001", " slack:C0HOME/1712.0001 ", "slack:C0HOME", "slack:C0HOME/",
		"slack:dm/D123", "slack:dm/D123/1712.0001",
		"gchat:spaces/AAA/threads/BBB", "gchat:dm/spaces/DDD/threads/TTT", "gchat:dm/spaces/DDD",
		"gchat:space/spaces/AAA", "gchat:space/spaces/AAA/threads/BBB", "gchat:spaces/AAA", "gchat:/threads/x",
		"teams:a:1UxxASmI", "teams:19:group@thread.v2", "teams:19:chan@thread.tacv2",
		"teams:19:chan@thread.tacv2;messageid=1794603936403", "teams:19:chan@thread.tacv2;MessageId=1",
		"inject:bench-7", "discord:g/c", "", "  ",
	}
	for _, key := range keys {
		for _, replyTo := range []string{"", "task-1"} {
			want := incidentAddresses(key, "a2a-x", replyTo)
			got := lib.ReportAddresses(key, "a2a-x", replyTo)
			if len(got) != len(want) {
				t.Errorf("%q %q: lib %+v, bridge %+v", key, replyTo, got, want)
				continue
			}
			for i := range got {
				if got[i].ChatID != want[i].chatID || got[i].ThreadID != want[i].threadID {
					t.Errorf("%q %q [%d]: lib %+v, bridge %+v", key, replyTo, i, got[i], want[i])
				}
			}
		}
	}
}
