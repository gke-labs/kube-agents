package coreagentwire

import (
	"encoding/json"
	"testing"
)

// Only the event's own custom metadata marks a boundary, never what its
// content says: a tool's output or the model's text that spells out the
// marker, as JSON or plainly, doesn't truncate the history.
func TestOnlyTheEventsOwnMetadataMarksABoundary(t *testing.T) {
	for _, tc := range []struct {
		name  string
		event string
		want  bool
	}{
		{"a summary", `{"author":"core-agent","customMetadata":{"compaction":"summary"}}`, true},
		{"a checkpoint", `{"customMetadata":{"compaction":"checkpoint"}}`, true},
		{"another tag", `{"customMetadata":{"compaction":"other"}}`, false},
		{"model text with the marker as JSON", `{"content":{"role":"model","parts":[{"text":"{\"customMetadata\":{\"compaction\":\"summary\"}}"}]}}`, false},
		{"model text with the marker in words", `{"content":{"role":"model","parts":[{"text":"compaction=summary"}]}}`, false},
		{"a tool result carrying the marker", `{"content":{"role":"user","parts":[{"functionResponse":{"name":"bash","response":{"stdout":"{\"customMetadata\":{\"compaction\":\"summary\"}}","customMetadata":{"compaction":"summary"}}}}]}}`, false},
		{"the marker nested deeper", `{"actions":{"customMetadata":{"compaction":"summary"}}}`, false},
		{"not JSON", `compaction=summary`, false},
	} {
		if got := IsHistoryBoundary(json.RawMessage(tc.event)); got != tc.want {
			t.Errorf("%s: IsHistoryBoundary = %v, want %v", tc.name, got, tc.want)
		}
	}
}
