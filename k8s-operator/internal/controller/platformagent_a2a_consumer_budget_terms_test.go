package controller

// The reserve's itemization, held to what it itemizes.
//
// a2aTasksReservedConsumers is a literal with a table above it, and a table
// beside a number is the kind of comment that goes wrong quietly: a term
// changes, the total is re-derived by hand, the row is not. These tests read
// the table out of the source and hold every row to the constant it names,
// hold the literal to the sum of its terms, and hold the one term copied out
// of the a2a module to the original.

import (
	"encoding/json"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	a2aManifestsSource  = "platformagent_a2a_manifests.go"
	a2aBridgeMainSource = "../../../a2a/cmd/hermes-bridge/main.go"
	a2aBridgeSource     = "../../../a2a/hermes-bridge/bridge.go"
	ciDeploySource      = "../../../hack/ci-deploy.sh"
)

// reserveTableRow matches one row of the two tables above the reserve whose
// second cell opens with a constant's name: `|    2 | a2aTasksStandingDurables:`.
// Rows whose second cell is prose ("in flight", the tail factor's "x 2") are
// not rows of the sum and do not match.
var reserveTableRow = regexp.MustCompile(`^\s*//\s*\|\s*(\d+)\s*\|\s*(a2a[A-Za-z]+)\b`)

// reserveTerms is every constant the tables name, with its value. A constant
// the table names that is missing here fails the test, so adding a row means
// adding it here too.
var reserveTerms = map[string]int{
	"a2aTasksStandingDurables":      a2aTasksStandingDurables,
	"a2aTasksAuditDurableHeadroom":  a2aTasksAuditDurableHeadroom,
	"a2aTasksIncarnationOverlap":    a2aTasksIncarnationOverlap,
	"a2aTasksWebReaders":            a2aTasksWebReaders,
	"a2aTasksReplayConsumers":       a2aTasksReplayConsumers,
	"a2aTasksReservedConsumers":     a2aTasksReservedConsumers,
	"a2aTasksReplayBridgeDispatch":  a2aTasksReplayBridgeDispatch,
	"a2aTasksReplayGatewaySweep":    a2aTasksReplayGatewaySweep,
	"a2aTasksReplayAsks":            a2aTasksReplayAsks,
	"a2aTasksReplayBridgeLookAhead": a2aTasksReplayBridgeLookAhead,
}

// The literal equals the sum of its named terms. The total is a literal and
// the leaves are literals, so changing one without the other fails here
// rather than leaving a stale comment. The replay term and the asks row are
// products in the source, not literals, so equating them to their own
// factors would prove nothing; the numbers the comment's tables state for
// them are pinned by TestReserveTableRowsMatchTheConstants, and the
// in-flight subtotal, which is prose in the table, is pinned here.
func TestReservedConsumersIsTheSumOfItsTerms(t *testing.T) {
	sum := a2aTasksStandingDurables + a2aTasksAuditDurableHeadroom + a2aTasksIncarnationOverlap +
		a2aTasksWebReaders + a2aTasksReplayConsumers
	if sum != a2aTasksReservedConsumers {
		t.Errorf("a2aTasksReservedConsumers = %d but its terms sum to %d (standing %d + audit %d + overlap %d + web %d + replay %d); the table above the constant is no longer the number",
			a2aTasksReservedConsumers, sum, a2aTasksStandingDurables, a2aTasksAuditDurableHeadroom,
			a2aTasksIncarnationOverlap, a2aTasksWebReaders, a2aTasksReplayConsumers)
	}
	inFlight := a2aTasksReplayBridgeDispatch + a2aTasksReplayGatewaySweep + a2aTasksReplayAsks +
		a2aTasksReplayBridgeLookAhead
	if inFlight != 8 {
		t.Errorf("replays in flight = %d, the table above a2aTasksReservedConsumers says 8; re-derive the row that moved and the table with it", inFlight)
	}
	// The sum is written twice: once as the constants above, which are the
	// default-install table, and once as a function of the bridge's worker
	// count, which is what the budget reads for a CR. A row added to one and
	// not the other -- #2010's look-ahead row was the one that arrived -- fails
	// here rather than sizing the default install and a declared install
	// from different tables.
	if got := a2aTasksReplayConsumersFor(a2aBridgeDefaultConcurrency); got != a2aTasksReplayConsumers {
		t.Errorf("a2aTasksReplayConsumersFor(%d) = %d, the constant a2aTasksReplayConsumers is %d; the two spellings of the replay sum have a row the other lacks", a2aBridgeDefaultConcurrency, got, a2aTasksReplayConsumers)
	}
	if got := a2aTasksReservedConsumersFor(a2aBridgeDefaultConcurrency); got != a2aTasksReservedConsumers {
		t.Errorf("a2aTasksReservedConsumersFor(%d) = %d, the literal a2aTasksReservedConsumers is %d; the table evaluated at the default is no longer the number", a2aBridgeDefaultConcurrency, got, a2aTasksReservedConsumers)
	}
	for name, v := range reserveTerms {
		if v <= 0 {
			t.Errorf("%s = %d; every term of the reserve is a positive count of consumer slots", name, v)
		}
	}
}

// Every row of the two tables in the source names a constant and states its
// value; the value in the row is the value of the constant.
func TestReserveTableRowsMatchTheConstants(t *testing.T) {
	src, err := os.ReadFile(a2aManifestsSource)
	if err != nil {
		t.Fatalf("reading %s: %v", a2aManifestsSource, err)
	}
	rows := 0
	seen := map[string]bool{}
	for i, line := range strings.Split(string(src), "\n") {
		m := reserveTableRow.FindStringSubmatch(line)
		if m == nil {
			continue
		}
		rows++
		stated, _ := strconv.Atoi(m[1])
		name := m[2]
		actual, known := reserveTerms[name]
		if !known {
			t.Errorf("%s:%d: the table names %s, which this test does not know; add it to reserveTerms so its row is checked", a2aManifestsSource, i+1, name)
			continue
		}
		seen[name] = true
		if stated != actual {
			t.Errorf("%s:%d: the table says %s is %d, the constant is %d", a2aManifestsSource, i+1, name, stated, actual)
		}
	}
	// The guard the SessionConsumerRoles test states: an extractor that
	// stops matching must fail, not pass by finding nothing.
	if rows == 0 {
		t.Fatalf("no table rows of the form `| <n> | a2a...` found in %s; the reserve's itemization is no longer where this test reads it", a2aManifestsSource)
	}
	for _, name := range []string{"a2aTasksStandingDurables", "a2aTasksAuditDurableHeadroom", "a2aTasksIncarnationOverlap", "a2aTasksWebReaders", "a2aTasksReplayConsumers", "a2aTasksReservedConsumers"} {
		if !seen[name] {
			t.Errorf("the table above a2aTasksReservedConsumers has no row for %s; every term of the sum is a row", name)
		}
	}
}

// a2aBridgeDefaultConcurrency is a copy of a number that lives in the a2a
// module, which this module cannot import. This reads the original: the
// defaultConcurrency constant in the bridge's main package, which is what the
// bridge runs when BRIDGE_CONCURRENCY is unset. Every step fails the test
// rather than falling back to a default, for the reason the
// SessionConsumerRoles test gives.
func TestBridgeConcurrencyMatchesTheA2AModule(t *testing.T) {
	fset := token.NewFileSet()
	f, err := parser.ParseFile(fset, a2aBridgeMainSource, nil, 0)
	if err != nil {
		t.Fatalf("parse %s: %v (if the bridge's main moved, this test's path must move with it; it is what keeps a2aBridgeDefaultConcurrency honest)", a2aBridgeMainSource, err)
	}
	var lit *ast.BasicLit
	ast.Inspect(f, func(n ast.Node) bool {
		spec, ok := n.(*ast.ValueSpec)
		if !ok {
			return true
		}
		for i, name := range spec.Names {
			if name.Name != "defaultConcurrency" || i >= len(spec.Values) {
				continue
			}
			if l, ok := spec.Values[i].(*ast.BasicLit); ok && l.Kind == token.INT {
				lit = l
			}
		}
		return true
	})
	if lit == nil {
		t.Fatalf("no `defaultConcurrency = <int>` in %s; it is what a2aBridgeDefaultConcurrency mirrors", a2aBridgeMainSource)
	}
	got, err := strconv.Atoi(lit.Value)
	if err != nil {
		t.Fatalf("defaultConcurrency in %s is %q, not an integer", a2aBridgeMainSource, lit.Value)
	}
	if got != a2aBridgeDefaultConcurrency {
		t.Errorf("the bridge's defaultConcurrency is %d, a2aBridgeDefaultConcurrency says %d: the replay term counts running conversations by the wrong number", got, a2aBridgeDefaultConcurrency)
	}
}

// a2aBridgeConcurrencyMax is the bridge's taskQueueCapacity, the queue behind
// its workers and the ceiling hack/ci-deploy.sh puts on the concurrency it
// writes; the bridge itself caps nothing (envInt takes any int, Config.defaults
// rewrites only a count below one), so this is the one number of the bridge's
// own that bounds its worker count. Read from the source, for the reason the
// SessionConsumerRoles test gives, and every step fails rather than defaults.
func TestBridgeConcurrencyMaxMatchesTheBridgeQueue(t *testing.T) {
	fset := token.NewFileSet()
	f, err := parser.ParseFile(fset, a2aBridgeSource, nil, 0)
	if err != nil {
		t.Fatalf("parse %s: %v (if the bridge package moved, this test's path must move with it; it is what keeps a2aBridgeConcurrencyMax honest)", a2aBridgeSource, err)
	}
	var lit *ast.BasicLit
	ast.Inspect(f, func(n ast.Node) bool {
		spec, ok := n.(*ast.ValueSpec)
		if !ok {
			return true
		}
		for i, name := range spec.Names {
			if name.Name != "taskQueueCapacity" || i >= len(spec.Values) {
				continue
			}
			if l, ok := spec.Values[i].(*ast.BasicLit); ok && l.Kind == token.INT {
				lit = l
			}
		}
		return true
	})
	if lit == nil {
		t.Fatalf("no `taskQueueCapacity = <int>` in %s; it is what a2aBridgeConcurrencyMax mirrors", a2aBridgeSource)
	}
	got, err := strconv.Atoi(lit.Value)
	if err != nil {
		t.Fatalf("taskQueueCapacity in %s is %q, not an integer", a2aBridgeSource, lit.Value)
	}
	if got != a2aBridgeConcurrencyMax {
		t.Errorf("the bridge's taskQueueCapacity is %d, a2aBridgeConcurrencyMax says %d: the budget caps the worker count at a number that is no longer the bridge's queue", got, a2aBridgeConcurrencyMax)
	}
	// The reason the cap exists, held as arithmetic: at both API ceilings the
	// budget is a small number, and the reserve at the cap is above the
	// default's, so nothing downstream of a2aBridgeConcurrency can wrap.
	if atCap := a2aTasksReservedConsumersFor(a2aBridgeConcurrencyMax); atCap <= a2aTasksReservedConsumers || atCap > 10000 {
		t.Errorf("reserve at the cap = %d; it should sit above the default's %d and well under any int", atCap, a2aTasksReservedConsumers)
	}
}

// The provision script's refusal is the reserve's other reader: it quotes the
// number, computes the maxSessions that still fits from it, and names what
// the reserve is for. All three move with the constant, or the operator is
// told a remedy computed from a stale number.
func TestProvisionRefusalMovesWithTheReserve(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"}}
	sessions := 100
	agent.Spec.Harness = &agentv1alpha1.HarnessSpec{Tuning: &agentv1alpha1.TuningSpec{MaxSessions: &sessions}}
	script := a2aProvisionScript(agent)
	for _, want := range []string{
		"required_consumers=332",
		"plus 32 reserved for the standing durables, the web rail and tasks/get replays.",
		"fits=$(( (live_consumers - 32) / 3 ))",
		"one session still needs 35",
	} {
		if !strings.Contains(script, want) {
			t.Errorf("the provision script does not contain %q; the refusal is computed from a reserve other than a2aTasksReservedConsumers=%d", want, a2aTasksReservedConsumers)
		}
	}
	if a2aTasksConsumerBudget(agent) != sessions*a2aSessionConsumersPerSession+a2aTasksReservedConsumers {
		t.Errorf("a2aTasksConsumerBudget = %d, want %d*%d + %d", a2aTasksConsumerBudget(agent), sessions, a2aSessionConsumersPerSession, a2aTasksReservedConsumers)
	}
}

// a2aBridgeConcurrency reads BRIDGE_CONCURRENCY off spec.deployment.sidecars
// the way the bridge reads it off its environment, and the comment above the
// reserve states the rule; this holds the function to each clause of it. The
// per-worker arithmetic the rule feeds -- 44 at 4, 56 at 6 -- is pinned
// beside the cases, so a change to a2aTasksReplayAsk or the tail factor that
// silently moved the reserve a declared install carries fails here.
func TestBridgeConcurrencyReadsTheSidecarLikeTheBridgeDoes(t *testing.T) {
	lit := func(v string) corev1.EnvVar { return corev1.EnvVar{Name: "BRIDGE_CONCURRENCY", Value: v} }
	fromRef := corev1.EnvVar{Name: "BRIDGE_CONCURRENCY", ValueFrom: &corev1.EnvVarSource{
		ConfigMapKeyRef: &corev1.ConfigMapKeySelector{LocalObjectReference: corev1.LocalObjectReference{Name: "eval"}, Key: "parallelism"},
	}}
	sidecar := func(name string, env ...corev1.EnvVar) corev1.Container {
		return corev1.Container{Name: name, Image: name + ":dev", Env: env}
	}
	for _, tc := range []struct {
		name        string
		deployment  *agentv1alpha1.DeploymentSpec
		want        int
		wantReserve int
	}{
		{"no deployment block is the default", nil, 2, 32},
		{"no sidecars is the default", &agentv1alpha1.DeploymentSpec{}, 2, 32},
		{"a sidecar that does not set it is not a bridge", &agentv1alpha1.DeploymentSpec{Sidecars: []corev1.Container{
			sidecar("fluent-bit", corev1.EnvVar{Name: "FLB_LOG_LEVEL", Value: "info"})}}, 2, 32},
		{"a literal is the count", &agentv1alpha1.DeploymentSpec{Sidecars: []corev1.Container{
			sidecar("hermes-bridge", lit("6"))}}, 6, 56},
		{"the presubmit's four", &agentv1alpha1.DeploymentSpec{Sidecars: []corev1.Container{
			sidecar("hermes-bridge", lit("4"))}}, 4, 44},
		{"the default written out is the default", &agentv1alpha1.DeploymentSpec{Sidecars: []corev1.Container{
			sidecar("hermes-bridge", lit("2"))}}, 2, 32},
		{"a valueFrom cannot be read here and is the default", &agentv1alpha1.DeploymentSpec{Sidecars: []corev1.Container{
			sidecar("hermes-bridge", fromRef)}}, 2, 32},
		// envFrom is not walked, so a sidecar with envFrom and no entry in
		// env declares nothing here and neither flag is set; the provision
		// script's NOTE, not the count, is where that shape is reported
		// (a2aBridgeEnvFromUnread).
		{"envFrom is not read, so nothing is declared", &agentv1alpha1.DeploymentSpec{Sidecars: []corev1.Container{
			{Name: "hermes-bridge", Image: "bridge:dev", EnvFrom: []corev1.EnvFromSource{{ConfigMapRef: &corev1.ConfigMapEnvSource{LocalObjectReference: corev1.LocalObjectReference{Name: "eval"}}}}}}}, 2, 32},
		{"a non-integer is the default, as envInt makes it", &agentv1alpha1.DeploymentSpec{Sidecars: []corev1.Container{
			sidecar("hermes-bridge", lit("six"))}}, 2, 32},
		{"an empty value is the default, as envInt makes it", &agentv1alpha1.DeploymentSpec{Sidecars: []corev1.Container{
			sidecar("hermes-bridge", lit(""))}}, 2, 32},
		{"zero is the default, as Config.defaults makes it", &agentv1alpha1.DeploymentSpec{Sidecars: []corev1.Container{
			sidecar("hermes-bridge", lit("0"))}}, 2, 32},
		{"a negative is the default, as Config.defaults makes it", &agentv1alpha1.DeploymentSpec{Sidecars: []corev1.Container{
			sidecar("hermes-bridge", lit("-3"))}}, 2, 32},
		{"two bridges sum", &agentv1alpha1.DeploymentSpec{Sidecars: []corev1.Container{
			sidecar("bridge-a", lit("4")), sidecar("fluent-bit"), sidecar("bridge-b", lit("6"))}}, 10, 80},
		{"a bridge that can be read and one that cannot", &agentv1alpha1.DeploymentSpec{Sidecars: []corev1.Container{
			sidecar("bridge-a", lit("6")), sidecar("bridge-b", fromRef)}}, 8, 68},
		{"the last entry of the name wins within one container", &agentv1alpha1.DeploymentSpec{Sidecars: []corev1.Container{
			sidecar("hermes-bridge", lit("3"), corev1.EnvVar{Name: "NATS_URL", Value: "nats://bus:4222"}, lit("5"))}}, 5, 50},
		// The kubelet expands $(NAME) in env[].value against the entries
		// declared before it, in declaration order, before envInt ever sees
		// the string, so the count is what the reference resolves to in the
		// pod. The rows follow expansion.Expand's rules: an unresolvable
		// reference is left as written, $$ is one $, and the chain resolves
		// because each earlier entry was expanded when it was declared.
		{"a reference to an earlier literal is that literal, as the kubelet expands it", &agentv1alpha1.DeploymentSpec{Sidecars: []corev1.Container{
			sidecar("hermes-bridge", corev1.EnvVar{Name: "EVAL_PARALLELISM", Value: "6"}, lit("$(EVAL_PARALLELISM)"))}}, 6, 56},
		{"a reference through an earlier reference resolves in declaration order, as the kubelet does", &agentv1alpha1.DeploymentSpec{Sidecars: []corev1.Container{
			sidecar("hermes-bridge", corev1.EnvVar{Name: "EVAL_TASK_PARALLELISM", Value: "6"}, corev1.EnvVar{Name: "EVAL_PARALLELISM", Value: "$(EVAL_TASK_PARALLELISM)"}, lit("$(EVAL_PARALLELISM)"))}}, 6, 56},
		{"a self-reference is the earlier entry of the same name", &agentv1alpha1.DeploymentSpec{Sidecars: []corev1.Container{
			sidecar("hermes-bridge", lit("4"), lit("$(BRIDGE_CONCURRENCY)"))}}, 4, 44},
		{"expansion is textual, so two references side by side are their digits", &agentv1alpha1.DeploymentSpec{Sidecars: []corev1.Container{
			sidecar("hermes-bridge", corev1.EnvVar{Name: "TENS", Value: "1"}, corev1.EnvVar{Name: "ONES", Value: "2"}, lit("$(TENS)$(ONES)"))}}, 12, 92},
		{"a reference to a later entry is left as written and is the default", &agentv1alpha1.DeploymentSpec{Sidecars: []corev1.Container{
			sidecar("hermes-bridge", lit("$(EVAL_PARALLELISM)"), corev1.EnvVar{Name: "EVAL_PARALLELISM", Value: "6"})}}, 2, 32},
		{"a reference to a valueFrom cannot be read here and is the default", &agentv1alpha1.DeploymentSpec{Sidecars: []corev1.Container{
			sidecar("hermes-bridge", corev1.EnvVar{Name: "EVAL_PARALLELISM", ValueFrom: fromRef.ValueFrom}, lit("$(EVAL_PARALLELISM)"))}}, 2, 32},
		{"a reference to a literal a later valueFrom shadows is the default", &agentv1alpha1.DeploymentSpec{Sidecars: []corev1.Container{
			sidecar("hermes-bridge", corev1.EnvVar{Name: "EVAL_PARALLELISM", Value: "6"}, corev1.EnvVar{Name: "EVAL_PARALLELISM", ValueFrom: fromRef.ValueFrom}, lit("$(EVAL_PARALLELISM)"))}}, 2, 32},
		{"a reference to a name no entry declares is left as written and is the default", &agentv1alpha1.DeploymentSpec{Sidecars: []corev1.Container{
			sidecar("hermes-bridge", lit("$(KUBERNETES_SERVICE_PORT)"))}}, 2, 32},
		{"$$ is one literal $, so $$(NAME) is not a reference and is the default", &agentv1alpha1.DeploymentSpec{Sidecars: []corev1.Container{
			sidecar("hermes-bridge", corev1.EnvVar{Name: "EVAL_PARALLELISM", Value: "6"}, lit("$$(EVAL_PARALLELISM)"))}}, 2, 32},
		{"an unclosed $( is literal characters and is the default", &agentv1alpha1.DeploymentSpec{Sidecars: []corev1.Container{
			sidecar("hermes-bridge", corev1.EnvVar{Name: "EVAL_PARALLELISM", Value: "6"}, lit("$(EVAL_PARALLELISM"))}}, 2, 32},
		{"a reference that resolves above the cap is the cap", &agentv1alpha1.DeploymentSpec{Sidecars: []corev1.Container{
			sidecar("hermes-bridge", corev1.EnvVar{Name: "EVAL_PARALLELISM", Value: "1000000000"}, lit("$(EVAL_PARALLELISM)"))}}, 1024, 6164},
		// The cap, which the bridge does not have. 20 + 6*1024 = 6164.
		{"the cap itself is a count", &agentv1alpha1.DeploymentSpec{Sidecars: []corev1.Container{
			sidecar("hermes-bridge", lit("1024"))}}, 1024, 6164},
		{"a literal above the cap is the cap, not the default", &agentv1alpha1.DeploymentSpec{Sidecars: []corev1.Container{
			sidecar("hermes-bridge", lit("1000000000"))}}, 1024, 6164},
		{"MaxInt64 is the cap and the reserve does not wrap", &agentv1alpha1.DeploymentSpec{Sidecars: []corev1.Container{
			sidecar("hermes-bridge", lit("9223372036854775807"))}}, 1024, 6164},
		{"two sidecars whose literals sum past the cap are the cap", &agentv1alpha1.DeploymentSpec{Sidecars: []corev1.Container{
			sidecar("bridge-a", lit("1000")), sidecar("bridge-b", lit("1000"))}}, 1024, 6164},
		{"two sidecars each at the cap are the cap", &agentv1alpha1.DeploymentSpec{Sidecars: []corev1.Container{
			sidecar("bridge-a", lit("9223372036854775807")), sidecar("bridge-b", lit("9223372036854775807"))}}, 1024, 6164},
		// Past int64 envInt cannot parse it and the bridge runs the default,
		// so the default is the count that mirrors the bridge, not the cap.
		{"a literal too wide for an int is the default, as envInt makes it", &agentv1alpha1.DeploymentSpec{Sidecars: []corev1.Container{
			sidecar("hermes-bridge", lit("99999999999999999999"))}}, 2, 32},
	} {
		t.Run(tc.name, func(t *testing.T) {
			agent := &agentv1alpha1.PlatformAgent{ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"}}
			agent.Spec.Deployment = tc.deployment
			if got := a2aBridgeConcurrency(agent); got != tc.want {
				t.Errorf("a2aBridgeConcurrency = %d, want %d", got, tc.want)
			}
			if got := a2aTasksReserve(agent); got != tc.wantReserve {
				t.Errorf("a2aTasksReserve = %d, want %d (16 fixed + 2*(1+1+2*%d+%d))", got, tc.wantReserve, tc.want, tc.want)
			}
			// The capped flag is what the two refusal surfaces read: set on
			// exactly the rows the cap decided, whether one literal or the sum.
			_, capped, defaulted := a2aBridgeWorkers(agent)
			if wantCapped := strings.Contains(tc.name, "past the cap") || strings.Contains(tc.name, "above the cap") || strings.Contains(tc.name, "MaxInt64") || strings.Contains(tc.name, "each at the cap"); capped != wantCapped {
				t.Errorf("a2aBridgeWorkers capped = %v, want %v", capped, wantCapped)
			}
			// The defaulted flag is what the status message reads: set on
			// exactly the rows where an entry sets the key and took the
			// default in place of a count it could not read, not on the rows
			// where the default is the count because nothing sets the key
			// or the CR wrote the default out.
			wantDefaulted := strings.Contains(tc.name, "cannot") ||
				(strings.Contains(tc.name, "is the default") && !strings.HasPrefix(tc.name, "no ") && !strings.Contains(tc.name, "does not set") && !strings.Contains(tc.name, "written out"))
			if defaulted != wantDefaulted {
				t.Errorf("a2aBridgeWorkers defaulted = %v, want %v", defaulted, wantDefaulted)
			}
		})
	}
	if a2aBridgeConcurrency(nil) != a2aBridgeDefaultConcurrency {
		t.Errorf("a2aBridgeConcurrency(nil) = %d, want the default", a2aBridgeConcurrency(nil))
	}
}

// The floor's edge, stated in the comment and pinned here: the first
// maxSessions whose budget clears a2aTasksMaxConsumersFloor, and the default
// install still under it.
func TestTheFloorHidesTheReserveUpToTen(t *testing.T) {
	first := 0
	for m := 1; m <= a2aTasksMaxConsumersFloor; m++ {
		agent := &agentv1alpha1.PlatformAgent{ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"}}
		agent.Spec.Harness = &agentv1alpha1.HarnessSpec{Tuning: &agentv1alpha1.TuningSpec{MaxSessions: &m}}
		if a2aTasksMaxConsumers(agent) > a2aTasksMaxConsumersFloor {
			first = m
			break
		}
	}
	if first != 11 {
		t.Errorf("the first maxSessions rendering above the floor is %d, the comment above a2aTasksReservedConsumers says 11", first)
	}
	def := &agentv1alpha1.PlatformAgent{ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"}}
	if got := a2aTasksConsumerBudget(def); got != 62 {
		t.Errorf("default budget = %d, the comment says 62", got)
	}
	if got := a2aTasksMaxConsumers(def); got != a2aTasksMaxConsumersFloor {
		t.Errorf("default install renders max_consumers=%d, want the floor %d", got, a2aTasksMaxConsumersFloor)
	}
}

// a2aBridgeSourceDirs are the bridge's own packages in the a2a module: the
// command that configures and starts it, and the package that consumes,
// dispatches and sweeps. Globbed rather than listed file by file, so a file
// added beside them is read too; an empty glob is a failure, not a pass.
var a2aBridgeSourceDirs = map[string]string{
	"../../../a2a/hermes-bridge":     "TasksGet",
	"../../../a2a/cmd/hermes-bridge": "New",
}

// a2aBridgeLookAheadCall names the bridge's pre-spawn replay by substring:
// lib.TaskInReplay today, and any sibling look-ahead read that ends up called
// something else, since a pre-spawn "is this task already in replay" call is
// a tasks/get replay whatever it is named.
const a2aBridgeLookAheadCall = "InReplay"

// The reserve has a look-ahead term because the bridge has a look-ahead, and
// this is what holds those two facts together.
//
// The bridge's worker calls lib.TaskInReplay at most once per dequeue (it
// answers from the in subject's newest message by direct get first) and
// holds at most Concurrency of those replays in hand at once (it paces the
// rest), and the reserve counts that ceiling: a2aTasksReplayBridgeLookAhead,
// one slot per worker and its tail.
// Before the call existed the term was out, because four slots reserved
// against code no render could reach moved the provision gate's first refused
// maxSessions on an existing 64-wide TASKS from 13 to 11 for nothing, and a
// test of the opposite sense failed on the day the call appeared. The hazard
// now runs this way -- the look-ahead is removed and nothing reminds anyone to
// take the term out -- so this fails on the day the call leaves the bridge's
// sources, naming the arithmetic that has to move with it.
//
// It is a source-reading test, so it is built to fail loudly and for the right
// reason: a glob that finds nothing, a file that is empty, a file that does
// not parse, and a directory whose known call the walk does not find are all
// failures of their own. That last one is the positive control -- the bridge
// really does call TasksGet, and main really does call hermesbridge.New -- so
// a walk that stopped collecting call names is reported as a broken extractor
// rather than as a look-ahead that left.
func TestBridgeLookAheadIsInTheA2AModule(t *testing.T) {
	var lookAheadCalls []string
	for dir, control := range a2aBridgeSourceDirs {
		files, err := filepath.Glob(filepath.Join(dir, "*.go"))
		if err != nil {
			t.Fatalf("globbing %s: %v", dir, err)
		}
		var sources []string
		for _, f := range files {
			if strings.HasSuffix(f, "_test.go") {
				continue
			}
			sources = append(sources, f)
		}
		if len(sources) == 0 {
			t.Fatalf("no non-test .go files under %s; the bridge's sources are no longer where this test reads them, and it would otherwise fail for the wrong reason", dir)
		}

		// Every call name in the package, as the parser sees it:
		// the Sel of `x.Foo()` and the name of a bare `foo()`.
		called := map[string][]string{}
		for _, src := range sources {
			info, err := os.Stat(src)
			if err != nil {
				t.Fatalf("stat %s: %v", src, err)
			}
			if info.Size() == 0 {
				t.Fatalf("%s is empty; a walk over nothing finds nothing, which this test must not read as an absent call", src)
			}
			fset := token.NewFileSet()
			f, err := parser.ParseFile(fset, src, nil, 0)
			if err != nil {
				t.Fatalf("parse %s: %v (this test cannot tell an absent call from a file it could not read, so a parse error is a failure)", src, err)
			}
			ast.Inspect(f, func(n ast.Node) bool {
				call, ok := n.(*ast.CallExpr)
				if !ok {
					return true
				}
				switch fun := call.Fun.(type) {
				case *ast.SelectorExpr:
					called[fun.Sel.Name] = append(called[fun.Sel.Name], src)
				case *ast.Ident:
					called[fun.Name] = append(called[fun.Name], src)
				}
				return true
			})
		}

		if len(called[control]) == 0 {
			t.Fatalf("the walk over %s found no call to %s, which is there: the extractor is broken, and a missing look-ahead reported below would be its symptom, not a finding", dir, control)
		}

		for name, where := range called {
			if strings.Contains(name, a2aBridgeLookAheadCall) {
				lookAheadCalls = append(lookAheadCalls, name+" in "+strings.Join(where, ", "))
			}
		}
	}

	// The guard itself. The call sites are logged on the way through, so a
	// run's record names where the look-ahead lives on the day it moves.
	t.Logf("bridge look-ahead reads: %s", strings.Join(lookAheadCalls, "; "))
	if len(lookAheadCalls) == 0 {
		t.Errorf("no bridge source calls a *%s read: the bridge no longer replays per spawn, so a2aTasksReplayBridgeLookAhead reserves slots for code no render reaches. Remove it from the a2aTasksReplayConsumers sum, which takes replays in flight to 6, a2aTasksReplayConsumers to 12 and a2aTasksReservedConsumers to 28, and re-derive the two tables above the constants and the numbers the tests here pin.",
			a2aBridgeLookAheadCall)
	}
}

// hack/ci-deploy.sh sizes the eval CR's maxSessions so that the budget for
// the bridge's worker count fits the TASKS the provision Job creates at the
// floor (gke-labs/kube-agents#2077). The operator renders the bridge now and
// counts its workers from the first render, so there is one provision Job,
// and the sizing keeps that single budget at or under the floor. It computes that from four
// numbers copied from this package -- the floor, the per-session count, and
// the reserve table's intercept and slope -- because a shell script cannot
// evaluate Go constants. This holds the four to the constants and the
// function they stand for, holds the arithmetic they feed to what it
// promises (at the presubmit's 4 workers and at 6, a budget within the
// floor, so TASKS is created at it; at 8 the floor cannot hold the
// reserve, and the lane relies on its Degraded gate), and decodes the values
// document the script hands the chart into the CR type, so the field path it
// names is one the API has: under platformAgent the document carries the
// CR's own spec paths, which the chart renders one for one
// (tests/test_ci_deploy_mode_next.py renders it to check that half). Read from the source, for the reason the SessionConsumerRoles
// test gives, and every step fails rather than defaults.
func TestCiDeploySizesMaxSessionsToTheTasksFloor(t *testing.T) {
	script, err := os.ReadFile(ciDeploySource)
	if err != nil {
		t.Fatalf("read %s: %v (if the script moved, this test's path must move with it; it is what keeps its budget constants honest)", ciDeploySource, err)
	}
	read := func(name string) int {
		m := regexp.MustCompile(`(?m)^readonly ` + name + `=(\d+)$`).FindSubmatch(script)
		if m == nil {
			t.Fatalf("no `readonly %s=<int>` in %s; section 2b's maxSessions sizing reads it", name, ciDeploySource)
		}
		n, err := strconv.Atoi(string(m[1]))
		if err != nil {
			t.Fatalf("%s in %s is %q, not an integer", name, ciDeploySource, m[1])
		}
		return n
	}
	floor, perSession := read("A2A_TASKS_FLOOR"), read("A2A_SESSION_CONSUMERS")
	fixed, perWorker := read("A2A_RESERVE_FIXED"), read("A2A_RESERVE_PER_WORKER")
	if floor != a2aTasksMaxConsumersFloor {
		t.Errorf("A2A_TASKS_FLOOR is %d, a2aTasksMaxConsumersFloor is %d: the script sizes maxSessions against a stream width the first provision no longer creates", floor, a2aTasksMaxConsumersFloor)
	}
	if perSession != a2aSessionConsumersPerSession {
		t.Errorf("A2A_SESSION_CONSUMERS is %d, a2aSessionConsumersPerSession is %d", perSession, a2aSessionConsumersPerSession)
	}
	if want := a2aTasksReservedConsumersFor(0); fixed != want {
		t.Errorf("A2A_RESERVE_FIXED is %d, the reserve at zero workers is %d", fixed, want)
	}
	if want := a2aTasksReservedConsumersFor(1) - a2aTasksReservedConsumersFor(0); perWorker != want {
		t.Errorf("A2A_RESERVE_PER_WORKER is %d, the reserve's slope is %d", perWorker, want)
	}
	// The script's two-term line is the table only while the table is linear
	// in the worker count.
	for _, w := range []int{1, 2, 4, 6, 8, a2aBridgeConcurrencyMax} {
		if got, want := a2aTasksReservedConsumersFor(w), fixed+perWorker*w; got != want {
			t.Errorf("reserve at %d workers = %d; the script's %d + %d*w says %d", w, got, fixed, perWorker, want)
		}
	}
	// Section 2b's arithmetic: bash's integer division truncates toward zero,
	// and the clamp holds the API's minimum.
	size := func(workers int) int {
		n := (floor - fixed - perWorker*workers) / perSession
		if n < 1 {
			n = 1
		}
		return n
	}
	format := regexp.MustCompile(`(?m)^readonly MODE_NEXT_HELM_VALUES_FORMAT='(.*)'$`).FindSubmatch(script)
	if format == nil {
		t.Fatalf("no `readonly MODE_NEXT_HELM_VALUES_FORMAT='...'` in %s; it is the values document that carries the mode and the cap", ciDeploySource)
	}
	for _, tc := range []struct {
		workers, want int
		fits          bool
	}{
		{4, 6, true},
		{6, 2, true},
		{8, 1, false},
	} {
		n := size(tc.workers)
		if n != tc.want {
			t.Errorf("at %d workers the script sizes maxSessions=%d, want %d", tc.workers, n, tc.want)
		}
		// The first change's values document, its platformAgent object
		// decoded as the CR's spec with unknown fields refused: the path is
		// spec.harness.tuning.maxSessions, and the mode rides in the same
		// upgrade so the first render sees both.
		var values struct {
			PlatformAgent json.RawMessage `json:"platformAgent"`
		}
		vdec := json.NewDecoder(strings.NewReader(fmt.Sprintf(string(format[1]), n)))
		vdec.DisallowUnknownFields()
		if err := vdec.Decode(&values); err != nil || values.PlatformAgent == nil {
			t.Fatalf("MODE_NEXT_HELM_VALUES_FORMAT is not a document with platformAgent alone at its top (%v)", err)
		}
		agent := &agentv1alpha1.PlatformAgent{}
		dec := json.NewDecoder(strings.NewReader(`{"spec":` + string(values.PlatformAgent) + `}`))
		dec.DisallowUnknownFields()
		if err := dec.Decode(agent); err != nil {
			t.Fatalf("MODE_NEXT_HELM_VALUES_FORMAT's platformAgent does not decode into a PlatformAgent spec: %v", err)
		}
		if agent.Spec.Mode == nil || *agent.Spec.Mode != "next" {
			t.Errorf("the first change does not set spec.mode: next (got %v)", agent.Spec.Mode)
		}
		if got := resolveA2AMaxSessions(agent); got != n {
			t.Errorf("the first change's maxSessions resolves to %d, want %d: the field path is not the one the operator reads", got, n)
		}
		// The render, with the bridge concurrency CI passes through the
		// operator's env: the operator renders the bridge and budgets it
		// from the first provision, so TASKS is created at the floor while
		// the budget fits, and wider (the lane's Degraded gate) when not.
		t.Setenv(a2aBridgeConcurrencyOperatorEnvVar, strconv.Itoa(tc.workers))
		budget := a2aTasksConsumerBudget(agent)
		if fits := budget <= a2aTasksMaxConsumersFloor; fits != tc.fits {
			t.Errorf("at %d workers with maxSessions=%d the budget is %d against a %d-wide TASKS; fits=%v, want %v", tc.workers, n, budget, a2aTasksMaxConsumersFloor, fits, tc.fits)
		}
		if got := a2aTasksMaxConsumers(agent); tc.fits && got != a2aTasksMaxConsumersFloor {
			t.Errorf("at %d workers the render creates TASKS at %d consumers, not the floor %d", tc.workers, got, a2aTasksMaxConsumersFloor)
		}
	}
}
