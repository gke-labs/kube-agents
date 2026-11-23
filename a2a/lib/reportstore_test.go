package lib

import (
	"encoding/json"
	"reflect"
	"regexp"
	"strings"
	"testing"
	"time"
)

// A report row's key is one valid KV key under the prefix, whatever the
// address carries, and distinct addresses get distinct keys, the NUL
// keeping ("a", "b/c") apart from ("a/b", "c").
func TestReportKeyIsAValidKVKey(t *testing.T) {
	valid := regexp.MustCompile(`^report\.[0-9a-f]{64}$`)
	seen := map[string]ReportAddress{}
	for _, a := range []ReportAddress{
		{"C0HOME", "1712.0001"},
		{"spaces/AAA", "spaces/AAA/threads/BBB"},
		{"console:abc", "console:abc#task-1"},
		{"a", "b/c"},
		{"a/b", "c"},
		{"", ""},
		{"19:chan@thread.tacv2;messageid=1", "19:chan@thread.tacv2;messageid=1"},
	} {
		k := a.Key()
		if !valid.MatchString(k) || k != ReportKey(a.ChatID, a.ThreadID) {
			t.Errorf("%+v: key %q is not one report key", a, k)
		}
		if prev, dup := seen[k]; dup {
			t.Errorf("%+v and %+v share key %q", a, prev, k)
		}
		seen[k] = a
	}
	if !strings.HasPrefix(ReportKeysFilter, ReportKeyPrefix) || ReportKeysFilter != "report.>" {
		t.Fatalf("filter %q", ReportKeysFilter)
	}
}

// The order and the guards, with a Hermes session (the bridge's order) and
// without one (the gateway's).
func TestReportAddresses(t *testing.T) {
	cases := []struct {
		key, session, replyTo string
		want                  []ReportAddress
	}{
		{"console:abc", "a2a-x", "task-1", []ReportAddress{{"a2a-x", "console:abc#task-1"}, {"a2a-x", "console:abc"}}},
		{"console:abc", "", "task-1", []ReportAddress{{"console:abc", "console:abc#task-1"}}},
		{"console:abc", "", "", nil},
		{"console:abc", "a2a-x", "", []ReportAddress{{"a2a-x", "console:abc"}}},
		{"slack:C0HOME/1712.0001", "a2a-x", "task-1", []ReportAddress{{"a2a-x", "slack:C0HOME/1712.0001#task-1"}, {"C0HOME", "1712.0001"}, {"a2a-x", "slack:C0HOME/1712.0001"}}},
		{"slack:C0HOME/1712.0001", "", "task-1", []ReportAddress{{"slack:C0HOME/1712.0001", "slack:C0HOME/1712.0001#task-1"}, {"C0HOME", "1712.0001"}}},
		{"slack:C0HOME/1712.0001", "", "", []ReportAddress{{"C0HOME", "1712.0001"}}},
		{" slack:C0HOME/1712.0001 ", "", "", []ReportAddress{{"C0HOME", "1712.0001"}}},
		{"gchat:spaces/AAA/threads/BBB", "", "", []ReportAddress{{"spaces/AAA", "spaces/AAA/threads/BBB"}}},
		{"gchat:dm/spaces/DDD/threads/TTT", "", "", []ReportAddress{{"spaces/DDD", "spaces/DDD/threads/TTT"}}},
		{"teams:19:chan@thread.tacv2;messageid=17", "", "", []ReportAddress{{"19:chan@thread.tacv2;messageid=17", "19:chan@thread.tacv2;messageid=17"}}},
		// A thread in a Slack DM is its own conversation but has no stored
		// home-channel post.
		{"slack:dm/D123/1712.0001", "", "", nil},
		{"slack:dm/D123/1712.0001", "a2a-x", "", []ReportAddress{{"a2a-x", "slack:dm/D123/1712.0001"}}},
		// Whole conversations take none, task named or not.
		{"slack:dm/D123", "a2a-x", "task-1", nil},
		{"slack:dm/D123", "", "task-1", nil},
		{"gchat:dm/spaces/DDD", "", "", nil},
		{"gchat:space/spaces/AAA", "", "task-1", nil},
		{"gchat:space/spaces/AAA/threads/BBB", "", "", nil},
		{"teams:a:1UxxASmI", "", "", nil},
		{"teams:19:chan@thread.tacv2", "a2a-x", "task-1", nil},
		{"", "a2a-x", "task-1", nil},
		{"  ", "", "task-1", nil},
	}
	for _, tc := range cases {
		got := ReportAddresses(tc.key, tc.session, tc.replyTo)
		if !reflect.DeepEqual(got, tc.want) {
			t.Errorf("ReportAddresses(%q, %q, %q) = %+v, want %+v", tc.key, tc.session, tc.replyTo, got, tc.want)
		}
	}
}

// The row's JSON field names are the ones another writer of the record
// reads, and posted_at is RFC 3339.
func TestReportRecordJSON(t *testing.T) {
	at := time.Date(2026, 11, 23, 10, 4, 5, 123000000, time.UTC)
	raw, err := json.Marshal(ReportRecord{Text: "t", Source: ReportSourceRelay, PostedAt: at, Conversation: "slack:C/1.2", Root: "", ChatID: "C", ThreadID: "1.2"})
	if err != nil {
		t.Fatal(err)
	}
	var fields map[string]any
	if err := json.Unmarshal(raw, &fields); err != nil {
		t.Fatal(err)
	}
	for _, f := range []string{"text", "source", "posted_at", "conversation", "root", "chat_id", "thread_id"} {
		if _, ok := fields[f]; !ok {
			t.Errorf("field %q missing from %s", f, raw)
		}
	}
	if _, ok := fields["epoch"]; ok {
		t.Errorf("an unelected row carries an epoch: %s", raw)
	}
	if _, err := time.Parse(time.RFC3339, fields["posted_at"].(string)); err != nil {
		t.Errorf("posted_at %q is not RFC 3339: %v", fields["posted_at"], err)
	}
}
