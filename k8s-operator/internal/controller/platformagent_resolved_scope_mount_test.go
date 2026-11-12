package controller

import (
	"testing"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/utils/ptr"
)

func resolvedScopeVolume(vols []corev1.Volume) *corev1.Volume {
	for i := range vols {
		if vols[i].Name == resolvedScopeVolumeName {
			return &vols[i]
		}
	}
	return nil
}

// Under mode next the agent container reads the scope publisher's resolved
// scope (#5621): the <agent>-resolved-scope ConfigMap, mounted read-only as
// a directory (no subPath, so the kubelet updates it in place), optional
// (the publisher may not have written it yet), and named by an env var the
// managed .env pins, so the agent can't point its scripts at a file it wrote.
func TestTheAgentReadsTheResolvedScopeUnderModeNext(t *testing.T) {
	agent := a2aTestAgent()
	dep := buildDeployment(agent, "h1", "h2", "h3", "h4", nil, renderOptions{imageVolumeSupported: true})
	vol := resolvedScopeVolume(dep.Spec.Template.Spec.Volumes)
	if vol == nil || vol.ConfigMap == nil {
		t.Fatalf("no ConfigMap volume %q", resolvedScopeVolumeName)
	}
	if vol.ConfigMap.Name != scopeResolvedConfigMapName(agent) {
		t.Errorf("the volume projects %q, want %q", vol.ConfigMap.Name, scopeResolvedConfigMapName(agent))
	}
	if vol.ConfigMap.Optional == nil || !*vol.ConfigMap.Optional {
		t.Error("the volume must be optional: the publisher writes the ConfigMap after the agent starts")
	}
	if len(vol.ConfigMap.Items) != 1 || vol.ConfigMap.Items[0].Key != scopeResolvedDataKey || vol.ConfigMap.Items[0].Path != scopeResolvedDataKey {
		t.Errorf("the volume must project exactly %s: %+v", scopeResolvedDataKey, vol.ConfigMap.Items)
	}
	gateway := containerNamed(t, dep, "platform-agent")
	var mount *corev1.VolumeMount
	for i, m := range gateway.VolumeMounts {
		if m.Name == resolvedScopeVolumeName {
			mount = &gateway.VolumeMounts[i]
		}
	}
	if mount == nil {
		t.Fatalf("the agent container doesn't mount %s", resolvedScopeVolumeName)
	}
	if !mount.ReadOnly || mount.SubPath != "" || mount.MountPath != resolvedScopeDir {
		t.Errorf("mount %+v: want read-only at %s with no subPath", *mount, resolvedScopeDir)
	}
	want := resolvedScopeDir + "/" + scopeResolvedDataKey
	if got, ok := envValue(gateway, resolvedScopeFileEnvKey); !ok || got != want {
		t.Errorf("%s = %q (set %v), want %q", resolvedScopeFileEnvKey, got, ok, want)
	}
	if got, ok := managedEnvValue(renderManagedEnv(agent), resolvedScopeFileEnvKey); !ok || got != want {
		t.Errorf("the managed .env pins %s = %q (set %v), want %q", resolvedScopeFileEnvKey, got, ok, want)
	}
}

// A today install has no scope publisher, so nothing about the resolved
// scope renders.
func TestATodayInstallGetsNoResolvedScope(t *testing.T) {
	agent := a2aTestAgent()
	agent.Spec.Mode = ptr.To("today")
	dep := buildDeployment(agent, "h1", "h2", "h3", "h4", nil, renderOptions{imageVolumeSupported: true})
	if resolvedScopeVolume(dep.Spec.Template.Spec.Volumes) != nil {
		t.Error("a today install mounts the resolved scope")
	}
	gateway := containerNamed(t, dep, "platform-agent")
	for _, m := range gateway.VolumeMounts {
		if m.Name == resolvedScopeVolumeName || m.MountPath == resolvedScopeDir {
			t.Errorf("a today install mounts %+v", m)
		}
	}
	if _, ok := envValue(gateway, resolvedScopeFileEnvKey); ok {
		t.Errorf("a today install sets %s in the container env", resolvedScopeFileEnvKey)
	}
	// Pinned empty, not left out: an absent key is one the agent may write.
	if got, ok := managedEnvValue(renderManagedEnv(agent), resolvedScopeFileEnvKey); !ok || got != "" {
		t.Errorf("a today install pins %s = %q (set %v); want it pinned empty", resolvedScopeFileEnvKey, got, ok)
	}
}

// The shell sandbox doesn't get it: the helper scripts read the cluster list
// in the agent container and run only their gcloud and kubectl in the sandbox.
func TestTheSandboxGetsNoResolvedScope(t *testing.T) {
	agent := a2aTestAgent()
	sts := buildShellSandboxStatefulSet(agent, "sandbox-ssh", "", "settings-hash")
	if resolvedScopeVolume(sts.Spec.Template.Spec.Volumes) != nil {
		t.Error("the shell sandbox has the resolved scope's volume")
	}
	for _, c := range append(sts.Spec.Template.Spec.InitContainers, sts.Spec.Template.Spec.Containers...) {
		for _, m := range c.VolumeMounts {
			if m.Name == resolvedScopeVolumeName || m.MountPath == resolvedScopeDir {
				t.Errorf("sandbox container %s mounts %+v", c.Name, m)
			}
		}
		if _, ok := envValue(c, resolvedScopeFileEnvKey); ok {
			t.Errorf("sandbox container %s sets %s", c.Name, resolvedScopeFileEnvKey)
		}
	}
}
