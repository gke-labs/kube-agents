package controller

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/utils/ptr"
)

// runStaleMeta runs the init step's script, as the container does, against a
// store directory on this machine.
func runStaleMeta(t *testing.T, store, podName string) (string, error) {
	t.Helper()
	cmd := exec.Command("sh", "-c", a2aNATSStaleMetaScript, a2aNATSStaleMetaContainer, store, a2aAccountSys, "platformagent-a2a-nats-0")
	cmd.Env = []string{"POD_NAME=" + podName, "PATH=" + os.Getenv("PATH")}
	out, err := cmd.CombinedOutput()
	return string(out), err
}

// seedStore lays out a store the way a server that ran clustered leaves it:
// the cluster metadata under the system account, and the streams beside it.
func seedStore(t *testing.T) (store, meta, streams string) {
	t.Helper()
	store = t.TempDir()
	meta = filepath.Join(store, "jetstream", a2aAccountSys, "_js_")
	streams = filepath.Join(store, "jetstream", "APP", "streams", "TASKS")
	for _, d := range []string{filepath.Join(meta, "_meta_"), streams} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(filepath.Join(meta, "_meta_", "meta.inf"), []byte("stale"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(streams, "msgs.blk"), []byte("data"), 0o600); err != nil {
		t.Fatal(err)
	}
	return store, meta, streams
}

func exists(p string) bool { _, err := os.Lstat(p); return err == nil }

func TestStaleMetaRemovesExactlyTheClusterMetadataOnOrdinalZero(t *testing.T) {
	store, meta, streams := seedStore(t)
	out, err := runStaleMeta(t, store, "platformagent-a2a-nats-0")
	if err != nil {
		t.Fatalf("script failed: %v\n%s", err, out)
	}
	if exists(meta) {
		t.Errorf("%s survived", meta)
	}
	if !exists(filepath.Join(streams, "msgs.blk")) || !exists(filepath.Join(store, "jetstream", a2aAccountSys)) {
		t.Error("the step removed more than the cluster metadata")
	}
	if !strings.Contains(out, "removing "+meta+" (") || !strings.Contains(out, "KiB)") {
		t.Errorf("the path and size were not logged:\n%s", out)
	}
}

func TestStaleMetaLeavesAnyOtherPodAlone(t *testing.T) {
	store, meta, _ := seedStore(t)
	for _, pod := range []string{"platformagent-a2a-nats-1", "platformagent-a2a-nats-10", "", "other-0"} {
		out, err := runStaleMeta(t, store, pod)
		if err != nil {
			t.Fatalf("pod %q: script failed: %v\n%s", pod, err, out)
		}
		if !exists(meta) {
			t.Fatalf("pod %q removed the metadata", pod)
		}
		if !strings.Contains(out, "leaving "+meta+" alone") {
			t.Errorf("pod %q: no refusal logged:\n%s", pod, out)
		}
	}
}

// A symbolic link anywhere on the path is refused, failing the pod, and
// nothing it points at is touched.
func TestStaleMetaRefusesASymbolicLinkOnThePath(t *testing.T) {
	for _, link := range []string{"jetstream", filepath.Join("jetstream", a2aAccountSys), filepath.Join("jetstream", a2aAccountSys, "_js_")} {
		t.Run(link, func(t *testing.T) {
			store := t.TempDir()
			elsewhere := t.TempDir()
			victim := filepath.Join(elsewhere, "keep")
			if err := os.MkdirAll(filepath.Join(elsewhere, a2aAccountSys, "_js_"), 0o755); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(victim, []byte("x"), 0o600); err != nil {
				t.Fatal(err)
			}
			parent := filepath.Dir(filepath.Join(store, link))
			if err := os.MkdirAll(parent, 0o755); err != nil {
				t.Fatal(err)
			}
			if err := os.Symlink(elsewhere, filepath.Join(store, link)); err != nil {
				t.Fatal(err)
			}
			out, err := runStaleMeta(t, store, "platformagent-a2a-nats-0")
			if err == nil {
				t.Fatalf("a symbolic link at %s was not refused:\n%s", link, out)
			}
			if !strings.Contains(out, "is a symbolic link") {
				t.Errorf("no refusal logged:\n%s", out)
			}
			if !exists(victim) || !exists(filepath.Join(elsewhere, a2aAccountSys, "_js_")) {
				t.Error("the step followed the link")
			}
		})
	}
}

func TestStaleMetaWithNothingToRemoveSucceeds(t *testing.T) {
	out, err := runStaleMeta(t, t.TempDir(), "platformagent-a2a-nats-0")
	if err != nil || !strings.Contains(out, "absent; nothing to remove") {
		t.Fatalf("err=%v out=%s", err, out)
	}
}

// Rendered on a one-server bus only, mounting the claim alone, with no token,
// no other env and a read-only root; and its path comes from what nats.conf
// renders.
func TestStaleMetaStepIsRenderedOnlyOnAOneServerBus(t *testing.T) {
	agent := a2aTestAgent()
	for _, tc := range []struct {
		replicas int32
		want     bool
	}{{1, true}, {3, false}} {
		agent.Status.BusReplicas = tc.replicas
		sts := buildA2ANATSStatefulSet(agent, "hash")
		inits := sts.Spec.Template.Spec.InitContainers
		if got := len(inits) == 1; got != tc.want {
			t.Fatalf("replicas %d: init containers %+v", tc.replicas, inits)
		}
		if !tc.want {
			continue
		}
		c := inits[0]
		if c.Name != a2aNATSStaleMetaContainer || c.Image != a2aNATSImage() {
			t.Errorf("init container %s on %s", c.Name, c.Image)
		}
		if len(c.VolumeMounts) != 1 || c.VolumeMounts[0].Name != a2aNATSDataClaim || c.VolumeMounts[0].MountPath != a2aNATSStoreDir {
			t.Errorf("mounts %+v, want the claim alone at %s", c.VolumeMounts, a2aNATSStoreDir)
		}
		if len(c.Env) != 1 || c.Env[0].Name != "POD_NAME" || len(c.EnvFrom) != 0 {
			t.Errorf("env %+v envFrom %+v, want POD_NAME alone", c.Env, c.EnvFrom)
		}
		if sc := c.SecurityContext; sc == nil || !ptr.Deref(sc.ReadOnlyRootFilesystem, false) || ptr.Deref(sc.AllowPrivilegeEscalation, true) {
			t.Errorf("security context %+v", sc)
		}
		if sts.Spec.Template.Spec.AutomountServiceAccountToken == nil || *sts.Spec.Template.Spec.AutomountServiceAccountToken {
			t.Error("the bus pod mounts a ServiceAccount token")
		}
		args := strings.Join(c.Command[3:], " ")
		if want := strings.Join([]string{a2aNATSStaleMetaContainer, a2aNATSStoreDir, a2aAccountSys, sts.Name + "-0"}, " "); args != want {
			t.Errorf("arguments %q, want %q", args, want)
		}
		for _, r := range []corev1.ResourceName{corev1.ResourceCPU, corev1.ResourceMemory} {
			if c.Resources.Requests[r] != c.Resources.Limits[r] {
				t.Errorf("%s request %v != limit %v", r, c.Resources.Requests[r], c.Resources.Limits[r])
			}
		}
	}
	conf := renderA2ANATSConf(agent, func(string) string { return "pw" }, &a2aCalloutKeys{IssuerPublic: "A", XKeyPublic: "X"})
	if !strings.Contains(conf, "store_dir: "+a2aNATSStoreDir+"\n") || !strings.Contains(conf, "system_account: "+a2aAccountSys+"\n") {
		t.Error("nats.conf no longer renders the store_dir and system_account the step's path is built from")
	}
	// Globbing off, and no pattern a shell could expand had it been on.
	if !strings.HasPrefix(a2aNATSStaleMetaScript, "set -euf\n") || strings.ContainsAny(a2aNATSStaleMetaScript, "*?") {
		t.Error("the script globs, or holds a glob pattern")
	}
}
