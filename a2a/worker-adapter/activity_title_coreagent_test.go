package workeradapter

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/coreagentwire"
)

// titleOf is activityTitle for tool with input marshalled from args.
func titleOf(t *testing.T, tool string, args map[string]any) string {
	t.Helper()
	input, err := json.Marshal(args)
	if err != nil {
		t.Fatal(err)
	}
	return activityTitle(tool, input)
}

// A core-agent tool call's title names its subject the way a Claude Code
// call's does: a shell command's program and verb, a single-file tool's file
// name. A tool whose argument is a pattern or a list, as Claude Code's Glob,
// Grep and LS, keeps its bare name.
func TestACoreAgentActivityTitleNamesItsSubject(t *testing.T) {
	for _, c := range []struct {
		tool string
		args map[string]any
		want string
	}{
		{"bash", map[string]any{"command": "kubectl get pods -n checkout"}, "bash kubectl get"},
		{"bash", map[string]any{"command": "/usr/bin/git status"}, "bash git status"},
		{"bash", map[string]any{"command": "python3 /var/lib/agent-persona/skills/vcs/scripts/vcs.py status"}, "bash python3"},
		{"bash", map[string]any{}, "bash"},
		{"read_file", map[string]any{"path": "/scratch/clusters/seeded-a/deploy.yaml"}, "read_file deploy.yaml"},
		{"write_file", map[string]any{"path": "/scratch/out/report.md", "content": "body"}, "write_file report.md"},
		{"edit_file", map[string]any{"path": "notes.txt"}, "edit_file notes.txt"},
		{"view_file_outline", map[string]any{"path": "/scratch/main.go"}, "view_file_outline main.go"},
		{"stat", map[string]any{"path": "/scratch/clusters"}, "stat clusters"},
		{"read_file", map[string]any{}, "read_file"},
		{"grep", map[string]any{"pattern": "checkout-gateway", "path": "/scratch"}, "grep"},
		{"glob", map[string]any{"pattern": "*.yaml", "path": "/scratch"}, "glob"},
		{"list_dir", map[string]any{"path": "/scratch/clusters"}, "list_dir"},
		{"read_many_files", map[string]any{"paths": []string{"/scratch/a.yaml"}}, "read_many_files"},
		{"cluster_kubectl_get", map[string]any{"resource": "pods"}, "cluster_kubectl_get"},
	} {
		if got := titleOf(t, c.tool, c.args); got != c.want {
			t.Errorf("activityTitle(%s, %v) = %q, want %q", c.tool, c.args, got, c.want)
		}
	}
}

// core-agent's shell title follows the same rules as Claude Code's: no
// argument, no assignment, no non-word reaches it. The same commands as the
// Bash cases give the same titles under core-agent's name.
func TestACoreAgentShellTitleTakesNoArgument(t *testing.T) {
	for cmd, want := range map[string]string{
		"TOKEN=" + synthShapeValue + " curl https://example.invalid": "bash curl",
		"TOKEN='" + synthShapeValue + " more' curl -s x":             "bash",
		"kubectl --token=" + synthShapeValue + " get pods":           "bash kubectl",
		"kubectl get pods --token=" + synthShapeValue:                "bash kubectl get",
		"curl -H 'Authorization: Bearer " + synthShapeValue + "' x":  "bash curl",
		"TOKEN=$(cat f) curl":                                        "bash",
		"echo " + synthShapeValue:                                    "bash echo",
	} {
		got := titleOf(t, "bash", map[string]any{"command": cmd})
		if got != want {
			t.Errorf("activityTitle(bash, %q) = %q, want %q", cmd, got, want)
		}
		if strings.Contains(got, synthShapeValue) || strings.Contains(got, "example.invalid") {
			t.Errorf("activityTitle(bash, %q) = %q leaks an argument", cmd, got)
		}
		if claude := titleOf(t, "Bash", map[string]any{"command": cmd}); claude != "Bash"+strings.TrimPrefix(want, "bash") {
			t.Errorf("activityTitle(Bash, %q) = %q, want the same title as core-agent's under Claude Code's name", cmd, claude)
		}
	}
}

// A secret in a core-agent call's command or path never reaches the
// published title: the title is scrubbed with the task's seen values as
// publishActivity scrubs it, so a seen value that a file is named after, or
// that a command passes as its verb, is redacted.
func TestACoreAgentActivityTitleIsScrubbedWithSeenValues(t *testing.T) {
	a := &adapter{}
	a.seen.Add(synthSeenValue)
	for _, c := range []struct {
		tool string
		args map[string]any
	}{
		{"read_file", map[string]any{"path": "/scratch/" + synthSeenValue}},
		{"write_file", map[string]any{"path": "/scratch/out/" + synthSeenValue, "content": "x"}},
		{"view_file_outline", map[string]any{"path": synthSeenValue}},
		{"stat", map[string]any{"path": "/var/run/" + synthSeenValue}},
		{"bash", map[string]any{"command": "git " + synthSeenValue}},
		{"bash", map[string]any{"command": synthSeenValue + " get"}},
	} {
		got := a.scrub(titleOf(t, c.tool, c.args))
		if strings.Contains(got, synthSeenValue) {
			t.Errorf("published title for %s %v = %q, want the seen value redacted", c.tool, c.args, got)
		}
		if !strings.HasPrefix(got, c.tool) {
			t.Errorf("published title for %s %v = %q, want it to still name the tool", c.tool, c.args, got)
		}
	}
}

// End to end through the core-agent driver: the activity entries a turn's
// tool calls publish carry core-agent's tool names with their subjects, and
// none of the calls' arguments.
func TestACoreAgentTurnsActivityTitlesNameTheirSubjects(t *testing.T) {
	url := startServer(t)
	cl := testClient(t, url)
	session, taskID := "chat-coreagent-titles", "task-coreagent-titles"
	submit(t, cl, session, taskID, "look")
	useRespawnScope(t)
	script := `
read first || exit 1
echo '{"type":"init","session":"s1","model":"m"}'
echo '{"type":"tool_call","id":"adk-1","tool":"bash","input":{"command":"kubectl get pods --token=ARG-SECRET-MARKER"}}'
echo '{"type":"tool_result","id":"adk-1","tool":"bash","text":"ok"}'
echo '{"type":"tool_call","id":"adk-2","tool":"read_file","input":{"path":"/scratch/clusters/deploy.yaml"}}'
echo '{"type":"tool_result","id":"adk-2","tool":"read_file","text":"ok"}'
echo '{"type":"tool_call","id":"adk-3","tool":"grep","input":{"pattern":"ARG-SECRET-MARKER","path":"/scratch"}}'
echo '{"type":"tool_result","id":"adk-3","tool":"grep","text":"none"}'
echo '{"type":"result","text":"done"}'
`
	cfg := adapterConfig(url, taskID, session, stub(t, script))
	cfg.Driver = NewCoreAgentDriver(coreagentwire.Policy{}, nil)
	if out := waitOutcome(t, runAdapter(context.Background(), cfg), 30*time.Second); out.err != nil {
		t.Fatalf("run: %v", out.err)
	}
	a := foldTask(t, cl, session, taskID).Artifact(lib.ArtifactActivity)
	if a == nil {
		t.Fatal("no activity artifact")
	}
	var titles []string
	for _, p := range a.Parts {
		if strings.Contains(string(p.Data), "ARG-SECRET-MARKER") {
			t.Errorf("activity entry %s carries a value from the call's arguments", p.Data)
		}
		var entry struct {
			Title string `json:"title"`
		}
		if err := json.Unmarshal(p.Data, &entry); err != nil {
			t.Fatal(err)
		}
		titles = append(titles, entry.Title)
	}
	want := []string{"bash kubectl get", "read_file deploy.yaml", "grep"}
	if strings.Join(titles, "|") != strings.Join(want, "|") {
		t.Errorf("activity titles %q, want %q", titles, want)
	}
}
