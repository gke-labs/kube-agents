package authcallout

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// natsSubjectMatches reports whether the NATS subject pattern pat (with *
// and > wildcards) matches the literal subject subj.
func natsSubjectMatches(pat, subj string) bool {
	p, s := strings.Split(pat, "."), strings.Split(subj, ".")
	for i, tok := range p {
		if tok == ">" {
			return len(s) > i
		}
		if i >= len(s) || (tok != "*" && tok != s[i]) {
			return false
		}
	}
	return len(p) == len(s)
}

// Only the dispatcher may publish on a worker's dispatch subject, in every
// identity map the operator renders: a confirm there is where a script-run
// task's script, path and credentials come from (#5622), so its publisher
// must be the one component that checked them. Workers read it through
// APP's import and answer on .accept and .lease only.
func TestOnlyTheDispatcherPublishesAConfirm(t *testing.T) {
	files, err := filepath.Glob("testdata/rendered-identity-map*.json")
	if err != nil || len(files) == 0 {
		t.Fatalf("no rendered identity maps: %v", err)
	}
	confirm := lib.DispatchSubject("tse", "unattended", "tse-unattended-job-1")
	sawDispatcher := false
	for _, f := range files {
		raw, err := os.ReadFile(f)
		if err != nil {
			t.Fatal(err)
		}
		var m struct {
			Identities []struct {
				User   string `json:"user"`
				Grants struct {
					Publish []string `json:"publish"`
				} `json:"grants"`
			} `json:"identities"`
		}
		if err := json.Unmarshal(raw, &m); err != nil {
			t.Fatalf("%s: %v", f, err)
		}
		for _, id := range m.Identities {
			for _, p := range id.Grants.Publish {
				if !natsSubjectMatches(p, confirm) {
					continue
				}
				if id.User != "dispatcher" {
					t.Errorf("%s: %s may publish %s through %q", f, id.User, confirm, p)
				}
				sawDispatcher = true
			}
		}
	}
	if !sawDispatcher {
		t.Error("no rendered map lets the dispatcher publish a confirm, so this check proves nothing")
	}
	for pat, subj := range map[string]string{"a2a.dispatch.*.*.*": confirm, ">": confirm, "a2a.dispatch.>": confirm} {
		if !natsSubjectMatches(pat, subj) {
			t.Errorf("the matcher misses %q against %q", pat, subj)
		}
	}
	if natsSubjectMatches("a2a.dispatch.*.*.*", confirm+".accept") {
		t.Error("the matcher takes a pattern one token short")
	}
}
