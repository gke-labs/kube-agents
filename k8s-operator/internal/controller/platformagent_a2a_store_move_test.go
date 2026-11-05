package controller

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// a2aNATSServerExec is the tail of a2aNATSStartupScript the tests below swap
// for a marker, so the script runs as rendered up to the point it would
// start the server.
const a2aNATSServerExec = "exec nats-server -c /etc/nats/nats.conf"

// runA2AStoreMove runs the rendered startup script under /bin/sh with /data
// mapped to root and the server start replaced by a marker. It returns
// whether the server would have started, and stderr.
func runA2AStoreMove(t *testing.T, root string) (bool, string) {
	t.Helper()
	if !strings.Contains(a2aNATSStartupScript, a2aNATSServerExec) || strings.Count(a2aNATSStartupScript, "/data/") < 3 {
		t.Fatalf("a2aNATSStartupScript no longer has the shape this test rewrites (%q); the test would run something else", a2aNATSStartupScript)
	}
	script := strings.ReplaceAll(a2aNATSStartupScript, "/data/", root+"/")
	script = strings.Replace(script, a2aNATSServerExec, "exec echo nats-server-started", 1)
	cmd := exec.Command("/bin/sh", "-c", script)
	var stderr strings.Builder
	cmd.Stderr = &stderr
	out, _ := cmd.Output()
	return strings.TrimSpace(string(out)) == "nats-server-started", stderr.String()
}

func storeDirs(t *testing.T, root string) (app, trusted bool) {
	t.Helper()
	_, errApp := os.Stat(filepath.Join(root, "jetstream", "APP"))
	_, errTrusted := os.Stat(filepath.Join(root, "jetstream", "TRUSTED"))
	return errApp == nil, errTrusted == nil
}

func mkStore(t *testing.T, root, account, marker string) {
	t.Helper()
	dir := filepath.Join(root, "jetstream", account, "streams")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "marker"), []byte(marker), 0o644); err != nil {
		t.Fatal(err)
	}
}

func readMarker(t *testing.T, root, account string) string {
	t.Helper()
	b, err := os.ReadFile(filepath.Join(root, "jetstream", account, "streams", "marker"))
	if err != nil {
		return ""
	}
	return string(b)
}

// The bus's startup script, as rendered, on each store layout it can meet:
// the store moves from APP to TRUSTED once, a second start leaves it, both
// directories present starts on TRUSTED and says so, and a move that fails
// does not start the server on an empty store.
func TestTheBusStartupScriptMovesTheStoreOnce(t *testing.T) {
	t.Run("an APP store moves to TRUSTED", func(t *testing.T) {
		root := t.TempDir()
		mkStore(t, root, "APP", "upgraded")
		started, _ := runA2AStoreMove(t, root)
		app, trusted := storeDirs(t, root)
		if !started || app || !trusted || readMarker(t, root, "TRUSTED") != "upgraded" {
			t.Errorf("started=%v app=%v trusted=%v marker=%q; want the store under TRUSTED and the server started", started, app, trusted, readMarker(t, root, "TRUSTED"))
		}
	})
	t.Run("a second start leaves a TRUSTED store alone", func(t *testing.T) {
		root := t.TempDir()
		mkStore(t, root, "TRUSTED", "moved")
		started, stderr := runA2AStoreMove(t, root)
		app, trusted := storeDirs(t, root)
		if !started || app || !trusted || readMarker(t, root, "TRUSTED") != "moved" || stderr != "" {
			t.Errorf("started=%v app=%v trusted=%v stderr=%q; want the store untouched and the server started", started, app, trusted, stderr)
		}
	})
	t.Run("both directories start on TRUSTED and say so", func(t *testing.T) {
		root := t.TempDir()
		mkStore(t, root, "TRUSTED", "kept")
		mkStore(t, root, "APP", "written-after-rollback")
		started, stderr := runA2AStoreMove(t, root)
		if !started || readMarker(t, root, "TRUSTED") != "kept" || readMarker(t, root, "APP") != "written-after-rollback" {
			t.Errorf("started=%v TRUSTED=%q APP=%q; want both left in place and the server started", started, readMarker(t, root, "TRUSTED"), readMarker(t, root, "APP"))
		}
		if !strings.Contains(stderr, "both") || !strings.Contains(stderr, "APP left in place") {
			t.Errorf("stderr = %q; want the both-directories warning", stderr)
		}
	})
	t.Run("a fresh store starts with nothing to move", func(t *testing.T) {
		root := t.TempDir()
		started, _ := runA2AStoreMove(t, root)
		if app, trusted := storeDirs(t, root); !started || app || trusted {
			t.Errorf("started=%v app=%v trusted=%v; want the server started and nothing created", started, app, trusted)
		}
	})
	t.Run("a failed move does not start the server", func(t *testing.T) {
		if os.Geteuid() == 0 {
			t.Skip("root ignores the directory permission this case uses to make the move fail")
		}
		root := t.TempDir()
		mkStore(t, root, "APP", "stuck")
		js := filepath.Join(root, "jetstream")
		if err := os.Chmod(js, 0o555); err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { _ = os.Chmod(js, 0o755) })
		started, _ := runA2AStoreMove(t, root)
		if started {
			t.Error("the server started after the store move failed; it would have opened an empty TRUSTED store")
		}
		if readMarker(t, root, "APP") != "stuck" {
			t.Error("the APP store is gone after a failed move")
		}
	})
}

// The script above is the one the bus runs: the StatefulSet's container
// command is a2aNATSStartupScript under /bin/sh, so a test of the constant is
// a test of the pod.
func TestTheBusContainerRunsTheStartupScript(t *testing.T) {
	sts := buildA2ANATSStatefulSet(a2aTestAgent(), "hash")
	for _, c := range sts.Spec.Template.Spec.Containers {
		if c.Name != "nats" {
			continue
		}
		if len(c.Command) != 3 || c.Command[0] != "/bin/sh" || c.Command[1] != "-c" || c.Command[2] != a2aNATSStartupScript {
			t.Errorf("the nats container's command = %q, want /bin/sh -c a2aNATSStartupScript", c.Command)
		}
		return
	}
	t.Fatal("the bus StatefulSet has no nats container")
}
