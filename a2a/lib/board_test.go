package lib

import (
	"encoding/json"
	"testing"
)

// Unlisted is omitempty: a row that is not a placeholder marshals to the
// bytes it did before the field existed, so an older gateway or sender sees
// no change, and a placeholder row round-trips with the flag.
func TestBoardClusterUnlistedKeepsTodaysBytes(t *testing.T) {
	got, err := json.Marshal(BoardCluster{Name: "web"})
	if err != nil {
		t.Fatal(err)
	}
	if want := `{"name":"web","findings":0}`; string(got) != want {
		t.Errorf("marshal = %s, want %s", got, want)
	}
	var back BoardCluster
	if err := json.Unmarshal([]byte(`{"name":"mgmt (clusters unread)","findings":0,"unlisted":true}`), &back); err != nil {
		t.Fatal(err)
	}
	if !back.Unlisted || back.Name != "mgmt (clusters unread)" {
		t.Errorf("unmarshal = %+v, want Unlisted set", back)
	}
	again, err := json.Marshal(back)
	if err != nil {
		t.Fatal(err)
	}
	if want := `{"name":"mgmt (clusters unread)","findings":0,"unlisted":true}`; string(again) != want {
		t.Errorf("round trip = %s, want %s", again, want)
	}
}
