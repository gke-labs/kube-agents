package controller

import (
	"encoding/json"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func testTier(name, mode string) agentv1alpha1.Tier {
	return agentv1alpha1.Tier{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "test-ns"},
		Spec:       agentv1alpha1.TierSpec{Mode: mode},
	}
}

// The tiers file carries every Tier by name and mode, sorted, and leaves out
// one being deleted. Its shape is what capability.ParseTierFile reads: one
// key, `tiers`, of {name, mode}.
func TestTheTiersConfigMapListsEveryTierSortedByName(t *testing.T) {
	agent := a2aTestAgent()
	gone := testTier("gone", agentv1alpha1.TierModeInteractive)
	gone.DeletionTimestamp = &metav1.Time{Time: time.Now()}
	cm, err := buildA2ATiersConfigMap(agent, []agentv1alpha1.Tier{
		testTier("unattended", agentv1alpha1.TierModeUnattended),
		testTier("oncall", agentv1alpha1.TierModeInteractive),
		testTier("read", agentv1alpha1.TierModeInteractive),
		gone,
	})
	if err != nil {
		t.Fatal(err)
	}
	if cm.Name != agent.Name+"-a2a-tiers" || cm.Namespace != agent.Namespace {
		t.Fatalf("name %s/%s", cm.Namespace, cm.Name)
	}
	var f map[string][]map[string]string
	if err := json.Unmarshal([]byte(cm.Data[a2aTiersKey]), &f); err != nil {
		t.Fatal(err)
	}
	want := []map[string]string{
		{"name": "oncall", "mode": "interactive"},
		{"name": "read", "mode": "interactive"},
		{"name": "unattended", "mode": "unattended"},
	}
	if len(f) != 1 || len(f["tiers"]) != len(want) {
		t.Fatalf("file %v", f)
	}
	for i, w := range want {
		if f["tiers"][i]["name"] != w["name"] || f["tiers"][i]["mode"] != w["mode"] || len(f["tiers"][i]) != 2 {
			t.Errorf("tier %d = %v, want %v", i, f["tiers"][i], w)
		}
	}
	empty, err := buildA2ATiersConfigMap(agent, nil)
	if err != nil || empty.Data[a2aTiersKey] != "{\n  \"tiers\": []\n}\n" {
		t.Errorf("no tiers renders %q, %v; want an empty list, which serves the compiled-in tiers", empty.Data[a2aTiersKey], err)
	}
}

// The verifier, the gateway and the callout each mount the tiers ConfigMap,
// optionally and as a directory (so an edit reaches the file), and point
// A2A_TIERS_FILE at it.
func TestTheThreeTierCheckersMountTheTiersFile(t *testing.T) {
	agent := a2aTestAgent()
	for name, tc := range map[string]struct {
		spec      corev1.PodSpec
		container string
	}{
		"verifier": {buildA2AVerifierDeployment(agent).Spec.Template.Spec, a2aVerifierContainerName},
		"gateway":  {buildA2AGatewayDeployment(agent).Spec.Template.Spec, a2aGatewayContainerName},
		"callout":  {buildA2ACalloutDeployment(agent, "", 1).Spec.Template.Spec, "callout"},
	} {
		var vol *corev1.Volume
		for i := range tc.spec.Volumes {
			if tc.spec.Volumes[i].Name == a2aTiersVolume {
				vol = &tc.spec.Volumes[i]
			}
		}
		if vol == nil || vol.ConfigMap == nil || vol.ConfigMap.Name != agent.Name+"-a2a-tiers" ||
			vol.ConfigMap.Optional == nil || !*vol.ConfigMap.Optional {
			t.Errorf("%s: tiers volume %+v, want the optional %s-a2a-tiers ConfigMap", name, vol, agent.Name)
		}
		var c *corev1.Container
		for i := range tc.spec.Containers {
			if tc.spec.Containers[i].Name == tc.container {
				c = &tc.spec.Containers[i]
			}
		}
		if c == nil {
			t.Fatalf("%s: no container %s", name, tc.container)
		}
		var mount *corev1.VolumeMount
		for i := range c.VolumeMounts {
			if c.VolumeMounts[i].Name == a2aTiersVolume {
				mount = &c.VolumeMounts[i]
			}
		}
		if mount == nil || mount.MountPath != a2aTiersMountPath || mount.SubPath != "" || !mount.ReadOnly {
			t.Errorf("%s: tiers mount %+v, want %s, read-only, with no subPath", name, mount, a2aTiersMountPath)
		}
		env := ""
		for _, e := range c.Env {
			if e.Name == a2aTiersFileEnvVar {
				env = e.Value
			}
		}
		if env != a2aTiersMountPath+"/"+a2aTiersKey {
			t.Errorf("%s: %s = %q", name, a2aTiersFileEnvVar, env)
		}
	}
}
