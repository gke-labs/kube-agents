package profilepod

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/utils/ptr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func profile(tools ...agentv1alpha1.AgentProfileTool) *agentv1alpha1.AgentProfile {
	p := &agentv1alpha1.AgentProfile{}
	p.Spec.Persona.Image = "registry.example/persona:1"
	p.Spec.Tools = tools
	return p
}

func TestPersonaMountsReadOnlyAtSlashPersona(t *testing.T) {
	r := RenderPersonaAndTools(profile(), true)
	if len(r.Volumes) != 1 || len(r.Mounts) != 1 {
		t.Fatalf("want one volume and one mount for a profile with no tools, got %d and %d", len(r.Volumes), len(r.Mounts))
	}
	v, m := r.Volumes[0], r.Mounts[0]
	if v.Image == nil || v.Image.Reference != "registry.example/persona:1" {
		t.Errorf("persona volume is not an image volume of persona.image: %+v", v.VolumeSource)
	}
	if v.Image.PullPolicy != corev1.PullIfNotPresent {
		t.Errorf("default pull policy = %q, want IfNotPresent", v.Image.PullPolicy)
	}
	if PersonaMountPath != "/persona" {
		t.Errorf("PersonaMountPath = %q; the worker and the persona format both expect /persona", PersonaMountPath)
	}
	if m.MountPath != PersonaMountPath || !m.ReadOnly || m.Name != v.Name {
		t.Errorf("persona mount = %+v, want read-only %s on volume %q", m, PersonaMountPath, v.Name)
	}
	if m.SubPath != "files" {
		t.Errorf("persona mount subPath = %q, want files: the payload is the image's /files in both modes", m.SubPath)
	}
	if r.PathPrefix != "" || len(r.InitContainers) != 0 {
		t.Errorf("no tools and image volumes: want no PATH prefix and no init containers, got %q and %d", r.PathPrefix, len(r.InitContainers))
	}
}

func TestToolsMountReadOnlyAndJoinPathInListOrder(t *testing.T) {
	never := corev1.PullNever
	r := RenderPersonaAndTools(profile(
		agentv1alpha1.AgentProfileTool{Name: "jq", Image: "registry.example/jq:1"},
		agentv1alpha1.AgentProfileTool{Name: "helm", Image: "registry.example/helm:3", ImagePullPolicy: &never},
	), true)

	if got, want := r.PathPrefix, "/opt/tools/jq/bin:/opt/tools/helm/bin"; got != want {
		t.Errorf("PathPrefix = %q, want %q (list order)", got, want)
	}
	if len(r.Mounts) != 3 {
		t.Fatalf("want persona plus two tool mounts, got %d", len(r.Mounts))
	}
	for i, want := range []string{"/persona", "/opt/tools/jq", "/opt/tools/helm"} {
		if r.Mounts[i].MountPath != want || !r.Mounts[i].ReadOnly || r.Mounts[i].SubPath != "files" {
			t.Errorf("mount %d = %+v, want read-only %s with subPath files", i, r.Mounts[i], want)
		}
	}
	if r.Volumes[2].Image == nil || r.Volumes[2].Image.Reference != "registry.example/helm:3" || r.Volumes[2].Image.PullPolicy != corev1.PullNever {
		t.Errorf("helm volume does not carry its image and pull policy: %+v", r.Volumes[2].VolumeSource)
	}
	names := map[string]bool{}
	for _, v := range r.Volumes {
		if names[v.Name] {
			t.Errorf("volume name %q repeats", v.Name)
		}
		names[v.Name] = true
	}
}

func TestWithoutImageVolumesEachImageIsStagedIntoAnEmptyDir(t *testing.T) {
	r := RenderPersonaAndTools(profile(agentv1alpha1.AgentProfileTool{Name: "jq", Image: "registry.example/jq:1"}), false)

	if len(r.InitContainers) != 2 {
		t.Fatalf("want a staging init container per image, got %d", len(r.InitContainers))
	}
	for i, v := range r.Volumes {
		if v.EmptyDir == nil || v.Image != nil {
			t.Errorf("volume %q: want an emptyDir when image volumes are unsupported, got %+v", v.Name, v.VolumeSource)
		}
		c := r.InitContainers[i]
		m := r.Mounts[i]
		if len(c.VolumeMounts) != 1 || c.VolumeMounts[0].Name != v.Name || c.VolumeMounts[0].MountPath != m.MountPath {
			t.Errorf("init container %q does not stage into %q at %s: %+v", c.Name, v.Name, m.MountPath, c.VolumeMounts)
		}
		if c.VolumeMounts[0].ReadOnly {
			t.Errorf("init container %q mounts its volume read-only and cannot write it", c.Name)
		}
		if !m.ReadOnly || m.SubPath != "" {
			t.Errorf("staged harness mount %+v: want read-only with no subPath (the copy already took /files)", m)
		}
		for _, name := range []corev1.ResourceName{corev1.ResourceCPU, corev1.ResourceMemory} {
			if _, ok := c.Resources.Requests[name]; !ok {
				t.Errorf("init container %q has no %s request; a namespace quota on requests rejects the pod", c.Name, name)
			}
			if _, ok := c.Resources.Limits[name]; !ok {
				t.Errorf("init container %q has no %s limit", c.Name, name)
			}
		}
		sc := c.SecurityContext
		if sc == nil || !ptr.Deref(sc.ReadOnlyRootFilesystem, false) || ptr.Deref(sc.AllowPrivilegeEscalation, true) {
			t.Errorf("init container %q is not hardened: %+v", c.Name, sc)
		}
	}
	if r.InitContainers[0].Image != "registry.example/persona:1" || r.InitContainers[1].Image != "registry.example/jq:1" {
		t.Errorf("init containers run the wrong images: %q, %q", r.InitContainers[0].Image, r.InitContainers[1].Image)
	}
	if r.PathPrefix != "/opt/tools/jq/bin" {
		t.Errorf("PathPrefix = %q, want /opt/tools/jq/bin when staged too", r.PathPrefix)
	}
}

// runStage runs an init container's script with /files and the mount moved
// under a temp dir, so the copy's behaviour is checked, not its text.
func runStage(t *testing.T, c corev1.Container, mountPath string, payload map[string]string) (string, error) {
	t.Helper()
	root := t.TempDir()
	// A sibling of files/ the way a busybox base ships one: it must never
	// reach the mount.
	if err := os.MkdirAll(filepath.Join(root, "bin"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "bin", "sh"), []byte("shell"), 0o755); err != nil {
		t.Fatal(err)
	}
	if payload != nil {
		if err := os.MkdirAll(filepath.Join(root, "files"), 0o755); err != nil {
			t.Fatal(err)
		}
		for name, body := range payload {
			p := filepath.Join(root, "files", name)
			if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
				t.Fatal(err)
			}
		}
	}
	dst := filepath.Join(root, "mount")
	if err := os.MkdirAll(dst, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(dst, 0o755); err != nil {
		t.Fatal(err)
	}
	// A distinctive mode on files/ itself: a copy of the directory onto the
	// mount (rather than of its entries) re-applies it to the mount root, which
	// is what fails for a non-root container on GNU cp in the pod.
	if payload != nil {
		if err := os.Chmod(filepath.Join(root, "files"), 0o700); err != nil {
			t.Fatal(err)
		}
	}
	script := c.Command[2]
	script = strings.Replace(script, "src=/files", "src="+filepath.Join(root, "files"), 1)
	script = strings.Replace(script, `dst="`+mountPath+`"`, `dst="`+dst+`"`, 1)
	if strings.Contains(script, mountPath) {
		t.Fatalf("could not redirect the script's mount path: %s", script)
	}
	out, err := exec.Command("/bin/sh", "-c", script).CombinedOutput()
	if err != nil {
		return string(out), err
	}
	return dst, nil
}

func TestStagingCopiesOnlyTheFilesPayload(t *testing.T) {
	r := RenderPersonaAndTools(profile(agentv1alpha1.AgentProfileTool{Name: "jq", Image: "registry.example/jq:1"}), false)
	tool := r.InitContainers[1]
	payload := map[string]string{"bin/jq": "binary", ".config/jq.rc": "dotfile"}
	dst, err := runStage(t, tool, "/opt/tools/jq", payload)
	if err != nil {
		t.Fatalf("staging a well-formed tools image failed: %v %s", err, dst)
	}
	// The mount holds exactly the payload: every file under files/, dotfiles
	// included, and nothing else (runStage plants a bin/sh beside files/, the
	// way a busybox base does, so a second copy source would show here).
	var got []string
	_ = filepath.Walk(dst, func(p string, info os.FileInfo, err error) error {
		if err == nil && !info.IsDir() {
			rel, _ := filepath.Rel(dst, p)
			got = append(got, rel)
		}
		return nil
	})
	if len(got) != len(payload) {
		t.Errorf("mount holds %v, want exactly the payload %v", got, payload)
	}
	for name, body := range payload {
		if b, err := os.ReadFile(filepath.Join(dst, name)); err != nil || string(b) != body {
			t.Errorf("files/%s did not land at <mount>/%s: %v %q", name, name, err, b)
		}
	}
	info, err := os.Stat(dst)
	if err != nil {
		t.Fatal(err)
	}
	if got := info.Mode().Perm(); got != 0o755 {
		t.Errorf("staging changed the mount root's mode to %o (files/ is 0700): the script copied the /files directory onto the mount instead of its entries", got)
	}

	persona := r.InitContainers[0]
	if out, err := runStage(t, persona, "/persona", nil); err == nil {
		t.Errorf("staging an image with no /files succeeded; want a failure that stops the pod (got %s)", out)
	} else if !strings.Contains(out, "no ") {
		t.Errorf("the failure for a missing /files should say so, got %q", out)
	}
	if out, err := runStage(t, persona, "/persona", map[string]string{}); err == nil {
		t.Errorf("staging an image with an empty /files succeeded; want a failure (got %s)", out)
	} else if !strings.Contains(out, "is empty") {
		t.Errorf("the failure for an empty /files should say so, got %q", out)
	}
}

func TestDerivedNamesFitWithTheLongestToolName(t *testing.T) {
	longest := strings.Repeat("a", 24)
	r := RenderPersonaAndTools(profile(agentv1alpha1.AgentProfileTool{Name: longest, Image: "registry.example/x:1"}), false)
	for _, v := range r.Volumes {
		if len(v.Name) > 63 {
			t.Errorf("volume name %q is longer than a DNS-1123 label allows", v.Name)
		}
	}
	for _, c := range r.InitContainers {
		if len(c.Name) > 35 {
			t.Errorf("init container name %q is %d chars; GKE Autopilot (the staging path) allows 35", c.Name, len(c.Name))
		}
	}
}
