package gateway

import (
	"encoding/json"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The pull requests a stopped conversation's cards recorded (#4061): named
// after both stops' replies, and only when well formed.

func writePR(verb string, number int, url string) lib.CardStopWrite {
	return lib.CardStopWrite{Kind: lib.CardStopWritePullRequest, Verb: verb, URL: url, Repo: "acme/gitops", Number: number}
}

var (
	opened412  = writePR(lib.CardStopWriteOpened, 412, "https://github.com/acme/gitops/pull/412")
	opened413  = writePR(lib.CardStopWriteOpened, 413, "https://github.com/acme/gitops/pull/413")
	updated414 = writePR(lib.CardStopWriteUpdated, 414, "https://github.com/acme/gitops/pull/414")
)

func TestSlackStopReplyNamesThePullRequestsAlreadyOpened(t *testing.T) {
	stopped := []lib.CardStopCard{{ID: "t_1", Title: "scale checkout", PriorStatus: "running"}}
	for _, tc := range []struct {
		name string
		r    lib.CardStopResult
		turn bool
		want string
	}{
		{"one opened", lib.CardStopResult{Stopped: stopped, Writes: []lib.CardStopWrite{opened412}}, false,
			"Stopped. I'd already opened a pull request before you stopped me: [#412](https://github.com/acme/gitops/pull/412)."},
		{"two opened", lib.CardStopResult{Stopped: stopped, Writes: []lib.CardStopWrite{opened412, opened413}}, false,
			"Stopped. I'd already opened pull requests before you stopped me: [#412](https://github.com/acme/gitops/pull/412) and [#413](https://github.com/acme/gitops/pull/413)."},
		{"opened and updated", lib.CardStopResult{Stopped: stopped, Writes: []lib.CardStopWrite{updated414, opened412}}, false,
			"Stopped. I'd already opened a pull request before you stopped me: [#412](https://github.com/acme/gitops/pull/412). " +
				"I'd already updated a pull request before you stopped me: [#414](https://github.com/acme/gitops/pull/414)."},
		{"the card had finished", lib.CardStopResult{Settled: stopped, Writes: []lib.CardStopWrite{opened412}}, false,
			"Nothing was running. I'd already opened a pull request before you stopped me: [#412](https://github.com/acme/gitops/pull/412)."},
		{"could not stop", lib.CardStopResult{Stopped: stopped, Deadline: true, Writes: []lib.CardStopWrite{opened412}}, false,
			slackStopCouldntReply + " I'd already opened a pull request before you stopped me: [#412](https://github.com/acme/gitops/pull/412)."},
		{"no writes", lib.CardStopResult{Stopped: stopped}, false, "Stopped."},
	} {
		r := tc.r
		if got := slackStopReply(&r, tc.turn, false); got != tc.want {
			t.Errorf("%s:\n got %q\nwant %q", tc.name, got, tc.want)
		}
	}
}

func TestCardStopReplyNamesThePullRequestsAlreadyOpened(t *testing.T) {
	r := lib.CardStopResult{
		Stopped: []lib.CardStopCard{{ID: "t_1", Title: "scale checkout"}},
		Writes:  []lib.CardStopWrite{opened412, updated414},
	}
	want := "🛑 stopped 1 card: scale checkout; already opened a pull request: [#412](https://github.com/acme/gitops/pull/412); " +
		"already updated a pull request: [#414](https://github.com/acme/gitops/pull/414)"
	if got := cardStopReply(&r, false); got != want {
		t.Fatalf("reply\n got %q\nwant %q", got, want)
	}
}

// A write is model-adjacent data: anything but a well-formed github.com pull
// request whose number is its URL's is never named.
func TestCardStopWritesDropsAnythingMalformed(t *testing.T) {
	bad := []lib.CardStopWrite{
		writePR(lib.CardStopWriteOpened, 1, "https://evil.example/acme/gitops/pull/1"),
		writePR(lib.CardStopWriteOpened, 2, "https://github.com/acme/gitops/pull/2|click"),
		writePR(lib.CardStopWriteOpened, 3, "https://github.com/acme/gitops/pull/4"),
		writePR("merged", 5, "https://github.com/acme/gitops/pull/5"),
		{Kind: "branch", Verb: lib.CardStopWriteOpened, URL: "https://github.com/acme/gitops/pull/6", Number: 6},
		writePR(lib.CardStopWriteOpened, 7, "https://github.com/acme/gitops/pull/7)[x](https://evil.example"),
	}
	r := lib.CardStopResult{Writes: bad}
	if opened, updated := cardStopWrites(&r); opened != nil || updated != nil {
		t.Fatalf("named malformed writes: opened %+v, updated %+v", opened, updated)
	}
	if got := slackStopReply(&r, true, false); got != slackStopStoppedReply {
		t.Fatalf("reply named a malformed write: %q", got)
	}
}

// The writes ride the cardStop part and come back unchanged; an older
// executor's result, with none, reads as none recorded.
func TestCardStopPartCarriesWrites(t *testing.T) {
	part, err := lib.CardStopPart(lib.CardStop{CardStopResult: &lib.CardStopResult{Writes: []lib.CardStopWrite{opened412}}})
	if err != nil {
		t.Fatal(err)
	}
	got, ok := cardStopResultOf([]lib.Part{part})
	if !ok || len(got.Writes) != 1 || got.Writes[0] != opened412 {
		t.Fatalf("writes did not survive the part: %+v", got)
	}
	var wire map[string]map[string]json.RawMessage
	old, _ := lib.CardStopPart(lib.CardStop{CardStopResult: &lib.CardStopResult{}})
	if err := json.Unmarshal(old.Data, &wire); err != nil {
		t.Fatal(err)
	}
	if _, present := wire["cardStop"]["writes"]; present {
		t.Fatalf("an empty writes list went on the wire: %s", old.Data)
	}
}
