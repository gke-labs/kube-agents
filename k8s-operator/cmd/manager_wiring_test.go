package main

import (
	"go/ast"
	"go/parser"
	"go/token"
	"go/types"
	"os"
	"regexp"
	"strconv"
	"strings"
	"testing"
)

// Two lines in main() decide how much of the cluster this process holds in
// memory, and neither can be observed from a test that does not start a
// manager against a real API server. It reads main.go rather than running it,
// because main() dials a cluster and calls os.Exit.
//
// The value matters as much as the field, and more for one of the two than
// the other:
//
//   - Cache. Nothing else sets it. Delete the line and the manager is built
//     with controller-runtime's default options, keeping the managedFields of
//     every object it caches; set it to a bare cache.Options{} and the same
//     is true with the field still there. Every other test in the repository
//     passes either way -- manager_cache_test.go exercises
//     ManagerCacheOptions() directly and never reaches main.go.
//   - Client. controller.ManagerClientOptions is what makes "no Pod informer"
//     a property of the client rather than a convention the four known call
//     sites keep; drop it and the next cached Pod read anyone writes starts
//     one again.
//   - APIReader. SetupWithManager backfills it from the manager when it is
//     nil, so deleting the line is caught there rather than here; what the
//     backfill cannot catch is the line pointing somewhere else, because a
//     non-nil APIReader is left alone. mgr.GetClient() would put every Pod
//     read back on the cache with the field set and the reconciler running —
//     or it would, without the Client option above, which is why the two are
//     asserted together. The deletion is asserted too, so all three lines are
//     read the same way.

const (
	mainSourceFile           = "main.go"
	managerOptionsType       = "ctrl.Options"
	managerCacheField        = "Cache"
	managerCacheValue        = "controller.ManagerCacheOptions()"
	managerClientField       = "Client"
	managerClientValue       = "controller.ManagerClientOptions()"
	reconcilerType           = "controller.PlatformAgentReconciler"
	reconcilerAPIReaderField = "APIReader"
	reconcilerAPIReaderValue = "mgr.GetAPIReader()"
	memoryLimitCall          = "memlimit.Apply"
	memoryLimitCallEnvArg    = "operatorMemoryLimitEnv"
	memoryLimitCallShareArg  = "operatorMemoryLimitFraction"
	managerConstructorCall   = "ctrl.NewManager"
	managerStartCall         = "mgr.Start"
	// Below controller-runtime's 30 s default graceful-shutdown timeout, so
	// the kubelet kills a slow stop before the timeout releases the lease.
	operatorMaxTerminationGraceSeconds = 20
	// The webhook's namespace rule reaches the validator through this one
	// call's second argument. Every webhook test builds the validator by
	// hand, so a call that passed "" or another name would switch the rule
	// off on every install with every suite green.
	webhookSetupCall           = "agentwebhook.SetupPlatformAgentWebhookWithManager"
	webhookSetupArgs           = "mgr, operatorNamespace"
	webhookSetupNamespaceIdent = "operatorNamespace"
)

func TestMainHandsTheWebhookTheOperatorNamespace(t *testing.T) {
	fset := token.NewFileSet()
	file, err := parser.ParseFile(fset, mainSourceFile, nil, parser.SkipObjectResolution)
	if err != nil {
		t.Fatalf("parsing %s: %v", mainSourceFile, err)
	}
	pos, args := callSite(file, webhookSetupCall)
	if !pos.IsValid() {
		t.Fatalf("%s does not call %s: the PlatformAgent webhook is not registered", mainSourceFile, webhookSetupCall)
	}
	if args != webhookSetupArgs {
		t.Errorf("%s calls %s(%s), want (%s): the validator holds a PlatformAgent to the namespace in %s, and any other value or an empty one admits it anywhere",
			mainSourceFile, webhookSetupCall, args, webhookSetupArgs, webhookSetupNamespaceIdent)
	}
}

// The soft memory limit is the manager's one defence between "the cache
// outgrew the limit" and the OOM killer, and like the two lines above it
// cannot be observed without a running process: memlimit's own tests cover the
// derivation, and nothing but this reads main.go for the call. It has to come
// before ctrl.NewManager, because the cache's initial list is the allocation
// it exists to pace.
func TestMainAppliesTheMemoryLimitBeforeBuildingTheManager(t *testing.T) {
	fset := token.NewFileSet()
	file, err := parser.ParseFile(fset, mainSourceFile, nil, parser.SkipObjectResolution)
	if err != nil {
		t.Fatalf("parsing %s: %v", mainSourceFile, err)
	}
	applyPos, applyArgs := callSite(file, memoryLimitCall)
	if !applyPos.IsValid() {
		t.Fatalf("%s never calls %s: the manager runs under the runtime's default pacing whatever the container's limit", mainSourceFile, memoryLimitCall)
	}
	if want := memoryLimitCallEnvArg + ", " + memoryLimitCallShareArg; applyArgs != want {
		t.Errorf("%s is called with (%s), want (%s): the variable and the share are the two constants the manifests and the comments are written to", memoryLimitCall, applyArgs, want)
	}
	managerPos, _ := callSite(file, managerConstructorCall)
	if !managerPos.IsValid() {
		t.Fatalf("%s never calls %s", mainSourceFile, managerConstructorCall)
	}
	if applyPos >= managerPos {
		t.Errorf("%s is called after %s: the cache's initial list runs under the default pacing", memoryLimitCall, managerConstructorCall)
	}
}

// callSite returns the position of the first call to `package.Func` in file
// and its arguments rendered as source, or an invalid position when there is
// none.
func callSite(file *ast.File, callee string) (token.Pos, string) {
	pos, args := token.NoPos, ""
	ast.Inspect(file, func(n ast.Node) bool {
		if pos.IsValid() {
			return false
		}
		call, ok := n.(*ast.CallExpr)
		if !ok || literalTypeName(call.Fun) != callee {
			return true
		}
		rendered := make([]string, 0, len(call.Args))
		for _, arg := range call.Args {
			rendered = append(rendered, types.ExprString(arg))
		}
		pos, args = call.Pos(), strings.Join(rendered, ", ")
		return false
	})
	return pos, args
}

func TestMainHandsTheManagerItsCacheAndTheReconcilerALiveReader(t *testing.T) {
	fset := token.NewFileSet()
	file, err := parser.ParseFile(fset, mainSourceFile, nil, parser.SkipObjectResolution)
	if err != nil {
		t.Fatalf("parsing %s: %v", mainSourceFile, err)
	}

	for _, tc := range []struct {
		literal string
		field   string
		value   string
		why     string
	}{
		{managerOptionsType, managerCacheField, managerCacheValue,
			"the manager is built with a cache that keeps the managedFields of every cached object"},
		{managerOptionsType, managerClientField, managerClientValue,
			"any Pod read through the manager's client is served from the cache, which starts a cluster-wide Pod informer"},
		{reconcilerType, reconcilerAPIReaderField, reconcilerAPIReaderValue,
			"podReader serves Pod reads from the cache unless SetupWithManager's nil-backfill catches it, and a non-nil wrong value it cannot"},
	} {
		got, set := literalField(file, tc.literal, tc.field)
		if !set {
			t.Errorf("%s in %s sets no %s: %s", tc.literal, mainSourceFile, tc.field, tc.why)
			continue
		}
		if got != tc.value {
			t.Errorf("%s in %s sets %s to %s, want %s: %s", tc.literal, mainSourceFile, tc.field, got, tc.value, tc.why)
		}
	}
}

// literalField returns the rendered value some composite literal of the named
// type in file assigns to the named field, and whether it assigns it at all.
func literalField(file *ast.File, typeName, field string) (string, bool) {
	value, found := "", false
	ast.Inspect(file, func(n ast.Node) bool {
		lit, ok := n.(*ast.CompositeLit)
		if !ok || literalTypeName(lit.Type) != typeName {
			return true
		}
		for _, elt := range lit.Elts {
			kv, ok := elt.(*ast.KeyValueExpr)
			if !ok {
				continue
			}
			if key, ok := kv.Key.(*ast.Ident); ok && key.Name == field {
				value, found = types.ExprString(kv.Value), true
				return false
			}
		}
		return true
	})
	return value, found
}

// literalTypeName renders a composite literal's type as `package.Name`, the
// only shape the two literals above have.
func literalTypeName(expr ast.Expr) string {
	sel, ok := expr.(*ast.SelectorExpr)
	if !ok {
		return ""
	}
	pkg, ok := sel.X.(*ast.Ident)
	if !ok {
		return ""
	}
	return pkg.Name + "." + sel.Sel.Name
}

// LeaderElectionReleaseOnCancel hands the lease over on shutdown instead of
// letting it expire. controller-runtime documents it as unsafe unless the
// binary ends as soon as the manager stops: the next leader can start the
// moment the lease is released, so anything this process did after mgr.Start
// returned would run alongside it. That holds today only by the shape of
// main(), which nothing else checks: mgr.Start is its last statement, and it
// has no defer (which runs after Start returns) and no go statement (which
// could outlive it). Each of the four assertions below is one way to break it.
func TestMainReleasesTheLeaseOnlyWhereItExitsWithTheManager(t *testing.T) {
	fset := token.NewFileSet()
	file, err := parser.ParseFile(fset, mainSourceFile, nil, parser.SkipObjectResolution)
	if err != nil {
		t.Fatalf("parsing %s: %v", mainSourceFile, err)
	}
	if got, set := literalField(file, managerOptionsType, "LeaderElectionReleaseOnCancel"); !set || got != "true" {
		t.Errorf("%s in %s sets LeaderElectionReleaseOnCancel to %q (set: %v), want true: a new operator pod waits out the whole lease before its first reconcile", managerOptionsType, mainSourceFile, got, set)
	}
	var mainFn *ast.FuncDecl
	for _, decl := range file.Decls {
		if fn, ok := decl.(*ast.FuncDecl); ok && fn.Recv == nil && fn.Name.Name == "main" {
			mainFn = fn
		}
	}
	if mainFn == nil || len(mainFn.Body.List) == 0 {
		t.Fatalf("%s has no main function with a body", mainSourceFile)
	}
	// The last statement is `if err := mgr.Start(...); err != nil { ... }`,
	// and its body only logs and exits: that body is the one place code can
	// run after Start returns.
	last, ok := mainFn.Body.List[len(mainFn.Body.List)-1].(*ast.IfStmt)
	startsManager := false
	if ok {
		if assign, isAssign := last.Init.(*ast.AssignStmt); isAssign && len(assign.Rhs) == 1 {
			if call, isCall := assign.Rhs[0].(*ast.CallExpr); isCall && literalTypeName(call.Fun) == managerStartCall {
				startsManager = true
			}
		}
	}
	if !startsManager {
		t.Fatalf("main's last statement isn't `if err := %s(...); err != nil {...}`: something runs after the manager stops, and with the lease released another operator may already be leading", managerStartCall)
	}
	for _, stmt := range last.Body.List {
		expr, isExpr := stmt.(*ast.ExprStmt)
		call, isCall := (*ast.CallExpr)(nil), false
		if isExpr {
			call, isCall = expr.X.(*ast.CallExpr)
		}
		if !isCall || (literalTypeName(call.Fun) != "setupLog.Error" && literalTypeName(call.Fun) != "os.Exit") {
			t.Errorf("the branch after %s returns does more than log and exit (%s): it runs with the lease already released", managerStartCall, fset.Position(stmt.Pos()))
		}
	}
	if _, set := literalField(file, managerOptionsType, "GracefulShutdownTimeout"); set {
		t.Errorf("%s sets GracefulShutdownTimeout: the release is safe because the pod's %d s grace period kills a slow stop before controller-runtime's default 30 s timeout releases the lease mid-reconcile; recheck that before changing it", managerOptionsType, operatorMaxTerminationGraceSeconds)
	}
	for _, manifest := range []string{"../../charts/kube-agents/templates/operator-deployment.yaml", "../config/manager/manager.yaml"} {
		raw, err := os.ReadFile(manifest)
		if err != nil {
			t.Fatalf("reading %s: %v", manifest, err)
		}
		m := regexp.MustCompile(`terminationGracePeriodSeconds:\s*(\d+)`).FindSubmatch(raw)
		if m == nil {
			t.Errorf("%s sets no terminationGracePeriodSeconds: Kubernetes' default 30 s lets a stop reach controller-runtime's 30 s timeout, which releases the lease while a reconcile may still run", manifest)
			continue
		}
		if n, _ := strconv.Atoi(string(m[1])); n > operatorMaxTerminationGraceSeconds {
			t.Errorf("%s gives the operator %d s to stop, want at most %d: past controller-runtime's 30 s default the lease is released while a reconcile may still run", manifest, n, operatorMaxTerminationGraceSeconds)
		}
	}
	ast.Inspect(mainFn.Body, func(n ast.Node) bool {
		switch n.(type) {
		case *ast.DeferStmt:
			t.Errorf("main has a defer at %s: it runs after mgr.Start returns, when the lease is already released", fset.Position(n.Pos()))
		case *ast.GoStmt:
			t.Errorf("main starts a goroutine at %s: it can still be running after mgr.Start returns and the lease is released", fset.Position(n.Pos()))
		case *ast.FuncLit:
			return false // a closure handed to something else isn't main's own statement
		}
		return true
	})
}
