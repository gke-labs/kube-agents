package gateway

import (
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The notice a conversation reads never carries the executors' plumbing:
// the `reason:` prefix and the token stay out of user text
// (.agents/rules/chat_ux.md): a known token reads as its plain sentence, an
// unknown one as its detail cut before any session id, and plain text as
// itself. One case per input class: a known and an unknown token with and
// without detail, plain text, empty.
func TestReasonInPlainWords(t *testing.T) {
	for _, tc := range []struct {
		name   string
		reason string
		want   string
		shaped bool
	}{
		{"known token with detail reads as its sentence", "reason: hermes-unstructured-reasoning - the agent's reply was withheld because it contained unstructured model reasoning instead of an answer; session: s-1; turn 3",
			reasonSentences["hermes-unstructured-reasoning"], true},
		{"known token with an operator's detail reads as its sentence", "reason: hermes-api-failed - HTTP 500; session: s-1; error: upstream - timed out",
			reasonSentences["hermes-api-failed"], true},
		{"detail-shown token reads as its detail", "reason: verifier-refused - the verifier does not allow this agent to run on prod-east",
			"the verifier does not allow this agent to run on prod-east", true},
		{"detail-shown token's detail keeps its own dashes and stops before a session id", "reason: capability-refused - the chain does not terminate at a root - the gateway minted none; session: s-1; turn 2",
			"the chain does not terminate at a root - the gateway minted none", true},
		{"unknown token with detail reads as nothing", "reason: something-new - exec: /usr/bin/x: no such file", "", true},
		{"known token without detail", "reason: hermes-unstructured-reasoning",
			reasonSentences["hermes-unstructured-reasoning"], true},
		{"known token with words but no detail separator", "reason: bus-publish-failed at working",
			reasonSentences["bus-publish-failed"], true},
		{"known token with an empty detail", "reason: spawn-failed - ", reasonSentences["spawn-failed"], true},
		{"unknown token without detail", "reason: something-new", "", true},
		{"malformed token without detail", "reason: ", "", true},
		{"plain text", "session pod exited without a terminal event; declared failed by its supervisor",
			"session pod exited without a terminal event; declared failed by its supervisor", false},
		{"empty", "", "", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got, shaped := reasonInPlainWords(tc.reason)
			if got != tc.want || shaped != tc.shaped {
				t.Fatalf("reasonInPlainWords(%q) = %q, %v; want %q, %v", tc.reason, got, shaped, tc.want, tc.shaped)
			}
		})
	}
}

func TestTerminalNoticeCarriesNoToken(t *testing.T) {
	for _, tc := range []struct {
		name   string
		state  lib.TaskState
		reason string
		want   string
	}{
		{"failed with a known token and detail", lib.StateFailed, "reason: spawn-failed - exec: no such file",
			"❌ failed — " + reasonSentences["spawn-failed"]},
		{"failed with a detail-shown token", lib.StateFailed, "reason: queue-timeout - no worker took the task within 10m",
			"❌ failed — no worker took the task within 10m"},
		{"failed with an unknown token and a raw detail", lib.StateFailed, "reason: something-new - dial unix /run/x.sock: no such file",
			"❌ the task failed"},
		{"failed with a known bare token", lib.StateFailed, "reason: bridge-queue-overflow",
			"❌ failed — " + reasonSentences["bridge-queue-overflow"]},
		{"failed with an unknown bare token", lib.StateFailed, "reason: something-new", "❌ the task failed"},
		{"failed with plain text", lib.StateFailed, "executor died", "❌ failed: executor died"},
		{"failed with nothing", lib.StateFailed, "", "❌ the task failed"},
		{"rejected with its detail", lib.StateRejected, "reason: capability-refused - the verifier could not be reached",
			"🚫 the executor rejected the task — the verifier could not be reached"},
		{"rejected with a dispatcher's detail", lib.StateRejected, "reason: parameters-invalid - the cluster name is empty",
			"🚫 the executor rejected the task — the cluster name is empty"},
		{"rejected with a known token and the executor's detail", lib.StateRejected, "reason: no-text-parts - the submission message carries nothing hermes can be asked",
			"🚫 the executor rejected the task — " + reasonSentences["no-text-parts"]},
		{"rejected with an unknown bare token", lib.StateRejected, "reason: something-new", "🚫 the executor rejected the task"},
		{"rejected with plain text", lib.StateRejected, "no capability", "🚫 the executor rejected the task: no capability"},
		{"rejected with nothing", lib.StateRejected, "", "🚫 the executor rejected the task"},
		{"canceled ignores the reason", lib.StateCanceled, "reason: canceled-by-request", "🛑 canceled"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got := terminalNotice(tc.state, tc.reason)
			if got != tc.want {
				t.Fatalf("terminalNotice(%s, %q) = %q, want %q", tc.state, tc.reason, got, tc.want)
			}
			if strings.Contains(got, reasonPrefix) {
				t.Fatalf("the notice carries the reason prefix: %q", got)
			}
		})
	}
}

// Every sentence reads as one plain line: no token, no `reason:`, and
// every token in the map is one the executors can write (reasonToken
// accepts it).
func TestReasonSentencesAreUserText(t *testing.T) {
	for token := range reasonDetailShown {
		if reasonToken(reasonPrefix+token) != token {
			t.Errorf("%q is not a reason token", token)
		}
		if _, both := reasonSentences[token]; both {
			t.Errorf("%q has a sentence and shows its detail", token)
		}
	}
	for token, sentence := range reasonSentences {
		if reasonToken(reasonPrefix+token) != token {
			t.Errorf("%q is not a reason token", token)
		}
		if sentence == "" || strings.Contains(sentence, token) || strings.Contains(sentence, reasonPrefix) || strings.Contains(sentence, "\n") {
			t.Errorf("the sentence for %q is not one plain line: %q", token, sentence)
		}
	}
}
