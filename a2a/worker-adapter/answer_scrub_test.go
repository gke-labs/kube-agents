package workeradapter

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/answerscrub"
)

// Synthetic values only: none of these is, or was copied from, a real
// credential.
const (
	synthSeenValue  = "synthetic-secret-value-0001"
	synthShapeValue = "synthetic-secret-value-0002"
)

// eventLine marshals one stream-json line the way the harness writes it.
func eventLine(t *testing.T, v any) string {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatalf("marshal event: %v", err)
	}
	return string(b)
}

// secretManifest is `kubectl get secret -o json` output carrying value under
// data, base64 as the API returns it.
func secretManifest(t *testing.T, value string) string {
	t.Helper()
	return eventLine(t, map[string]any{
		"apiVersion": "v1",
		"kind":       "Secret",
		"metadata":   map[string]any{"name": "app-db", "namespace": "default"},
		"type":       "Opaque",
		"data":       map[string]any{"db-auth": base64.StdEncoding.EncodeToString([]byte(value))},
	})
}

// toolResultLine is a harness user message carrying one tool_result whose
// content is the given JSON (a string or a list of blocks).
func toolResultLine(t *testing.T, content any) string {
	t.Helper()
	return eventLine(t, map[string]any{
		"type": "user",
		"message": map[string]any{"role": "user", "content": []any{map[string]any{
			"type": "tool_result", "tool_use_id": "toolu_synthetic", "content": content,
		}}},
	})
}

func assistantTextLine(t *testing.T, text string) string {
	t.Helper()
	return eventLine(t, map[string]any{"type": "assistant", "message": map[string]any{
		"content": []any{map[string]any{"type": "text", "text": text}},
	}})
}

func resultLine(t *testing.T, text string) string {
	t.Helper()
	return eventLine(t, map[string]any{"type": "result", "subtype": "success", "result": text})
}

// linesStub is a harness that reports session sessionID, reads the opening
// prompt, and writes lines to stdout, one per line, from a file (so no shell
// quoting touches them).
func linesStub(t *testing.T, sessionID string, lines ...string) []string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "lines.jsonl")
	if err := os.WriteFile(path, []byte(strings.Join(lines, "\n")+"\n"), 0o600); err != nil {
		t.Fatalf("write lines: %v", err)
	}
	return stub(t, `
echo '{"type":"system","subtype":"init","session_id":"`+sessionID+`"}'
read first || exit 1
cat '`+path+`'
`)
}

// runLines runs one task against linesStub and returns the folded task and
// the terminal status.
func runLines(t *testing.T, session, taskID string, lines ...string) (*lib.Task, lib.StatusUpdate, Result) {
	t.Helper()
	url := startServer(t)
	c := testClient(t, url)
	submit(t, c, session, taskID, "show me the app's database secret")
	out := waitOutcome(t, runAdapter(context.Background(), adapterConfig(url, taskID, session, linesStub(t, "stub-"+taskID, lines...))), 30*time.Second)
	if out.err != nil {
		t.Fatalf("run: %v", out.err)
	}
	events := replayEvents(t, url, session, taskID)
	return foldTask(t, c, session, taskID), statusOf(t, events[len(events)-1]), out.res
}

// A Secret object a tool result carried feeds the task's seen set, and the
// value, echoed in the progress text and the result, is redacted in both;
// a result whose content is a list of text blocks feeds it the same way.
func TestToolResultSecretIsRedactedFromTheResultAndProgress(t *testing.T) {
	for name, content := range map[string]any{
		"string content":      secretManifest(t, synthSeenValue),
		"text block content":  []any{map[string]any{"type": "text", "text": secretManifest(t, synthSeenValue)}},
		"yaml string content": "apiVersion: v1\nkind: Secret\nmetadata:\n  name: app-db\ndata:\n  db-auth: " + base64.StdEncoding.EncodeToString([]byte(synthSeenValue)) + "\n",
	} {
		t.Run(name, func(t *testing.T) {
			task, _, res := runLines(t, "chat-heron-s1c1", "task-scrub-seen-"+strings.ReplaceAll(name, " ", "-"),
				toolResultLine(t, content),
				assistantTextLine(t, "the stored value is "+synthSeenValue),
				resultLine(t, "The database secret decodes to "+synthSeenValue+"."),
			)
			if res.State != lib.StateCompleted {
				t.Fatalf("state %q", res.State)
			}
			for _, artifact := range []string{lib.ArtifactResult, lib.ArtifactProgress} {
				got := artifactText(task, artifact)
				if strings.Contains(got, synthSeenValue) || !strings.Contains(got, answerscrub.Redacted) {
					t.Errorf("%s artifact = %q, want the seen value redacted", artifact, got)
				}
			}
		})
	}
}

// A credential written in a shape the shape rules know is redacted from the
// result and the progress text with nothing in the seen set.
func TestShapeRuleCredentialIsRedactedWithNoSeenValues(t *testing.T) {
	text := "start it with app --api-key=" + synthShapeValue + " -v"
	task, _, res := runLines(t, "chat-heron-s2c2", "task-scrub-shape",
		assistantTextLine(t, text),
		resultLine(t, text),
	)
	if res.State != lib.StateCompleted {
		t.Fatalf("state %q", res.State)
	}
	for _, artifact := range []string{lib.ArtifactResult, lib.ArtifactProgress} {
		if got := artifactText(task, artifact); strings.Contains(got, synthShapeValue) || !strings.Contains(got, answerscrub.Redacted) {
			t.Errorf("%s artifact = %q, want the credential redacted", artifact, got)
		}
	}
}

// Failure evidence (the partial output on a failed terminal) is scrubbed too.
func TestFailureEvidenceIsScrubbed(t *testing.T) {
	_, final, res := runLines(t, "chat-heron-s3c3", "task-scrub-evidence",
		toolResultLine(t, secretManifest(t, synthSeenValue)),
		eventLine(t, map[string]any{"type": "result", "subtype": "error_max_turns", "is_error": true,
			"result": "ran out of turns; the value was " + synthSeenValue}),
	)
	if res.State != lib.StateFailed {
		t.Fatalf("state %q", res.State)
	}
	if text := final.Status.Message.Parts[0].Text; strings.Contains(text, synthSeenValue) || !strings.Contains(text, answerscrub.Redacted) {
		t.Errorf("terminal text = %q, want the seen value redacted", text)
	}
}

// The seen set is the task's: in a reused pod, the second task's result
// echoing a value only the first task's tool result carried is not redacted,
// because nothing seen in one task carries into the next.
func TestSeenValuesDoNotCarryIntoTheNextTask(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session = "chat-heron-r4c4"
	dir := t.TempDir()
	write := func(n string, lines ...string) {
		if err := os.WriteFile(filepath.Join(dir, "lines."+n), []byte(strings.Join(lines, "\n")+"\n"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	write("1", toolResultLine(t, secretManifest(t, synthSeenValue)), resultLine(t, "first: "+synthSeenValue))
	write("2", resultLine(t, "second: "+synthSeenValue))
	harness := stub(t, `
dir='`+dir+`'
n=$(( $(cat "$dir/n" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$dir/n"
echo '{"type":"system","subtype":"init","session_id":"sess-'"$n"'"}'
read first || exit 1
cat "$dir/lines.$n"
`)
	submit(t, c, session, "task-carry-1", "first question")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := runAdapter(ctx, reuseConfig(url, "task-carry-1", session, harness))
	waitState(t, c, session, "task-carry-1", lib.StateCompleted)
	submit(t, c, session, "task-carry-2", "second question")
	waitState(t, c, session, "task-carry-2", lib.StateCompleted)
	cancel()
	waitOutcome(t, done, 30*time.Second)

	if got := artifactText(foldTask(t, c, session, "task-carry-1"), lib.ArtifactResult); got != "first: "+answerscrub.Redacted {
		t.Errorf("first task's result = %q, want the value it saw redacted", got)
	}
	if got := artifactText(foldTask(t, c, session, "task-carry-2"), lib.ArtifactResult); got != "second: "+synthSeenValue {
		t.Errorf("second task's result = %q, want it untouched: the first task's seen value carried over", got)
	}
}

// An answer that opens with unstructured reasoning is not published: the
// task ends failed with the bridge's token and notice and the harness
// session, and neither the result nor the reason carries the text. An answer
// that only mentions the word passes.
func TestUnstructuredReasoningAnswerIsWithheld(t *testing.T) {
	const marker = "synthetic plan the user must not see"
	task, final, res := runLines(t, "chat-heron-t5c5", "task-reasoning-withheld",
		resultLine(t, "thought:\n"+marker),
	)
	if res.State != lib.StateFailed || task.State != lib.StateFailed {
		t.Fatalf("state %q, folded %q, want failed", res.State, task.State)
	}
	if a := task.Artifact(lib.ArtifactResult); a != nil {
		t.Errorf("a withheld answer was published as the result: %+v", a)
	}
	reason := final.Status.Message.Parts[0].Text
	want := "reason: " + answerscrub.UnstructuredReasoningToken + " - " + answerscrub.UnstructuredReasoningNotice + "; session: stub-task-reasoning-withheld"
	if reason != want {
		t.Errorf("reason = %q, want %q", reason, want)
	}
	if strings.Contains(reason, marker) {
		t.Errorf("reason carries the withheld text: %q", reason)
	}

	task, _, res = runLines(t, "chat-heron-t6c6", "task-reasoning-normal",
		resultLine(t, "Thought about it: the cluster runs 1.33."),
	)
	if res.State != lib.StateCompleted {
		t.Fatalf("normal answer: state %q", res.State)
	}
	if got := artifactText(task, lib.ArtifactResult); got != "Thought about it: the cluster runs 1.33." {
		t.Errorf("normal answer result = %q", got)
	}
}

// A user message whose content is a plain string decodes as no blocks rather
// than failing the line, and toolResultRaw hands CollectToolResult a string
// as it is and a block list as its text blocks only.
func TestToolResultDecoding(t *testing.T) {
	var ev harnessEvent
	if err := json.Unmarshal([]byte(`{"type":"user","message":{"role":"user","content":"a prompt"}}`), &ev); err != nil {
		t.Fatalf("string content: %v", err)
	}
	if ev.Message == nil || len(ev.Message.Content) != 0 {
		t.Errorf("string content decoded as %+v, want no blocks", ev.Message)
	}
	if got := string(toolResultRaw(json.RawMessage(`"plain"`))); got != `"plain"` {
		t.Errorf("string content = %s", got)
	}
	got := string(toolResultRaw(json.RawMessage(`[{"type":"text","text":"a"},{"type":"image","source":{"data":"aW1n"}},{"type":"text","text":"b"}]`)))
	if got != `["a","b"]` {
		t.Errorf("block content = %s, want the text blocks only", got)
	}
}

// The stderr tail is scrubbed whole before it's cut to stderrTailBytes, so
// a credential that the cut would split is redacted, not left as a fragment.
func TestStderrTailIsScrubbedBeforeItIsCut(t *testing.T) {
	const value = "synthetic-secret-value-0001"
	line := "password=" + value + "\n"
	// Put the credential so the cut point falls inside its value.
	cut := stderrTailBytes - 10
	padding := strings.Repeat("x", 100) + "\n"
	tail := strings.Repeat("-", cut-len(value)/2) // after the credential
	stderr := &tailBuffer{max: stderrScanBytes}
	_, _ = stderr.Write([]byte(padding + line + tail))
	p := &harnessProc{stderr: stderr}
	got := p.stderrEvidence(answerscrub.ScrubInline)
	for i := 6; i <= len(value); i++ {
		if strings.Contains(got, value[len(value)-i:]) {
			t.Fatalf("the stderr tail kept a fragment of the credential: %q", got)
		}
	}
	if len(got) > len(stderrTailHeading)+stderrTailBytes {
		t.Fatalf("the tail is %d bytes, over stderrTailBytes", len(got)-len(stderrTailHeading))
	}
}

// assistantBlockLine is an assistant message carrying one content block.
func assistantBlockLine(t *testing.T, block map[string]any) string {
	t.Helper()
	return eventLine(t, map[string]any{"type": "assistant", "message": map[string]any{"role": "assistant", "content": []any{block}}})
}

// The model's thinking is scrubbed before it's published: a value seen in a
// tool result and echoed in the reasoning comes out redacted.
func TestASeenSecretEchoedInThinkingIsRedacted(t *testing.T) {
	task, _, res := runLines(t, "chat-heron-s7c7", "task-scrub-thinking",
		toolResultLine(t, secretManifest(t, synthSeenValue)),
		assistantBlockLine(t, map[string]any{"type": "thinking", "thinking": "the secret decodes to " + synthSeenValue + ", so I'll report it"}),
		resultLine(t, "The database secret exists."),
	)
	if res.State != lib.StateCompleted {
		t.Fatalf("state %q", res.State)
	}
	got := artifactText(task, lib.ArtifactThinking)
	if got == "" || strings.Contains(got, synthSeenValue) || !strings.Contains(got, answerscrub.Redacted) {
		t.Fatalf("thinking artifact = %q, want the seen value redacted", got)
	}
}

// An activity entry carries the tool and a short title, never the call's
// arguments: a token on a command line doesn't reach the activity artifact.
func TestAnActivityEntryCarriesNoRawArgument(t *testing.T) {
	cmd := "curl -H 'Authorization: Bearer " + synthShapeValue + "' https://example.invalid/api"
	task, _, res := runLines(t, "chat-heron-s8c8", "task-scrub-activity",
		assistantBlockLine(t, map[string]any{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": map[string]any{"command": cmd}}),
		assistantBlockLine(t, map[string]any{"type": "tool_use", "id": "toolu_2", "name": "Read", "input": map[string]any{"file_path": "/opt/data/notes/runbook.md"}}),
		resultLine(t, "done"),
	)
	if res.State != lib.StateCompleted {
		t.Fatalf("state %q", res.State)
	}
	art := task.Artifact(lib.ArtifactActivity)
	if art == nil || len(art.Parts) != 2 {
		t.Fatalf("activity artifact = %+v, want two entries", art)
	}
	want := []map[string]any{{"tool": "Bash", "title": "Bash curl"}, {"tool": "Read", "title": "Read runbook.md"}}
	for i, p := range art.Parts {
		var entry map[string]any
		if err := json.Unmarshal(p.Data, &entry); err != nil {
			t.Fatal(err)
		}
		if strings.Contains(string(p.Data), synthShapeValue) || strings.Contains(string(p.Data), "example.invalid") || entry["input"] != nil {
			t.Fatalf("activity entry %d = %s, want no arguments", i, p.Data)
		}
		if entry["tool"] != want[i]["tool"] || entry["title"] != want[i]["title"] {
			t.Fatalf("activity entry %d = %v, want %v", i, entry, want[i])
		}
	}
}

// A shell title never takes an argument: leading NAME=value words (a quoted
// one split on its spaces included) are skipped, and a subcommand counts only
// as a bare word, so a path, a URL or an assignment there leaves the program alone.
func TestAnActivityTitleSkipsAssignmentsAndNonWords(t *testing.T) {
	for cmd, want := range map[string]string{
		"TOKEN=" + synthShapeValue + " curl https://example.invalid": "Bash curl",
		"TOKEN='" + synthShapeValue + " more' curl -s x":             "Bash",
		"A=1 B=2 kubectl get pods":                                   "Bash kubectl get",
		"kubectl --token=" + synthShapeValue + " get pods":           "Bash kubectl",
		"curl https://example.invalid/" + synthShapeValue:            "Bash curl",
		"env FOO=" + synthShapeValue + " app":                        "Bash env",
		"/usr/bin/git status":                                        "Bash git status",
	} {
		input, err := json.Marshal(map[string]any{"command": cmd})
		if err != nil {
			t.Fatal(err)
		}
		got := activityTitle("Bash", input)
		if got != want {
			t.Errorf("activityTitle(%q) = %q, want %q", cmd, got, want)
		}
		if strings.Contains(got, synthShapeValue) || strings.Contains(got, "example.invalid") {
			t.Errorf("activityTitle(%q) = %q leaks an argument", cmd, got)
		}
	}
}
