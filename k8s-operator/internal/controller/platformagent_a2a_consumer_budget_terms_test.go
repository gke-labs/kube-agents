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
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	a2aManifestsSource  = "platformagent_a2a_manifests.go"
	a2aBridgeMainSource = "../../../a2a/cmd/hermes-bridge/main.go"
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
	"a2aTasksStandingDurables":     a2aTasksStandingDurables,
	"a2aTasksAuditDurableHeadroom": a2aTasksAuditDurableHeadroom,
	"a2aTasksIncarnationOverlap":   a2aTasksIncarnationOverlap,
	"a2aTasksWebReaders":           a2aTasksWebReaders,
	"a2aTasksReplayConsumers":      a2aTasksReplayConsumers,
	"a2aTasksReservedConsumers":    a2aTasksReservedConsumers,
	"a2aTasksReplayBridgeDispatch": a2aTasksReplayBridgeDispatch,
	"a2aTasksReplayGatewaySweep":   a2aTasksReplayGatewaySweep,
	"a2aTasksReplayAsks":           a2aTasksReplayAsks,
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
	inFlight := a2aTasksReplayBridgeDispatch + a2aTasksReplayGatewaySweep + a2aTasksReplayAsks
	if inFlight != 6 {
		t.Errorf("replays in flight = %d, the table above a2aTasksReservedConsumers says 6; re-derive the row that moved and the table with it", inFlight)
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
		"required_consumers=328",
		"plus 28 reserved for the standing durables, the web rail and tasks/get replays.",
		"fits=$(( (live_consumers - 28) / 3 ))",
		"one session still needs 31",
	} {
		if !strings.Contains(script, want) {
			t.Errorf("the provision script does not contain %q; the refusal is computed from a reserve other than a2aTasksReservedConsumers=%d", want, a2aTasksReservedConsumers)
		}
	}
	if a2aTasksConsumerBudget(agent) != sessions*a2aSessionConsumersPerSession+a2aTasksReservedConsumers {
		t.Errorf("a2aTasksConsumerBudget = %d, want %d*%d + %d", a2aTasksConsumerBudget(agent), sessions, a2aSessionConsumersPerSession, a2aTasksReservedConsumers)
	}
}

// The floor's edge, stated in the comment and pinned here: the first
// maxSessions whose budget clears a2aTasksMaxConsumersFloor, and the default
// install still under it.
func TestTheFloorHidesTheReserveUpToTwelve(t *testing.T) {
	first := 0
	for m := 1; m <= a2aTasksMaxConsumersFloor; m++ {
		agent := &agentv1alpha1.PlatformAgent{ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"}}
		agent.Spec.Harness = &agentv1alpha1.HarnessSpec{Tuning: &agentv1alpha1.TuningSpec{MaxSessions: &m}}
		if a2aTasksMaxConsumers(agent) > a2aTasksMaxConsumersFloor {
			first = m
			break
		}
	}
	if first != 13 {
		t.Errorf("the first maxSessions rendering above the floor is %d, the comment above a2aTasksReservedConsumers says 13", first)
	}
	def := &agentv1alpha1.PlatformAgent{ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"}}
	if got := a2aTasksConsumerBudget(def); got != 58 {
		t.Errorf("default budget = %d, the comment says 58", got)
	}
	if got := a2aTasksMaxConsumers(def); got != a2aTasksMaxConsumersFloor {
		t.Errorf("default install renders max_consumers=%d, want the floor %d", got, a2aTasksMaxConsumersFloor)
	}
}

// a2aBridgeSourceDirs are the bridge's own packages in the a2a module: the
// command that configures and starts it, and the package that consumes,
// dispatches and sweeps. Globbed rather than listed file by file, so a file
// gke-labs#2010 adds is read too; an empty glob is a failure, not a pass.
var a2aBridgeSourceDirs = map[string]string{
	"../../../a2a/hermes-bridge":     "TasksGet",
	"../../../a2a/cmd/hermes-bridge": "New",
}

// The reserve has no look-ahead term because the bridge has no look-ahead,
// and this is what holds those two facts together.
//
// gke-labs#2010 adds lib.TaskInReplay, called once per spawn from each of the
// bridge's workers, and the reserve carried a term for it before the call
// existed: four slots on every install, reserved against code no render could
// reach, moving the provision gate's first refused maxSessions on an existing
// 64-wide TASKS from 13 to 11. The term came out. The hazard now runs the
// other way -- #2010 lands and nothing reminds anyone to put it back -- so
// this fails on the day the call appears in the bridge's sources, naming the
// arithmetic that has to move with it.
//
// It is a source-reading test, so it is built to fail loudly rather than
// quietly: a glob that finds nothing, a file that is empty, a file that does
// not parse, and a directory whose known call the walk does not find are all
// failures. That last one is the positive control -- the bridge really does
// call TasksGet, and main really does call hermesbridge.New, so a walk that
// stopped collecting call names would report "no TaskInReplay" and pass
// forever without it.
func TestBridgeLookAheadIsNotInTheA2AModule(t *testing.T) {
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
			t.Fatalf("no non-test .go files under %s; the bridge's sources are no longer where this test reads them, and it would otherwise pass by reading nothing", dir)
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
			t.Fatalf("the walk over %s found no call to %s, which is there: the extractor is broken, and every assertion below it would pass by finding nothing", dir, control)
		}

		// The guard itself. TaskInReplay by name, and any sibling
		// look-ahead read that ends up called something else: a
		// pre-spawn "is this task already in replay" call is a
		// tasks/get replay whatever it is named.
		for name, where := range called {
			if !strings.Contains(name, "InReplay") {
				continue
			}
			t.Errorf("%s calls %s (in %s): the bridge replays per spawn again, so the reserve needs its look-ahead term back. Restore a2aTasksReplayBridgeLookAhead = a2aBridgeDefaultConcurrency to the a2aTasksReplayConsumers sum, which takes replays in flight to 8, a2aTasksReplayConsumers to 16 and a2aTasksReservedConsumers to 32, and re-derive the two tables above the constants and the numbers the tests here pin. Without it the budget under-counts by one slot per bridge worker and its tail.",
				dir, name, strings.Join(where, ", "))
		}
	}
}
