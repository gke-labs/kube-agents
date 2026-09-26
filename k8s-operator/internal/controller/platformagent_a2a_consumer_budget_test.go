package controller

// maxSessions and the TASKS consumer budget.
//
// The CRD accepts maxSessions up to 10000. Every session pod creates three
// named consumers on TASKS, so a stream pinned at max_consumers=64 cannot hold
// more than about twenty sessions: the twenty-first session's consumer create
// is refused by the server, and the operator's spec said the number was
// allowed. These tests pin the two halves of the fix — the render derives the
// cap from the CR, and the provision script refuses an install whose LIVE
// stream cannot hold what the CR asks for, loudly and at configuration time.

import (
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"

	"github.com/nats-io/nats.go/jetstream"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const a2aSessionRolesSource = "../../../a2a/lib/session.go"

// a2aSessionConsumersPerSession is a copy of a number that lives in the a2a
// module, which this module cannot import. A copy goes stale silently, so this
// reads the original: it parses lib.SessionConsumerRoles and counts it.
//
// The parse is deliberately not a regex over the file. A regex that stops
// matching returns nothing, and "nothing" reads the same as "no roles", which
// would make this test pass by finding a count of zero on the day the
// declaration moves. Every step below fails the test instead of falling back
// to a default.
func TestSessionConsumerCountMatchesTheA2AModule(t *testing.T) {
	fset := token.NewFileSet()
	f, err := parser.ParseFile(fset, a2aSessionRolesSource, nil, 0)
	if err != nil {
		t.Fatalf("parse %s: %v (if the a2a module moved, this test's path must move with it — it is the only thing keeping a2aSessionConsumersPerSession honest)", a2aSessionRolesSource, err)
	}

	var roles *ast.CompositeLit
	ast.Inspect(f, func(n ast.Node) bool {
		spec, ok := n.(*ast.ValueSpec)
		if !ok {
			return true
		}
		for i, name := range spec.Names {
			if name.Name != "SessionConsumerRoles" || i >= len(spec.Values) {
				continue
			}
			if lit, ok := spec.Values[i].(*ast.CompositeLit); ok {
				roles = lit
			}
		}
		return true
	})
	if roles == nil {
		t.Fatalf("no `var SessionConsumerRoles = []string{...}` in %s; it is what a2aSessionConsumersPerSession counts, and this test cannot check a number it cannot find", a2aSessionRolesSource)
	}
	if len(roles.Elts) == 0 {
		t.Fatalf("SessionConsumerRoles parsed as empty in %s; an empty slice here would make every budget below zero-sized", a2aSessionRolesSource)
	}

	if len(roles.Elts) != a2aSessionConsumersPerSession {
		t.Errorf("lib.SessionConsumerRoles has %d roles, a2aSessionConsumersPerSession says %d: TASKS would be sized for the wrong number of consumers per session. Update the constant, the provision script's message, which quotes it, and docs/designs/spec-nats-deployment.md, which states it as 'a session pod creates three consumers there'.",
			len(roles.Elts), a2aSessionConsumersPerSession)
	}
}

// The derivation, including the floor.
//
// The floor matters as much as the arithmetic: 64 is what TASKS shipped with,
// so rendering below it on a small install would TIGHTEN a live stream's cap
// relative to today. Deriving downward is a silent capacity regression on
// somebody's working install; deriving upward is the thing being fixed. Hence
// max(64, budget), and hence the first row.
func TestTasksMaxConsumersDerivation(t *testing.T) {
	for _, tc := range []struct {
		name        string
		maxSessions *int
		wantBudget  int
		wantRender  int
	}{
		{"unset stays at the shipped cap", nil, 58, 64},
		{"below the floor does not tighten it", ptr.To(2), 34, 64},
		{"just under the floor still does not", ptr.To(12), 64, 64},
		{"the first value above the floor derives", ptr.To(13), 67, 67},
		{"above the floor derives", ptr.To(20), 88, 88},
		{"the CRD maximum", ptr.To(10000), 30028, 30028},
	} {
		t.Run(tc.name, func(t *testing.T) {
			agent := &agentv1alpha1.PlatformAgent{ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"}}
			if tc.maxSessions != nil {
				agent.Spec.Harness = &agentv1alpha1.HarnessSpec{Tuning: &agentv1alpha1.TuningSpec{MaxSessions: tc.maxSessions}}
			}
			if got := a2aTasksConsumerBudget(agent); got != tc.wantBudget {
				t.Errorf("budget = %d, want %d", got, tc.wantBudget)
			}
			if got := a2aTasksMaxConsumers(agent); got != tc.wantRender {
				t.Errorf("rendered max_consumers = %d, want %d", got, tc.wantRender)
			}
			if got := a2aTasksMaxConsumers(agent); got < a2aTasksMaxConsumersFloor {
				t.Errorf("rendered max_consumers = %d, below the shipped floor %d: this would tighten an existing install", got, a2aTasksMaxConsumersFloor)
			}
		})
	}
}

// The rendered flags, so a refactor that drops one is caught without running
// anything.
func TestTasksStreamCarriesItsLimits(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"}}
	sessions := 40
	agent.Spec.Harness = &agentv1alpha1.HarnessSpec{Tuning: &agentv1alpha1.TuningSpec{MaxSessions: &sessions}}
	script := a2aProvisionScript(agent)

	add, ok := streamAddInvocation(script, "TASKS")
	if !ok {
		t.Fatal("no `stream add TASKS` in the provision script; every assertion below would pass vacuously on a script that stopped creating it")
	}
	for _, want := range []string{
		"--max-msgs-per-subject=4096",
		"--max-consumers=148",
		"--discard=old",
	} {
		if !strings.Contains(add, want) {
			t.Errorf("stream add TASKS is missing %s\ngot: %s", want, add)
		}
	}
	// The per-subject cap is the one that changes replay, so it must not
	// spread to the append-only siblings by copy-paste. TOPICS-JOURNAL is
	// TASKS' retention-class twin and carries none.
	journal, ok := streamAddInvocation(script, "TOPICS-JOURNAL")
	if !ok {
		t.Fatal("no `stream add TOPICS-JOURNAL` in the provision script")
	}
	if strings.Contains(journal, "--max-msgs-per-subject") {
		t.Error("TOPICS-JOURNAL grew a per-subject cap; it is append-only and a cap there silently truncates a topic's history")
	}
}

// streamAddInvocation returns the `stream add <name>` command, joined onto one
// line through its backslash continuations.
func streamAddInvocation(script, stream string) (string, bool) {
	flat := strings.ReplaceAll(script, "\\\n", " ")
	for _, line := range strings.Split(flat, "\n") {
		if strings.Contains(line, "stream add "+stream+" ") {
			return strings.Join(strings.Fields(line), " "), true
		}
	}
	return "", false
}

// Proven by configuring it wrong: the script, executed, against a stream whose
// limits are not the ones this render would have created.
//
// Provisioning is create-only convergence — the `stream info X || stream add X`
// guards never edit a stream that already exists — so every limit the render
// has gained since an install's TASKS was created is absent from that install's
// stream, and a re-run does not add it. The script's closing block is what an
// operator hears about that, and the two limits get different treatment
// because the two gaps are different: a short max_consumers is a capacity
// shortfall whose only other symptom is a task failure, so it refuses — and
// refuses with exit 2, the status the Job's podFailurePolicy reads as "a retry
// reaches this same refusal"; an absent max_msgs_per_subject is a bound the
// install never had, and applying it would evict, so it reports and exits
// clean.
func TestProvisionReportsATasksStreamOlderThanItsRender(t *testing.T) {
	bash, err := exec.LookPath("bash")
	if err != nil {
		// Not skipped: this is the only test that executes the
		// script rather than reading it, and a skip would drop that
		// coverage silently on a runner without bash.
		//
		// bash is this test's interpreter and not the shipped one.
		// The provision Job runs `sh -c <script>` in
		// natsio/nats-box, so what the pod uses is that image's sh,
		// not bash. bash is chosen here because it has the
		// `set -o pipefail` the script opens with and dash does not;
		// the cost is that a bashism the script grew would pass here
		// and only fail in the pod.
		t.Fatalf("bash is required to execute the provision script (it uses `set -o pipefail`): %v", err)
	}

	agent := &agentv1alpha1.PlatformAgent{ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"}}
	sessions := 100
	agent.Spec.Harness = &agentv1alpha1.HarnessSpec{Tuning: &agentv1alpha1.TuningSpec{MaxSessions: &sessions}}

	for _, tc := range []struct {
		name       string
		liveJSON   string
		wantExit   int
		wantStderr []string
		notStderr  []string
	}{
		{
			// Exit 2, not 1, and the distinction is load-bearing: the
			// Job's podFailurePolicy fails the Job on the first pod
			// that returns 2 (buildA2AProvisionJob). Nothing about a
			// re-run moves either number, so twenty retries would be
			// ninety minutes of a CR reading Ready over a bus that
			// cannot hold the concurrency it advertises.
			name:     "a stream at the shipped cap cannot hold this CR",
			liveJSON: `{"name":"TASKS","max_consumers":64,"max_msgs_per_subject":4096}`,
			wantExit: 2,
			wantStderr: []string{
				"max_consumers=64", "needs 328",
				"stream configuration update can not change MaxConsumers",
				"lower spec.harness.tuning.maxSessions to at most 12",
				"delete the TASKS stream",
				"recreates TASKS at 328",
				"Delete the Job to re-run it now",
				// The recreate's third step. Deleting a stream
				// deletes every consumer on it, and the two
				// long-lived durables there -- the gateway's
				// event relay and the Hermes bridge's
				// bridge-<profile> -- are held by clients that
				// do not re-create them: both go through
				// lib.Client.SubscribeDurable, whose Consume
				// carries no jetstream.ConsumeErrHandler, so
				// nats.go stops the subscription on the
				// terminal ErrConsumerDeleted and logs nothing.
				// An operator who follows this remedy and stops
				// at the recreate is left with a gateway that
				// still spawns session pods and relays no
				// events, over a CR reading Ready.
				"kubectl rollout restart deployment/test-agent-a2a-gateway -n test-ns",
			},
			// What this refusal must never go back to naming. It
			// used to prescribe `nats stream edit TASKS
			// --max-consumers=N`, and nats-server refuses that:
			// server/stream.go answers any update that moves
			// MaxConsumers with "stream configuration update can
			// not change MaxConsumers", in 2.10 and 2.11 alike,
			// and the operator pins the bus to nats:2.10-alpine.
			// An operator who followed it got an error and a
			// stream no wider than before.
			//
			// The flag and not the command, deliberately: the
			// sibling max_msgs_per_subject report names a `nats
			// stream edit` that IS legal, so barring the command
			// here would bar a remedy that works.
			// And what the closing paragraph must never go
			// back to claiming. "Neither way out clears this
			// on its own" was true of the recreate and false
			// here: lowering maxSessions edits the CR, which
			// changes required_consumers in the render, which
			// moves the digest in the Job's name -- a new Job,
			// running by itself, with nothing to delete. An
			// operator told otherwise deletes a Job that was
			// about to be superseded anyway.
			notStderr: []string{"--max-consumers=", "Neither way out clears this on its own"},
		},
		{
			// Below the reserved block, where the first way out
			// does not exist at all. maxSessions carries
			// +kubebuilder:validation:Minimum=1 and one session
			// still needs 31 consumers, so no value of the field
			// fits a stream this narrow - offering "lower
			// maxSessions" here would be a remedy the API server
			// refuses. The script says why, and leaves the
			// recreate standing on its own.
			name:     "a stream too narrow for one session offers only the recreate",
			liveJSON: `{"name":"TASKS","max_consumers":8,"max_msgs_per_subject":4096}`,
			wantExit: 2,
			wantStderr: []string{
				"max_consumers=8", "needs 328",
				"minimum is 1, and one session still needs 31",
				"That leaves deleting the TASKS stream",
				"recreates TASKS at 328",
				// The only way out here is the recreate, so the
				// restart it needs is not optional detail.
				"kubectl rollout restart deployment/test-agent-a2a-gateway -n test-ns",
			},
			// The lowering branch's "finishes on its own" note
			// is gated on the same `fits` test that picked this
			// branch, and offering it here would point at a
			// value the API server refuses (Minimum=1).
			notStderr: []string{"So either lower", "to at most", "--max-consumers=", "finishes on its own"},
		},
		{
			// The boundary between the two branches above, and the
			// only place they can be told apart: 31 is the
			// narrowest stream that has room for a legal
			// maxSessions at all - the 28 reserved plus one
			// session's 3 - so it takes the "lower it" branch with
			// nothing to spare, and 30 takes the other one. A gate
			// off by one in either direction sends one of these
			// two cases down the wrong branch, and only a pair
			// sitting on the seam catches that.
			name:     "the narrowest stream that a legal maxSessions still fits",
			liveJSON: `{"name":"TASKS","max_consumers":31,"max_msgs_per_subject":4096}`,
			wantExit: 2,
			wantStderr: []string{
				"max_consumers=31",
				"lower spec.harness.tuning.maxSessions to at most 1 -",
				"delete the TASKS stream",
				"kubectl rollout restart deployment/test-agent-a2a-gateway -n test-ns",
			},
			// Both branches are on offer here, so both halves of
			// the split have to be: the recreate's restart above,
			// and no claim that the lowering half needs a Job
			// deleted to take effect.
			notStderr: []string{"one session still needs 31", "--max-consumers=", "Neither way out clears this on its own"},
		},
		{
			name:     "one consumer short of that, and lowering stops being a way out",
			liveJSON: `{"name":"TASKS","max_consumers":30,"max_msgs_per_subject":4096}`,
			wantExit: 2,
			wantStderr: []string{
				"max_consumers=30",
				"minimum is 1, and one session still needs 31",
				"kubectl rollout restart deployment/test-agent-a2a-gateway -n test-ns",
			},
			notStderr: []string{"So either lower", "to at most", "--max-consumers=", "finishes on its own"},
		},
		{
			name:      "a stream sized for it passes",
			liveJSON:  `{"name":"TASKS","max_consumers":328,"max_msgs_per_subject":4096}`,
			wantExit:  0,
			notStderr: []string{"max_consumers", "max_msgs_per_subject"},
		},
		{
			name:      "an operator who set it unlimited is not second-guessed",
			liveJSON:  `{"name":"TASKS","max_consumers":-1,"max_msgs_per_subject":4096}`,
			wantExit:  0,
			notStderr: []string{"max_consumers", "max_msgs_per_subject"},
		},
		{
			// The gap every install that predates this render is in. It
			// is reported and named, and it is NOT applied: the edit
			// evicts, and provisioning does not truncate a running
			// install's history on an operator's behalf.
			name:       "a stream that predates the per-subject cap is told, not edited",
			liveJSON:   `{"name":"TASKS","max_consumers":-1,"max_msgs_per_subject":-1}`,
			wantExit:   0,
			wantStderr: []string{"max_msgs_per_subject=-1", "no per-subject limit", "predates the limit", "nats stream edit TASKS --max-msgs-per-subject=4096", "evicts"},
			notStderr:  []string{"--max-consumers"},
		},
		{
			// A cap that is not the render's is not the same report. An
			// operator who deliberately set 8192 has a bounded stream,
			// and telling them it "predates the limit" and that one task
			// can still evict another session's history is telling them
			// something false about their own install.
			name:       "a cap the operator chose is drift, not the unbounded gap",
			liveJSON:   `{"name":"TASKS","max_consumers":328,"max_msgs_per_subject":8192}`,
			wantExit:   0,
			wantStderr: []string{"max_msgs_per_subject=8192", "drift between"},
			notStderr:  []string{"predates the limit", "can still evict", "--max-consumers"},
		},
		{
			// The property the comment beside the grep claims: a check
			// whose extractor stops matching must fail, not skip. Once
			// per limit — they are two greps.
			//
			// Exit 1 and not 2, on both: an empty extraction is what a
			// momentarily unreachable bus looks like from here as much
			// as a changed answer shape does, and only the second of
			// those fails the same way next time. 1 keeps them on the
			// backoffLimit, which is the retryable side of the
			// convention the script's exit 2 establishes.
			name:       "an answer the consumer extractor cannot read is retryable",
			liveJSON:   `{"name":"TASKS","consumer_limit":64,"max_msgs_per_subject":4096}`,
			wantExit:   1,
			wantStderr: []string{"could not read max_consumers"},
		},
		{
			name:       "an answer the subject-cap extractor cannot read is retryable",
			liveJSON:   `{"name":"TASKS","max_consumers":328,"per_subject_limit":4096}`,
			wantExit:   1,
			wantStderr: []string{"could not read max_msgs_per_subject"},
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			dir := t.TempDir()
			script := stageProvisionScript(t, dir, a2aProvisionScript(agent))
			callLog := stubNats(t, dir, tc.liveJSON)

			cmd := exec.Command(bash, script)
			cmd.Env = append(os.Environ(),
				"PATH="+filepath.Join(dir, "bin")+string(os.PathListSeparator)+os.Getenv("PATH"),
				"BUS_USER=test-agent-a2a-provision",
			)
			var stderr strings.Builder
			cmd.Stderr = &stderr
			cmd.Stdout = &strings.Builder{}
			runErr := cmd.Run()

			gotExit := 0
			if runErr != nil {
				ee, ok := runErr.(*exec.ExitError)
				if !ok {
					t.Fatalf("running the provision script: %v\nstderr:\n%s", runErr, stderr.String())
				}
				gotExit = ee.ExitCode()
			}
			// The status, not just the fact of failing: 2 is the
			// script telling the Job's podFailurePolicy that a retry
			// reaches the same refusal, and every other non-zero
			// status leaves the run on the backoffLimit.
			if gotExit != tc.wantExit {
				t.Fatalf("exit %d, want %d\nstderr:\n%s", gotExit, tc.wantExit, stderr.String())
			}
			for _, want := range tc.wantStderr {
				if !strings.Contains(stderr.String(), want) {
					t.Errorf("stderr does not name %q; an operator cannot act on a refusal that does not say what to change\ngot:\n%s", want, stderr.String())
				}
			}
			for _, unwanted := range tc.notStderr {
				if strings.Contains(stderr.String(), unwanted) {
					t.Errorf("stderr mentions %q, which this case is not about:\n%s", unwanted, stderr.String())
				}
			}

			calls := readStubCalls(t, callLog)
			// The guards guard: TASKS exists in every case here, so
			// nothing may be created on top of it.
			if strings.Contains(calls, "stream add TASKS") {
				t.Errorf("the script created TASKS over a stream the stub reports as existing:\n%s", calls)
			}
			// And it reports rather than converges. The name of the
			// per-subject case is "told, not edited" and nothing here
			// used to check the second half: the provision principal
			// holds STREAM.CREATE and STREAM.INFO and no UPDATE, so an
			// edit would fail at the server, but a script that reached
			// for one would also be a script that had decided to
			// truncate a running install's history.
			if strings.Contains(calls, "stream edit") {
				t.Errorf("the script edited a stream; provisioning reports a gap and never converges:\n%s", calls)
			}
			// And the refusal comes LAST. A check that exited in the
			// middle of the script would leave a bus missing the
			// streams and buckets below TASKS — a partially
			// provisioned install is worse than an unprovisioned one,
			// because it looks like neither.
			for _, reached := range []string{
				"stream info DIRECTORY",
				"stream info TOPICS-STATE",
				"stream info TOPICS-JOURNAL",
				"kv info runtime-state",
				"kv info session-state",
				"kv info cap",
			} {
				if !strings.Contains(calls, reached) {
					t.Errorf("the script exited before %q; the closing check must run after the rest of the bus is provisioned\ncalls:\n%s", reached, calls)
				}
			}
		})
	}
}

// readStubCalls returns everything the stubbed nats was asked to do, in order.
func readStubCalls(t *testing.T, path string) string {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("reading the stub call log: %v", err)
	}
	if len(b) == 0 {
		t.Fatal("the stub was never invoked; this test would assert nothing")
	}
	return string(b)
}

// stageProvisionScript writes the script somewhere runnable, with the one
// change a test outside a pod has to make: the projected token path.
//
// The substitution is asserted rather than assumed. If the script stops reading
// that path, a silent no-op replace would leave the test running something that
// is no longer what ships.
func stageProvisionScript(t *testing.T, dir, script string) string {
	t.Helper()
	const tokenRead = `BUS_TOKEN="$(cat /var/run/secrets/a2a-bus/token)"`
	if strings.Count(script, tokenRead) != 1 {
		t.Fatalf("expected exactly one %q in the provision script, found %d; this test would otherwise run a script it did not finish adapting",
			tokenRead, strings.Count(script, tokenRead))
	}
	script = strings.Replace(script, tokenRead, `BUS_TOKEN="stub-token"`, 1)

	path := filepath.Join(dir, "provision.sh")
	if err := os.WriteFile(path, []byte(script), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

// stubNats puts a `nats` on PATH that reports every stream and bucket as
// already existing — the live-install shape, where the create-only guards all
// short-circuit — and answers `stream info TASKS --json` with liveJSON. Every
// invocation is appended to a log the caller reads, which is how the ordering
// assertions below know how far the script got before it exited.
func stubNats(t *testing.T, dir, liveJSON string) string {
	t.Helper()
	bin := filepath.Join(dir, "bin")
	if err := os.MkdirAll(bin, 0o700); err != nil {
		t.Fatal(err)
	}
	log := filepath.Join(dir, "nats-calls.log")
	stub := fmt.Sprintf(`#!/bin/sh
echo "$*" >> %q
for a in "$@"; do
  if [ "$a" = "--json" ]; then
    cat <<'JSON'
%s
JSON
    exit 0
  fi
done
exit 0
`, log, liveJSON)
	if err := os.WriteFile(filepath.Join(bin, "nats"), []byte(stub), 0o700); err != nil {
		t.Fatal(err)
	}
	return log
}

// The envtest seed fixture says it is "the flags the script passes to natscli
// translated to StreamConfig". This holds it to that for the two limits this
// change put on TASKS.
//
// It is not decoration. Every A2A authz test in this package runs against the
// bus a2aProvisionLikeTheScript seeds, so a fixture that has drifted from the
// render is a suite proving things about a deployment nobody ships — and the
// drift is invisible, because the fixture is valid NATS config either way.
func TestTheSeedFixtureCarriesTheLimitsTheScriptRenders(t *testing.T) {
	script := a2aProvisionScript(a2aTestAgent())
	add, ok := streamAddInvocation(script, "TASKS")
	if !ok {
		t.Fatal("the provision script no longer creates TASKS; this test reads its flags")
	}

	var tasks *jetstream.StreamConfig
	for i, cfg := range a2aSeedStreamConfigs() {
		if cfg.Name == "TASKS" {
			tasks = &a2aSeedStreamConfigs()[i]
		}
	}
	if tasks == nil {
		t.Fatal("the seed fixture no longer carries TASKS")
	}

	for _, want := range []struct {
		flag string
		have int64
	}{
		{"--max-msgs-per-subject", tasks.MaxMsgsPerSubject},
		{"--max-consumers", int64(tasks.MaxConsumers)},
	} {
		rendered := want.flag + "=" + strconv.FormatInt(want.have, 10)
		if !strings.Contains(add, rendered) {
			t.Errorf("the seed fixture sets %s but the script renders %q; the authz suite is seeded against a stream the deployment does not create",
				rendered, add)
		}
	}
}
