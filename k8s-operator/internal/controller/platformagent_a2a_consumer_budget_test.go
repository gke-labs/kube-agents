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
	"context"
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
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

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
		{"unset stays at the shipped cap", nil, 62, 64},
		{"below the floor does not tighten it", ptr.To(2), 38, 64},
		{"just under the floor still does not", ptr.To(10), 62, 64},
		{"the first value above the floor derives", ptr.To(11), 65, 65},
		{"above the floor derives", ptr.To(20), 92, 92},
		{"the CRD maximum", ptr.To(10000), 30032, 30032},
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

// The budget reads the bridge sidecar's BRIDGE_CONCURRENCY. Before it did,
// an install that declared a bridge at 6 rendered the same 64-wide TASKS as
// one at 2 and was told the same 32-consumer reserve, while its bridge ran
// three times the conversations the asks row was sized for (gke-labs#2043).
// This asserts only through the budget, the render and the script -- the
// three things an install sees. A tree without the resolver produces 62, 64
// and a 32-consumer reserve on every shape below; the rows that declare a
// count expect other numbers, which is what pins that the resolver reads
// it, and the rows that do not pin that it reads nothing else.
func TestTasksBudgetReadsTheBridgeSidecarConcurrency(t *testing.T) {
	bridge := func(env ...corev1.EnvVar) *agentv1alpha1.DeploymentSpec {
		return &agentv1alpha1.DeploymentSpec{Sidecars: []corev1.Container{{Name: "hermes-bridge", Image: "bridge:dev", Env: env}}}
	}
	lit := func(v string) corev1.EnvVar { return corev1.EnvVar{Name: "BRIDGE_CONCURRENCY", Value: v} }
	for _, tc := range []struct {
		name       string
		deployment *agentv1alpha1.DeploymentSpec
		wantBudget int
		wantRender int
		wantScript []string
	}{
		{"no sidecar is the default install", nil, 62, 64,
			[]string{"required_consumers=62", "--max-consumers=64", "plus 32 reserved", "sized for 2 bridge workers"}},
		{"a sidecar that is not a bridge changes nothing", &agentv1alpha1.DeploymentSpec{Sidecars: []corev1.Container{{Name: "fluent-bit", Image: "fb:dev"}}}, 62, 64,
			[]string{"required_consumers=62", "--max-consumers=64", "plus 32 reserved", "sized for 2 bridge workers"}},
		{"the bridge's own default written on the CR changes nothing", bridge(lit("2")), 62, 64,
			[]string{"required_consumers=62", "--max-consumers=64", "plus 32 reserved", "sized for 2 bridge workers"}},
		{"the presubmit's four clears the floor", bridge(lit("4")), 74, 74,
			[]string{"required_consumers=74", "--max-consumers=74", "plus 44 reserved", "sized for 4 bridge workers"}},
		{"the nightly's six", bridge(lit("6")), 86, 86,
			[]string{"required_consumers=86", "--max-consumers=86", "plus 56 reserved", "sized for 6 bridge workers"}},
		{"a valueFrom is the default", bridge(corev1.EnvVar{Name: "BRIDGE_CONCURRENCY", ValueFrom: &corev1.EnvVarSource{
			SecretKeyRef: &corev1.SecretKeySelector{LocalObjectReference: corev1.LocalObjectReference{Name: "eval"}, Key: "parallelism"}}}), 62, 64,
			[]string{"required_consumers=62", "--max-consumers=64", "plus 32 reserved", "sized for 2 bridge workers"}},
		// The valueFrom path pinned by a number a literal-only render cannot
		// produce: a readable 6 beside an unreadable one is 8, not 6 and not 2.
		{"a bridge that can be read beside one that cannot counts the default for it", &agentv1alpha1.DeploymentSpec{Sidecars: []corev1.Container{
			{Name: "bridge-a", Image: "bridge:dev", Env: []corev1.EnvVar{lit("6")}},
			{Name: "bridge-b", Image: "bridge:dev", Env: []corev1.EnvVar{{Name: "BRIDGE_CONCURRENCY", ValueFrom: &corev1.EnvVarSource{
				ConfigMapKeyRef: &corev1.ConfigMapKeySelector{LocalObjectReference: corev1.LocalObjectReference{Name: "eval"}, Key: "parallelism"}}}}}}}, 98, 98,
			[]string{"required_consumers=98", "--max-consumers=98", "plus 68 reserved", "sized for 8 bridge workers"}},
		// The kubelet expands $(NAME) against the sidecar's earlier entries
		// before the bridge reads it: a reference to a literal 6 runs six
		// workers, and a render that counted the reference as unreadable
		// would budget 62, create TASKS at 64 and pass the gate.
		{"a reference to an earlier literal counts what the kubelet expands it to", bridge(corev1.EnvVar{Name: "EVAL_PARALLELISM", Value: "6"}, lit("$(EVAL_PARALLELISM)")), 86, 86,
			[]string{"required_consumers=86", "--max-consumers=86", "plus 56 reserved", "sized for 6 bridge workers"}},
		{"a reference to a later entry is left as written and is the default", bridge(lit("$(EVAL_PARALLELISM)"), corev1.EnvVar{Name: "EVAL_PARALLELISM", Value: "6"}), 62, 64,
			[]string{"required_consumers=62", "--max-consumers=64", "plus 32 reserved", "sized for 2 bridge workers"}},
		{"a reference to a valueFrom is the default", bridge(corev1.EnvVar{Name: "EVAL_PARALLELISM", ValueFrom: &corev1.EnvVarSource{
			SecretKeyRef: &corev1.SecretKeySelector{LocalObjectReference: corev1.LocalObjectReference{Name: "eval"}, Key: "parallelism"}}}, lit("$(EVAL_PARALLELISM)")), 62, 64,
			[]string{"required_consumers=62", "--max-consumers=64", "plus 32 reserved", "sized for 2 bridge workers"}},
		{"a non-integer is the default, as the bridge would run it", bridge(lit("six")), 62, 64,
			[]string{"required_consumers=62", "--max-consumers=64", "plus 32 reserved", "sized for 2 bridge workers"}},
		{"zero is the default, as the bridge would run it", bridge(lit("0")), 62, 64,
			[]string{"required_consumers=62", "--max-consumers=64", "plus 32 reserved", "sized for 2 bridge workers"}},
		{"two bridges bring their own workers", &agentv1alpha1.DeploymentSpec{Sidecars: []corev1.Container{
			{Name: "bridge-a", Image: "bridge:dev", Env: []corev1.EnvVar{lit("4")}},
			{Name: "bridge-b", Image: "bridge:dev", Env: []corev1.EnvVar{lit("6")}}}}, 110, 110,
			[]string{"required_consumers=110", "--max-consumers=110", "plus 80 reserved", "sized for 10 bridge workers"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			agent := &agentv1alpha1.PlatformAgent{ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"}}
			agent.Spec.Deployment = tc.deployment
			if got := a2aTasksConsumerBudget(agent); got != tc.wantBudget {
				t.Errorf("budget = %d, want %d", got, tc.wantBudget)
			}
			if got := a2aTasksMaxConsumers(agent); got != tc.wantRender {
				t.Errorf("rendered max_consumers = %d, want %d", got, tc.wantRender)
			}
			script := a2aProvisionScript(agent)
			add, ok := streamAddInvocation(script, "TASKS")
			if !ok {
				t.Fatal("no `stream add TASKS` in the provision script")
			}
			for _, want := range tc.wantScript {
				if !strings.Contains(script, want) {
					t.Errorf("the provision script does not contain %q (stream add: %s)", want, add)
				}
			}
		})
	}

	// The concurrency-dependent terms are the only ones that moved: the four
	// fixed rows and the per-session multiplier are the same on every
	// install, so the difference between two installs is exactly the tail
	// factor times the asks plus the look-ahead per worker times the workers.
	at := func(c string) int {
		agent := &agentv1alpha1.PlatformAgent{ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"}}
		agent.Spec.Deployment = bridge(lit(c))
		return a2aTasksConsumerBudget(agent)
	}
	if d := at("6") - at("2"); d != 24 {
		t.Errorf("six workers cost %d more than two; a worker is two asks and one look-ahead with a tail of two, so the difference is 2*(2+1)*4 = 24", d)
	}
}

// The refusal an install with a declared bridge hears is computed from the
// reserve that install carries. Executed, not read: the `fits` arithmetic
// and the two messages that quote the reserve come out of the shell, and a
// script that quoted the default table while checking the derived budget
// would tell an operator to lower maxSessions to a number that still refuses.
func TestProvisionRefusalNamesTheReserveItSizedFor(t *testing.T) {
	bash, err := exec.LookPath("bash")
	if err != nil {
		t.Fatalf("bash is required to execute the provision script: %v", err)
	}
	agent := &agentv1alpha1.PlatformAgent{ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"}}
	sessions := 100
	agent.Spec.Harness = &agentv1alpha1.HarnessSpec{Tuning: &agentv1alpha1.TuningSpec{MaxSessions: &sessions}}
	agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{Sidecars: []corev1.Container{{
		Name: "hermes-bridge", Image: "bridge:dev", Env: []corev1.EnvVar{{Name: "BRIDGE_CONCURRENCY", Value: "6"}},
	}}}

	dir := t.TempDir()
	script := stageProvisionScript(t, dir, a2aProvisionScript(agent))
	callLog := stubNats(t, dir, `{"name":"TASKS","max_consumers":64,"max_msgs_per_subject":4096}`)
	cmd := exec.Command(bash, script)
	cmd.Env = append(os.Environ(),
		"PATH="+filepath.Join(dir, "bin")+string(os.PathListSeparator)+os.Getenv("PATH"),
		"BUS_USER=test-agent-a2a-provision",
	)
	var stderr strings.Builder
	cmd.Stderr = &stderr
	cmd.Stdout = &strings.Builder{}
	runErr := cmd.Run()
	ee, ok := runErr.(*exec.ExitError)
	if !ok || ee.ExitCode() != 2 {
		t.Fatalf("exit %v, want 2\nstderr:\n%s", runErr, stderr.String())
	}
	for _, want := range []string{
		// 100*3 + (16 + 2*(1+1+12+6)) = 356, not the 332 the default table gives.
		"needs 356",
		"plus 56 reserved for the standing durables, the web rail and tasks/get replays.",
		"sized for 6 bridge workers",
		// (64 - 56) / 3 = 2; the default table would have said 10, and a CR
		// lowered to 10 would be refused again at 30 + 56 = 86.
		"lower spec.harness.tuning.maxSessions to at most 2 -",
		"with 56 of them reserved",
		"recreates TASKS at 356",
		// The parenthetical names the reference shape with a literal $(NAME):
		// inside the script's double quotes that is a command substitution
		// unless escaped, and an unescaped one would print an empty string.
		"a $(NAME) reference to an earlier literal in the same entry is expanded",
		"a valueFrom or a reference to one; 2 when none sets it",
	} {
		if !strings.Contains(stderr.String(), want) {
			t.Errorf("stderr does not name %q\ngot:\n%s", want, stderr.String())
		}
	}
	for _, unwanted := range []string{"plus 32 reserved", "to at most 10", "one session still needs"} {
		if strings.Contains(stderr.String(), unwanted) {
			t.Errorf("stderr names %q, the default table's number, on a CR that declared six workers:\n%s", unwanted, stderr.String())
		}
	}
	// The third lever, where it does not reach: beside maxSessions=100 no
	// worker count fits a 64-wide stream (one worker needs 300 + 20 + 6 =
	// 326), and the refusal says so rather than offering it.
	for _, want := range []string{
		"Fewer bridge workers alone will not fit it beside spec.harness.tuning.maxSessions=100: one worker",
		"still needs 326, more than this stream holds. One session beside one worker needs 29;",
	} {
		if !strings.Contains(stderr.String(), want) {
			t.Errorf("stderr does not name %q\ngot:\n%s", want, stderr.String())
		}
	}
	if strings.Contains(stderr.String(), "Or keep spec.harness.tuning.maxSessions") {
		t.Errorf("stderr offers fewer bridge workers as a way out beside maxSessions=100, where none fits:\n%s", stderr.String())
	}
	readStubCalls(t, callLog)
}

// runProvision executes the provision script for agent against a stub bus
// whose TASKS holds liveConsumers, and returns the exit code and what the
// script wrote to stderr.
func runProvision(t *testing.T, agent *agentv1alpha1.PlatformAgent, liveConsumers int) (int, string) {
	t.Helper()
	bash, err := exec.LookPath("bash")
	if err != nil {
		t.Fatalf("bash is required to execute the provision script: %v", err)
	}
	dir := t.TempDir()
	script := stageProvisionScript(t, dir, a2aProvisionScript(agent))
	callLog := stubNats(t, dir, fmt.Sprintf(`{"name":"TASKS","max_consumers":%d,"max_msgs_per_subject":4096}`, liveConsumers))
	cmd := exec.Command(bash, script)
	cmd.Env = append(os.Environ(),
		"PATH="+filepath.Join(dir, "bin")+string(os.PathListSeparator)+os.Getenv("PATH"),
		"BUS_USER=test-agent-a2a-provision",
	)
	var stderr strings.Builder
	cmd.Stderr = &stderr
	cmd.Stdout = &strings.Builder{}
	code := 0
	if runErr := cmd.Run(); runErr != nil {
		ee, ok := runErr.(*exec.ExitError)
		if !ok {
			t.Fatalf("running the provision script: %v\nstderr:\n%s", runErr, stderr.String())
		}
		code = ee.ExitCode()
	}
	readStubCalls(t, callLog)
	return code, stderr.String()
}

// runProvisionRefusal is runProvision expecting the exit-2 refusal.
func runProvisionRefusal(t *testing.T, agent *agentv1alpha1.PlatformAgent, liveConsumers int) string {
	t.Helper()
	code, stderr := runProvision(t, agent, liveConsumers)
	if code != 2 {
		t.Fatalf("exit %d, want 2\nstderr:\n%s", code, stderr)
	}
	return stderr
}

// The run that passes says what the budget could not read. A CR whose only
// BRIDGE_CONCURRENCY is a valueFrom resolves to the default, budgets 62,
// passes a 64-wide stream, and ran its real worker count on a stream sized
// for two with nothing in any log to say so: gke-labs#2043's under-sizing,
// reached with no refusal. A key delivered through envFrom is the same
// silence from further off -- a2aBridgeConcurrencyValue walks env, so the
// sidecar looked like no bridge at all. Both print a NOTE on every run, exit
// 0 included; neither moves a number, and a literal, in env alone or beside
// an envFrom the kubelet lets it override, prints nothing. Executed, since
// the run in question is the one that exits 0.
func TestProvisionSaysWhenABridgeCountWasNotRead(t *testing.T) {
	fromRef := corev1.EnvVar{Name: "BRIDGE_CONCURRENCY", ValueFrom: &corev1.EnvVarSource{
		ConfigMapKeyRef: &corev1.ConfigMapKeySelector{LocalObjectReference: corev1.LocalObjectReference{Name: "eval"}, Key: "parallelism"},
	}}
	envFrom := []corev1.EnvFromSource{{ConfigMapRef: &corev1.ConfigMapEnvSource{LocalObjectReference: corev1.LocalObjectReference{Name: "eval"}}}}
	sidecar := func(env []corev1.EnvVar, from []corev1.EnvFromSource) *agentv1alpha1.DeploymentSpec {
		return &agentv1alpha1.DeploymentSpec{Sidecars: []corev1.Container{{Name: "hermes-bridge", Image: "bridge:dev", Env: env, EnvFrom: from}}}
	}
	const (
		readNote    = "NOTE: a spec.deployment.sidecars entry sets BRIDGE_CONCURRENCY to a value this render could not read as a count -"
		envFromNote = "NOTE: a spec.deployment.sidecars entry carries envFrom and sets no BRIDGE_CONCURRENCY in env. A BRIDGE_CONCURRENCY"
	)
	for _, tc := range []struct {
		name              string
		deployment        *agentv1alpha1.DeploymentSpec
		wantEnvFromUnread bool
		want              []string
		unwanted          []string
	}{
		{"a valueFrom alone is the default, and the run says so", sidecar([]corev1.EnvVar{fromRef}, nil), false,
			[]string{
				readNote,
				// The reference shape with a literal $(NAME), which the
				// script's double quotes would otherwise substitute.
				"a valueFrom, a $(NAME) reference to one or to a name no earlier literal in the same entry set, or a value",
				"that is not a count - and it counted as the bridge's default of 2.",
				"so the 2 bridge workers this budget is sized for may be fewer than it runs, and TASKS may be",
				"or a $(NAME) reference to an earlier literal in the same entry.",
			},
			[]string{envFromNote}},
		{"an envFrom with no entry in env may carry the key, and the run says so", sidecar(nil, envFrom), true,
			[]string{
				envFromNote,
				"delivered through envFrom is not read: this render cannot see the keys a ConfigMap or Secret carries, so",
				"the entry counted as no bridge and added nothing to the 2 bridge workers this budget is sized for.",
				"read it, set it in env as a literal, which the kubelet lets override envFrom.",
			},
			[]string{readNote}},
		{"a literal in env beside envFrom is the count, and the run says nothing", sidecar([]corev1.EnvVar{{Name: "BRIDGE_CONCURRENCY", Value: "2"}}, envFrom), false,
			nil, []string{"NOTE: a spec.deployment.sidecars"}},
		{"a literal alone says nothing", sidecar([]corev1.EnvVar{{Name: "BRIDGE_CONCURRENCY", Value: "2"}}, nil), false,
			nil, []string{"NOTE: a spec.deployment.sidecars"}},
		{"no sidecar says nothing", nil, false,
			nil, []string{"NOTE: a spec.deployment.sidecars"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			agent := &agentv1alpha1.PlatformAgent{ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"}}
			agent.Spec.Deployment = tc.deployment
			if got := a2aBridgeEnvFromUnread(agent); got != tc.wantEnvFromUnread {
				t.Errorf("a2aBridgeEnvFromUnread = %v, want %v", got, tc.wantEnvFromUnread)
			}
			// The numbers do not move: the default install's budget, which
			// a 64-wide stream holds, so the run below exits 0.
			if got := a2aTasksConsumerBudget(agent); got != 62 {
				t.Fatalf("budget = %d, want 62", got)
			}
			code, stderr := runProvision(t, agent, 64)
			if code != 0 {
				t.Fatalf("exit %d, want 0\nstderr:\n%s", code, stderr)
			}
			if strings.Contains(stderr, "TASKS holds max_consumers") {
				t.Errorf("a 64-wide stream refused a budget of 62:\n%s", stderr)
			}
			for _, want := range tc.want {
				if !strings.Contains(stderr, want) {
					t.Errorf("stderr does not say %q\ngot:\n%s", want, stderr)
				}
			}
			for _, unwanted := range tc.unwanted {
				if strings.Contains(stderr, unwanted) {
					t.Errorf("stderr says %q\ngot:\n%s", unwanted, stderr)
				}
			}
		})
	}
}

// The refusal the install in gke-labs#2043 hears: a bus provisioned at 64
// with the default maxSessions (budget 62), then a bridge sidecar declared at
// 4, whose reserve of 44 takes the budget to 74. maxSessions=10 fit before
// the sidecar, so a refusal that named maxSessions alone and offered only to
// lower it or delete the stream sent the operator to the wrong input. It
// names both inputs, and the third lever with the number that fits: beside
// maxSessions=10 a 64-wide stream has room for (64 - 30 - 20) / 6 = 2
// workers. Executed, since the number comes out of the shell.
func TestProvisionRefusalOffersFewerBridgeWorkers(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"}}
	agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{Sidecars: []corev1.Container{{
		Name: "hermes-bridge", Image: "bridge:dev", Env: []corev1.EnvVar{{Name: "BRIDGE_CONCURRENCY", Value: "4"}},
	}}}
	stderr := runProvisionRefusal(t, agent, 64)
	for _, want := range []string{
		"TASKS holds max_consumers=64 but this PlatformAgent needs 74:",
		"spec.harness.tuning.maxSessions is 10, each session creates 3 consumers on TASKS,",
		"plus 44 reserved for the standing durables, the web rail and tasks/get replays.",
		"sized for 4 bridge workers: each spec.deployment.sidecars entry",
		// The two remedies that were there, computed from this CR's reserve.
		"lower spec.harness.tuning.maxSessions to at most 6 -",
		"or delete the TASKS stream and provision again.",
		// The third, with the worker count that fits beside the CR's maxSessions.
		"Or keep spec.harness.tuning.maxSessions at 10 and declare the bridge sidecar with at most 2",
		"workers - BRIDGE_CONCURRENCY on its spec.deployment.sidecars entry; unset, the bridge runs 2 - which is",
		"the reserve is 20 plus 6 a worker.",
		// And that it finishes the way a maxSessions edit does.
		"Lowering spec.harness.tuning.maxSessions, or the",
		"bridge's worker count, finishes on its own",
	} {
		if !strings.Contains(stderr, want) {
			t.Errorf("stderr does not name %q\ngot:\n%s", want, stderr)
		}
	}
	if strings.Contains(stderr, "The two ways out do not finish the same way") {
		t.Errorf("stderr counts two ways out on a CR with three:\n%s", stderr)
	}

	// At the default there is no worker count to lower, and the refusal reads
	// as it did: two ways out, no third lever, no worker arithmetic.
	agent = &agentv1alpha1.PlatformAgent{ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"}}
	sessions := 100
	agent.Spec.Harness = &agentv1alpha1.HarnessSpec{Tuning: &agentv1alpha1.TuningSpec{MaxSessions: &sessions}}
	stderr = runProvisionRefusal(t, agent, 64)
	for _, want := range []string{
		"sized for 2 bridge workers: each spec.deployment.sidecars entry",
		"lower spec.harness.tuning.maxSessions to at most 10 -",
		"The two ways out do not finish the same way. Lowering spec.harness.tuning.maxSessions",
	} {
		if !strings.Contains(stderr, want) {
			t.Errorf("stderr does not name %q\ngot:\n%s", want, stderr)
		}
	}
	for _, unwanted := range []string{"Or keep spec.harness.tuning.maxSessions", "Fewer bridge workers", "workers_fit", "the most this render sizes for"} {
		if strings.Contains(stderr, unwanted) {
			t.Errorf("stderr names %q on a CR at the bridge's default, which has no worker count to lower:\n%s", unwanted, stderr)
		}
	}

	// A CR whose maxSessions cannot fit but whose worker count can: 100
	// sessions and 6 workers against a stream of 40 leaves fits below one,
	// and the worker arithmetic (40 - 300 - 20) / 6 below one too, so the
	// refusal says what one session beside one worker needs and that
	// deleting is what is left below it -- not "That leaves deleting", which
	// would skip the second input.
	agent = &agentv1alpha1.PlatformAgent{ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"}}
	agent.Spec.Harness = &agentv1alpha1.HarnessSpec{Tuning: &agentv1alpha1.TuningSpec{MaxSessions: &sessions}}
	agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{Sidecars: []corev1.Container{{
		Name: "hermes-bridge", Image: "bridge:dev", Env: []corev1.EnvVar{{Name: "BRIDGE_CONCURRENCY", Value: "6"}},
	}}}
	stderr = runProvisionRefusal(t, agent, 40)
	for _, want := range []string{
		"one session still needs 59 at 6 bridge workers, more than this stream holds.",
		"One session beside one worker needs 29;",
		"below that, only deleting the TASKS stream and provisioning again fits.",
	} {
		if !strings.Contains(stderr, want) {
			t.Errorf("stderr does not name %q\ngot:\n%s", want, stderr)
		}
	}
	if strings.Contains(stderr, "That leaves deleting the TASKS stream") {
		t.Errorf("stderr closes on the delete before naming the worker count, on a CR that declared six:\n%s", stderr)
	}
}

// The bridge count the budget reads is bounded before it reaches the
// arithmetic. The bridge does not bound it (envInt takes any int), so a
// literal of 1000000000 rendered --max-consumers=6000000050 and MaxInt64
// wrapped the replay term negative, taking the reserve below the default
// install's 32 and passing a stream that is short. The cap is the
// bridge's queue capacity, and a literal above it is the cap, not the
// default: the default would size a real large install short. The render
// says when it capped, so the refusal does not read as if the CR said 1024.
func TestBridgeConcurrencyIsCappedBeforeTheArithmetic(t *testing.T) {
	bridge := func(literals ...string) *agentv1alpha1.DeploymentSpec {
		d := &agentv1alpha1.DeploymentSpec{}
		for i, v := range literals {
			d.Sidecars = append(d.Sidecars, corev1.Container{Name: fmt.Sprintf("bridge-%d", i), Image: "bridge:dev",
				Env: []corev1.EnvVar{{Name: "BRIDGE_CONCURRENCY", Value: v}}})
		}
		return d
	}
	// 10*3 + 20 + 6*1024 = 6194 at the cap.
	const atCap = 6194
	for _, tc := range []struct {
		name       string
		deployment *agentv1alpha1.DeploymentSpec
		wantCapped bool
	}{
		{"at the cap exactly, which is not capping", bridge("1024"), false},
		{"a literal above the cap", bridge("1000000000"), true},
		{"MaxInt64", bridge("9223372036854775807"), true},
		{"two sidecars whose literals sum past the cap", bridge("1000", "1000"), true},
		{"two sidecars each at MaxInt64, whose raw sum would wrap", bridge("9223372036854775807", "9223372036854775807"), true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			agent := &agentv1alpha1.PlatformAgent{ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"}}
			agent.Spec.Deployment = tc.deployment
			if got := a2aBridgeConcurrency(agent); got != a2aBridgeConcurrencyMax {
				t.Errorf("a2aBridgeConcurrency = %d, want the cap %d", got, a2aBridgeConcurrencyMax)
			}
			if got := a2aTasksReserve(agent); got < a2aTasksReservedConsumers {
				t.Errorf("a2aTasksReserve = %d, below the default install's %d: the replay term wrapped", got, a2aTasksReservedConsumers)
			}
			if got := a2aTasksConsumerBudget(agent); got != atCap {
				t.Errorf("budget = %d, want %d", got, atCap)
			}
			if got := a2aTasksMaxConsumers(agent); got != atCap {
				t.Errorf("rendered max_consumers = %d, want %d", got, atCap)
			}
			script := a2aProvisionScript(agent)
			if !strings.Contains(script, "--max-consumers="+strconv.Itoa(atCap)) {
				add, _ := streamAddInvocation(script, "TASKS")
				t.Errorf("the provision script does not render --max-consumers=%d (stream add: %s)", atCap, add)
			}
			if !strings.Contains(script, "required_consumers="+strconv.Itoa(atCap)) {
				t.Errorf("the provision script does not gate on required_consumers=%d", atCap)
			}
			capped := "sized for 1024 bridge workers, the most this render sizes for: this CR"
			plain := "sized for 1024 bridge workers: each spec.deployment.sidecars entry"
			if tc.wantCapped && !strings.Contains(script, capped) {
				t.Errorf("the refusal does not say the worker count was capped:\n%s", refusalLines(script))
			}
			if !tc.wantCapped && !strings.Contains(script, plain) {
				t.Errorf("the refusal reads a literal at the cap as capped:\n%s", refusalLines(script))
			}
			if tc.wantCapped != strings.Contains(script, "a count past it is a typo, not a sizing - correct the literal") {
				t.Errorf("capped = %v but the refusal's correct-the-literal line is %v:\n%s", tc.wantCapped, !tc.wantCapped, refusalLines(script))
			}
			// The status message's half of the same fact.
			status := a2aProvisionRefusalStatus(agent)
			if tc.wantCapped != strings.Contains(status, "The CR declares more than 1024; the budget sizes for at most that many") {
				t.Errorf("capped = %v but the status message says the count was capped = %v: %q", tc.wantCapped, !tc.wantCapped, status)
			}
		})
	}
}

// refusalLines is the provision script's refusal block, for a failure message
// that would otherwise print the whole script.
func refusalLines(script string) string {
	var out []string
	for _, l := range strings.Split(script, "\n") {
		if strings.Contains(l, "bridge workers") || strings.Contains(l, "typo") {
			out = append(out, l)
		}
	}
	return strings.Join(out, "\n")
}

// The status message the reconcile writes for a Job the podFailurePolicy
// failed is the other surface that named maxSessions alone. On the install
// gke-labs#2043 describes it read "maxSessions=10 needs (74)" though
// maxSessions=10 fit before the sidecar was declared. It now attributes the
// need to both inputs and offers the third lever when the CR declares more
// workers than the bridge's default, and reads as it did when it does not.
// Both through the helper, which is the format string, and once through the
// reconcile, which is what calls it.
func TestProvisionFailureStatusNamesBothInputs(t *testing.T) {
	bridge := func(v string) *agentv1alpha1.DeploymentSpec {
		return &agentv1alpha1.DeploymentSpec{Sidecars: []corev1.Container{{Name: "hermes-bridge", Image: "bridge:dev",
			Env: []corev1.EnvVar{{Name: "BRIDGE_CONCURRENCY", Value: v}}}}}
	}
	sidecar := func(name string, env ...corev1.EnvVar) corev1.Container {
		return corev1.Container{Name: name, Image: name + ":dev", Env: env}
	}
	lit := func(v string) corev1.EnvVar { return corev1.EnvVar{Name: "BRIDGE_CONCURRENCY", Value: v} }
	fromRef := corev1.EnvVar{Name: "BRIDGE_CONCURRENCY", ValueFrom: &corev1.EnvVarSource{
		ConfigMapKeyRef: &corev1.ConfigMapKeySelector{LocalObjectReference: corev1.LocalObjectReference{Name: "eval"}, Key: "parallelism"},
	}}
	// The clause the message adds only where an entry took the default in
	// place of a count the render could not read; the script's parenthetical
	// states the same rule on every render.
	const readClause = "the render reads from spec.deployment.sidecars (BRIDGE_CONCURRENCY; an entry it cannot read as a count, a valueFrom or a reference to one among them, counts as the bridge's default of 2)"
	for _, tc := range []struct {
		name       string
		deployment *agentv1alpha1.DeploymentSpec
		sessions   int // 0 is the default maxSessions
		want       []string
		unwanted   []string
	}{
		{"the default install names maxSessions and two ways out", nil, 0,
			[]string{
				"spec.harness.tuning.maxSessions=10 needs (62).",
				"so the two ways out are to lower maxSessions until the budget fits the stream, or to delete the TASKS stream and let provisioning recreate it at 64,",
				"and therefore the maxSessions that fits. The two do not finish the same way. Lowering maxSessions finishes by itself:",
			},
			[]string{"bridge workers", "fewer workers", "BRIDGE_CONCURRENCY", "The three"}},
		{"the bridge's own default written on the CR reads the same", bridge("2"), 0,
			[]string{"spec.harness.tuning.maxSessions=10 needs (62).", "so the two ways out are"},
			[]string{"bridge workers", "fewer workers", "BRIDGE_CONCURRENCY"}},
		{"the presubmit's four names both inputs and three ways out", bridge("4"), 0,
			[]string{
				"spec.harness.tuning.maxSessions=10 and the 4 bridge workers the CR declares (BRIDGE_CONCURRENCY on spec.deployment.sidecars) need together (74; the reserve is 44 at 4 workers and 32 at the bridge's default of 2).",
				"so the ways out are to lower maxSessions until the budget fits the stream, to declare the bridge sidecar with fewer workers (a lower BRIDGE_CONCURRENCY, or none for the bridge's default of 2) until it does, or to delete the TASKS stream and let provisioning recreate it at 74,",
				"and therefore the maxSessions, or the worker count, that fits. The three do not finish the same way. Lowering maxSessions or the bridge's worker count finishes by itself: either CR edit re-renders this Job,",
				// The rest of the remedy, unchanged.
				"Delete the Job to re-run it now",
				"kubectl rollout restart deployment/test-agent-a2a-gateway -n test-ns, and the agent workload test-agent-gateway with it",
			},
			[]string{"the two ways out", "The two do not finish", "needs (74)", "more than 1024"}},
		{"a capped count says so", bridge("9223372036854775807"), 0,
			[]string{
				"the 1024 bridge workers the CR declares",
				"need together (6194;",
				"The CR declares more than 1024; the budget sizes for at most that many, the queue behind the bridge's workers, and a count past it is a typo to correct.",
				"recreate it at 6194,",
			},
			[]string{"the two ways out", readClause, "That count is"}},
		// The count is attributed to the CR only when the CR declares it. A
		// literal beside a valueFrom is 6 + the default; the CR declares 6
		// and a reference, so the message says what the render read and
		// states the per-entry rule.
		{"a literal beside a valueFrom is a read, not a declaration, and says the rule", &agentv1alpha1.DeploymentSpec{Sidecars: []corev1.Container{
			sidecar("bridge-a", lit("6")), sidecar("bridge-b", fromRef)}}, 0,
			[]string{
				"spec.harness.tuning.maxSessions=10 and the 8 bridge workers " + readClause + " need together (98; the reserve is 68 at 8 workers and 32 at the bridge's default of 2).",
				"so the ways out are to lower maxSessions until the budget fits the stream, to declare the bridge sidecar with fewer workers",
				"recreate it at 98,",
			},
			[]string{"the CR declares", "the two ways out", "more than 1024"}},
		{"two literals are what the CR declares, with no rule to state", &agentv1alpha1.DeploymentSpec{Sidecars: []corev1.Container{
			sidecar("bridge-a", lit("4")), sidecar("bridge-b", lit("6"))}}, 0,
			[]string{
				"spec.harness.tuning.maxSessions=10 and the 10 bridge workers the CR declares (BRIDGE_CONCURRENCY on spec.deployment.sidecars) need together (110; the reserve is 80 at 10 workers and 32 at the bridge's default of 2).",
				"recreate it at 110,",
			},
			[]string{readClause, "the render reads", "cannot read", "That count is"}},
		// A lone reference to a valueFrom is the default, and the message
		// still says the count is what the render read and states the rule:
		// the sidecar may run more workers than the budget was sized for,
		// which is what the clause is for. The reserve is stated once, as
		// the default's, and the ways out stay two, there being no lower
		// count to offer at the default.
		{"a lone reference to a valueFrom is the default, and the message still states the read rule", &agentv1alpha1.DeploymentSpec{Sidecars: []corev1.Container{
			sidecar("hermes-bridge", corev1.EnvVar{Name: "EVAL_PARALLELISM", ValueFrom: fromRef.ValueFrom}, lit("$(EVAL_PARALLELISM)"))}}, 0,
			[]string{
				"spec.harness.tuning.maxSessions=10 and the 2 bridge workers " + readClause + " need together (62; the reserve is 32 at 2 workers, the bridge's default).",
				"so the two ways out are to lower maxSessions until the budget fits the stream, or to delete the TASKS stream and let provisioning recreate it at 64,",
				"and therefore the maxSessions that fits. The two do not finish the same way.",
			},
			[]string{"needs (62)", "the CR declares", "fewer workers", "The three", "and 32 at the bridge's default of 2", "That count is"}},
		// Capped over a read: the sentence that names the cap follows the
		// attribution, since 1023 and a reference is not a CR declaring
		// more than 1024.
		{"a capped count over a read says the count, not the CR, is past the cap", &agentv1alpha1.DeploymentSpec{Sidecars: []corev1.Container{
			sidecar("bridge-a", lit("1023")), sidecar("bridge-b", fromRef)}}, 0,
			[]string{
				"the 1024 bridge workers " + readClause + " need together (6194;",
				"That count is more than 1024; the budget sizes for at most that many, the queue behind the bridge's workers, and a count past it is a typo to correct.",
				"recreate it at 6194,",
			},
			[]string{"the CR declares"}},
		// One worker moved the reserve to 26, so the count is an input
		// the message names -- "maxSessions=20 needs (86)" would quote a
		// number maxSessions alone does not produce -- but there is no
		// lower count to declare, so the lever is not offered and the
		// ways out stay two.
		{"one worker is named as an input, with no lower count to offer", bridge("1"), 20,
			[]string{
				"spec.harness.tuning.maxSessions=20 and the 1 bridge worker the CR declares (BRIDGE_CONCURRENCY on spec.deployment.sidecars) need together (86; the reserve is 26 at 1 worker and 32 at the bridge's default of 2, with no lower count left to declare).",
				"so the two ways out are to lower maxSessions until the budget fits the stream, or to delete the TASKS stream and let provisioning recreate it at 86,",
				"and therefore the maxSessions that fits. The two do not finish the same way.",
			},
			[]string{"needs (86)", "fewer workers", "The three", "the worker count, that fits", "1 bridge workers", "at 1 workers"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			agent := &agentv1alpha1.PlatformAgent{ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"}}
			agent.Spec.Deployment = tc.deployment
			if tc.sessions != 0 {
				sessions := tc.sessions
				agent.Spec.Harness = &agentv1alpha1.HarnessSpec{Tuning: &agentv1alpha1.TuningSpec{MaxSessions: &sessions}}
			}
			msg := a2aProvisionRefusalStatus(agent)
			for _, want := range tc.want {
				if !strings.Contains(msg, want) {
					t.Errorf("status message does not say %q:\n%s", want, msg)
				}
			}
			for _, unwanted := range tc.unwanted {
				if strings.Contains(msg, unwanted) {
					t.Errorf("status message says %q:\n%s", unwanted, msg)
				}
			}
			// The width the recreate names clears the CR's own gate, in
			// every shape; a wrapped count would put it below the budget.
			if width := remedyRecreateWidth(t, msg); width < a2aTasksConsumerBudget(agent) || width < a2aTasksMaxConsumersFloor {
				t.Errorf("remedy recreates TASKS at %d, below the budget %d or the floor %d", width, a2aTasksConsumerBudget(agent), a2aTasksMaxConsumersFloor)
			}
		})
	}

	// Through the reconcile, on the sidecar install: the helper is what the
	// PodFailurePolicy branch appends, and only that branch.
	agent := a2aTestAgent()
	agent.Spec.Deployment = bridge("4")
	job := buildA2AProvisionJob(agent)
	withCommonLabels(job, agent)
	job.Status.Conditions = []batchv1.JobCondition{{
		Type: batchv1.JobFailed, Status: corev1.ConditionTrue,
		Reason:  batchv1.JobReasonPodFailurePolicy,
		Message: "Container provision for pod test/x failed with exit code 2 matching FailJob rule at index 0",
	}}
	scheme := setupScheme()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, job).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	state, err := r.reconcileA2A(context.Background(), agent)
	if err != nil {
		t.Fatalf("reconcileA2A: %v", err)
	}
	if !state.failed {
		t.Fatalf("failed = false on a PodFailurePolicy Job")
	}
	for _, want := range []string{"the 4 bridge workers the CR declares", "declare the bridge sidecar with fewer workers", "recreate it at 74,"} {
		if !strings.Contains(state.message, want) {
			t.Errorf("the reconcile's status message does not say %q:\n%s", want, state.message)
		}
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
		"--max-consumers=152",
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
				"max_consumers=64", "needs 332",
				"stream configuration update can not change MaxConsumers",
				"lower spec.harness.tuning.maxSessions to at most 10",
				"delete the TASKS stream",
				"recreates TASKS at 332",
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
			// still needs 35 consumers, so no value of the field
			// fits a stream this narrow - offering "lower
			// maxSessions" here would be a remedy the API server
			// refuses. The script says why, and leaves the
			// recreate standing on its own.
			name:     "a stream too narrow for one session offers only the recreate",
			liveJSON: `{"name":"TASKS","max_consumers":8,"max_msgs_per_subject":4096}`,
			wantExit: 2,
			wantStderr: []string{
				"max_consumers=8", "needs 332",
				"minimum is 1, and one session still needs 35",
				"That leaves deleting the TASKS stream",
				"recreates TASKS at 332",
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
			// only place they can be told apart: 35 is the
			// narrowest stream that has room for a legal
			// maxSessions at all - the 32 reserved plus one
			// session's 3 - so it takes the "lower it" branch with
			// nothing to spare, and 34 takes the other one. A gate
			// off by one in either direction sends one of these
			// two cases down the wrong branch, and only a pair
			// sitting on the seam catches that.
			name:     "the narrowest stream that a legal maxSessions still fits",
			liveJSON: `{"name":"TASKS","max_consumers":35,"max_msgs_per_subject":4096}`,
			wantExit: 2,
			wantStderr: []string{
				"max_consumers=35",
				"lower spec.harness.tuning.maxSessions to at most 1 -",
				"delete the TASKS stream",
				"kubectl rollout restart deployment/test-agent-a2a-gateway -n test-ns",
			},
			// Both branches are on offer here, so both halves of
			// the split have to be: the recreate's restart above,
			// and no claim that the lowering half needs a Job
			// deleted to take effect.
			notStderr: []string{"one session still needs 35", "--max-consumers=", "Neither way out clears this on its own"},
		},
		{
			name:     "one consumer short of that, and lowering stops being a way out",
			liveJSON: `{"name":"TASKS","max_consumers":34,"max_msgs_per_subject":4096}`,
			wantExit: 2,
			wantStderr: []string{
				"max_consumers=34",
				"minimum is 1, and one session still needs 35",
				"kubectl rollout restart deployment/test-agent-a2a-gateway -n test-ns",
			},
			notStderr: []string{"So either lower", "to at most", "--max-consumers=", "finishes on its own"},
		},
		{
			name:      "a stream sized for it passes",
			liveJSON:  `{"name":"TASKS","max_consumers":332,"max_msgs_per_subject":4096}`,
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
			liveJSON:   `{"name":"TASKS","max_consumers":332,"max_msgs_per_subject":8192}`,
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
			liveJSON:   `{"name":"TASKS","max_consumers":332,"per_subject_limit":4096}`,
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
