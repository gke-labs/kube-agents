package main

import (
	"os"
	"regexp"
	"strings"
	"testing"
)

// The broker's policy sources, relative to this package. The transform's
// read tables are checked against them as written, not against a copy.
const (
	repoCommandPolicy   = "../../../agents/platform/scripts/command_policy.py"
	repoCredentialProxy = "../../../agents/platform/scripts/credential_proxy.py"
)

var (
	pyComment = regexp.MustCompile(`(?m)#.*$`)
	pyTuple   = regexp.MustCompile(`\(([^()]*)\)`)
	pyString  = regexp.MustCompile(`"([^"]*)"`)
)

// pyTupleSet reads a module-level `NAME... = frozenset(...)` of string
// tuples from Python source and returns each tuple as its words joined by a
// space. It fails the test rather than return an empty set, and the caller
// names an entry the set must hold: a reader that silently matched nothing
// would pass every subset check below.
func pyTupleSet(t *testing.T, path, name, known string) map[string]bool {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	src := pyComment.ReplaceAllString(string(raw), "")
	start := regexp.MustCompile(`(?m)^` + regexp.QuoteMeta(name) + `\b[^=\n]*=\s*frozenset\(`).FindStringIndex(src)
	if start == nil {
		t.Fatalf("%s: no `%s = frozenset(` assignment", path, name)
	}
	depth, end := 1, -1
	for i := start[1]; i < len(src) && end < 0; i++ {
		switch src[i] {
		case '(':
			depth++
		case ')':
			if depth--; depth == 0 {
				end = i
			}
		}
	}
	if end < 0 {
		t.Fatalf("%s: %s is not closed", path, name)
	}
	set := map[string]bool{}
	for _, tup := range pyTuple.FindAllStringSubmatch(src[start[1]:end], -1) {
		var words []string
		for _, s := range pyString.FindAllStringSubmatch(tup[1], -1) {
			words = append(words, s[1])
		}
		if len(words) > 0 {
			set[strings.Join(words, " ")] = true
		}
	}
	if len(set) == 0 || !set[known] {
		t.Fatalf("%s: read %d entries from %s, and not %q; the reader is not reading it", path, len(set), name, known)
	}
	return set
}

// Every command the transform leaves unmarked is one the preamble tells the
// model to run, so its read tables have to be a subset of what the broker
// lets a session run: command_policy.py's allowlists, less its refused
// subcommands and the verbs credential_proxy.py refuses the session role.
// The other direction (the broker allows, the transform marks) is the safe
// one and is not checked.
func TestReadTablesAreASubsetOfTheBroker(t *testing.T) {
	brokerKubectl := pyTupleSet(t, repoCommandPolicy, "KUBECTL_READ_VERBS", "get")
	refusedSub := pyTupleSet(t, repoCommandPolicy, "KUBECTL_REFUSED_SUBCOMMANDS", "cluster-info dump")
	sessionRefused := pyTupleSet(t, repoCredentialProxy, "SESSION_KUBECTL_REFUSED_VERBS", "rollout status")
	brokerGcloud := pyTupleSet(t, repoCommandPolicy, "GCLOUD_READ_COMMANDS", "container clusters describe")

	for cmd := range kubectlReadCommands {
		switch {
		case !brokerKubectl[cmd]:
			t.Errorf("kubectl %s is a read here and not in command_policy.py's KUBECTL_READ_VERBS", cmd)
		case refusedSub[cmd] || sessionRefused[cmd]:
			t.Errorf("kubectl %s is a read here and refused to a session by the broker", cmd)
		}
		if got := classifyCommand("kubectl " + cmd); got != blockRead {
			t.Errorf("kubectl %s is in the read table but classifies as %d", cmd, got)
		}
	}
	// A refused form whose first word is a read on its own would ride in on
	// that word unless the transform refuses it too.
	for cmd := range refusedSub {
		if got := classifyCommand("kubectl " + cmd); got == blockRead {
			t.Errorf("kubectl %s classifies as a read; command_policy.py refuses it", cmd)
		}
	}
	for cmd := range sessionRefused {
		if got := classifyCommand("kubectl " + cmd); got == blockRead {
			t.Errorf("kubectl %s classifies as a read; credential_proxy.py refuses it to a session", cmd)
		}
	}
	for cmd := range gcloudReadCommands {
		if !brokerGcloud[cmd] {
			t.Errorf("gcloud %s is a read here and not in command_policy.py's GCLOUD_READ_COMMANDS", cmd)
		}
		if got := classifyCommand("gcloud " + cmd); got != blockRead {
			t.Errorf("gcloud %s is in the read table but classifies as %d", cmd, got)
		}
	}
}

// The classifier admits only what the tables name: a group the broker has
// no entry for is a write, however its leaf verb reads.
func TestGcloudReadsMatchTheBrokersPathsNotALeafVerb(t *testing.T) {
	brokerGcloud := pyTupleSet(t, repoCommandPolicy, "GCLOUD_READ_COMMANDS", "container clusters describe")
	for _, cmd := range []string{
		"iam service-accounts describe sa", "sql instances list", "run services describe s",
		"container clusters update c", "compute instances delete i",
	} {
		words := strings.Fields(cmd)
		listed := false
		for n := 1; n <= len(words); n++ {
			listed = listed || brokerGcloud[strings.Join(words[:n], " ")]
		}
		if listed {
			t.Fatalf("gcloud %s is in the broker's list; this case no longer tests a refusal", cmd)
		}
		if got := classifyCommand("gcloud " + cmd); got == blockRead {
			t.Errorf("gcloud %s classifies as a read; the broker refuses it", cmd)
		}
	}
}
