package gateway

import (
	"encoding/json"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The pull requests a stopped conversation's cards had opened (#4061): named
// after both stops' replies, and only when well formed.

func openedPR(number int, url string) lib.CardStopWrite {
	return lib.CardStopWrite{Kind: lib.CardStopWritePullRequest, Verb: lib.CardStopWriteOpened, URL: url, Number: number}
}

var (
	opened412 = openedPR(412, "https://github.com/acme/gitops/pull/412")
	opened413 = openedPR(413, "https://github.com/acme/gitops/pull/413")
	openedMR7 = openedPR(7, "https://gitlab.example.com/group/sub/proj/-/merge_requests/7")
)

func TestSlackStopReplyNamesThePullRequestsAlreadyOpened(t *testing.T) {
	stopped := []lib.CardStopCard{{ID: "t_1", Title: "scale checkout", PriorStatus: "running"}}
	for _, tc := range []struct {
		name string
		r    lib.CardStopResult
		want string
	}{
		{"one opened", lib.CardStopResult{Stopped: stopped, Writes: []lib.CardStopWrite{opened412}},
			"Stopped. I'd already opened a pull request before you stopped me: [#412](https://github.com/acme/gitops/pull/412)."},
		{"two opened and a merge request", lib.CardStopResult{Stopped: stopped, Writes: []lib.CardStopWrite{opened412, opened413, openedMR7}},
			"Stopped. I'd already opened pull requests before you stopped me: [#412](https://github.com/acme/gitops/pull/412), " +
				"[#413](https://github.com/acme/gitops/pull/413) and [!7](https://gitlab.example.com/group/sub/proj/-/merge_requests/7)."},
		{"the card had finished", lib.CardStopResult{Settled: stopped, Writes: []lib.CardStopWrite{opened412}},
			"Nothing was running. I'd already opened a pull request before you stopped me: [#412](https://github.com/acme/gitops/pull/412)."},
		{"could not stop", lib.CardStopResult{Stopped: stopped, Deadline: true, Writes: []lib.CardStopWrite{opened412}},
			slackStopCouldntReply + " I'd already opened a pull request before you stopped me: [#412](https://github.com/acme/gitops/pull/412)."},
		{"no writes", lib.CardStopResult{Stopped: stopped}, "Stopped."},
	} {
		r := tc.r
		if got := slackStopReply(&r, false, false); got != tc.want {
			t.Errorf("%s:\n got %q\nwant %q", tc.name, got, tc.want)
		}
	}
}

func TestCardStopReplyNamesThePullRequestsAlreadyOpened(t *testing.T) {
	r := lib.CardStopResult{
		Settled: []lib.CardStopCard{{ID: "t_1", Title: "scale checkout"}},
		Writes:  []lib.CardStopWrite{opened412},
	}
	want := nothingRunningNotice + "; already finished: scale checkout; already opened a pull request: [#412](https://github.com/acme/gitops/pull/412)"
	if got := cardStopReply(&r, false); got != want {
		t.Fatalf("reply\n got %q\nwant %q", got, want)
	}
}

// A write is passed on from the executor: anything but a well-formed pull or
// merge request url, opened, whose number is its url's, is never named.
func TestCardStopWritesDropsAnythingMalformed(t *testing.T) {
	bad := []lib.CardStopWrite{
		openedPR(1, "http://github.com/acme/gitops/pull/1"),
		openedPR(2, "https://github.com/acme/gitops/pull/2|click"),
		openedPR(3, "https://github.com/acme/gitops/pull/4"),
		{Kind: lib.CardStopWritePullRequest, Verb: "merged", URL: "https://github.com/acme/gitops/pull/5", Number: 5},
		{Kind: "branch", Verb: lib.CardStopWriteOpened, URL: "https://github.com/acme/gitops/pull/6", Number: 6},
		openedPR(7, "https://github.com/acme/gitops/pull/7)[x](https://evil.example"),
		openedPR(8, "https://github.com/acme/gitops/issues/8"),
		openedPR(9, "https://github.com/<!here>/pull/9"),
	}
	r := lib.CardStopResult{Writes: bad}
	if opened := cardStopWrites(&r); opened != nil {
		t.Fatalf("named malformed writes: %+v", opened)
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
	empty, _ := lib.CardStopPart(lib.CardStop{CardStopResult: &lib.CardStopResult{}})
	if err := json.Unmarshal(empty.Data, &wire); err != nil {
		t.Fatal(err)
	}
	if _, present := wire["cardStop"]["writes"]; present {
		t.Fatalf("an empty writes list went on the wire: %s", empty.Data)
	}
}
