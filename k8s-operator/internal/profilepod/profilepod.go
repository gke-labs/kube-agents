// Package profilepod renders the parts of an AgentProfile pod that come from
// the profile's persona and tools images. It is the mount contract between the
// persona format (#3656) and the dispatcher that runs profile pods (#3655):
//
//   - persona.image is mounted read-only at PersonaMountPath;
//   - each spec.tools entry is mounted read-only at ToolsMountRoot/<name>, and
//     its bin/ directory joins PATH, in list order.
//
// An image's payload lives under PayloadDir (/files) inside the image, in both
// mount modes, so one image works on every cluster: with image volumes the
// mount selects that directory with a subPath, and when staged only that
// directory is copied. The rest of the image (a /bin/sh the staging fallback
// needs, say) never reaches the mount, so it can't land on PATH.
//
// The package holds no controller state. Callers pass the profile and whether
// the cluster can mount image volumes, and put the result into whichever pod
// template they render (a warm-pool Deployment or a per-task Job).
package profilepod

import (
	"fmt"
	"path"
	"strings"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	"k8s.io/utils/ptr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	// PersonaMountPath is where the persona image is mounted. The worker's
	// persona adapter reads it from here.
	PersonaMountPath = "/persona"

	// ToolsMountRoot is the parent of every tools image's mount.
	ToolsMountRoot = "/opt/tools"

	// PayloadDir is the directory inside a persona or tools image whose
	// content is mounted. A tools image's binaries go in PayloadDir/bin.
	PayloadDir = "files"

	personaVolumeName = "persona"
	toolVolumePrefix  = "tool-"
	stagePrefix       = "stage-"
)

// stagingResources are the staging init containers' fixed requests and limits.
// A staging run is one streaming cp, so it needs little. Setting them matters
// anyway: a namespace quota on requests.cpu or requests.memory rejects a pod
// with any container that lacks them, and on GKE Autopilot an unset request is
// billed at the default.
var stagingResources = corev1.ResourceRequirements{
	Requests: corev1.ResourceList{
		corev1.ResourceCPU:    resource.MustParse("50m"),
		corev1.ResourceMemory: resource.MustParse("64Mi"),
	},
	Limits: corev1.ResourceList{
		corev1.ResourceCPU:    resource.MustParse("500m"),
		corev1.ResourceMemory: resource.MustParse("128Mi"),
	},
}

// Rendered is what a pod template needs from the profile's persona and tools.
type Rendered struct {
	// Volumes go on the pod.
	Volumes []corev1.Volume
	// Mounts go on the harness container. Every one is read-only.
	Mounts []corev1.VolumeMount
	// InitContainers stage the images into their volumes where image volumes
	// are not available. Empty when they are.
	InitContainers []corev1.Container
	// PathPrefix is the tools' bin directories joined with ':', in list order,
	// for the caller to put in front of the container's PATH. Empty when the
	// profile has no tools.
	PathPrefix string
}

// ToolMountPath is where the named tools image is mounted.
func ToolMountPath(name string) string {
	return path.Join(ToolsMountRoot, name)
}

// RenderPersonaAndTools renders the persona and tools mounts for a profile.
//
// With imageVolumesSupported, each image is an image volume, which the kubelet
// mounts read-only, with PayloadDir as the mount's subPath. Without it (GKE
// Autopilot, or Kubernetes below 1.35) each image gets an emptyDir and an init
// container that copies PayloadDir into it, the way AgentPlugin images are
// staged; such an image must then ship /bin/sh.
func RenderPersonaAndTools(profile *agentv1alpha1.AgentProfile, imageVolumesSupported bool) Rendered {
	var r Rendered
	r.add(personaVolumeName, profile.Spec.Persona.Image, nil, PersonaMountPath, imageVolumesSupported)

	bins := make([]string, 0, len(profile.Spec.Tools))
	for _, tool := range profile.Spec.Tools {
		mountPath := ToolMountPath(tool.Name)
		r.add(toolVolumePrefix+tool.Name, tool.Image, tool.ImagePullPolicy, mountPath, imageVolumesSupported)
		bins = append(bins, path.Join(mountPath, "bin"))
	}
	r.PathPrefix = strings.Join(bins, ":")
	return r
}

func (r *Rendered) add(volume, image string, policy *corev1.PullPolicy, mountPath string, imageVolumesSupported bool) {
	pullPolicy := corev1.PullIfNotPresent
	if policy != nil {
		pullPolicy = *policy
	}
	if imageVolumesSupported {
		r.Volumes = append(r.Volumes, corev1.Volume{
			Name: volume,
			VolumeSource: corev1.VolumeSource{
				Image: &corev1.ImageVolumeSource{Reference: image, PullPolicy: pullPolicy},
			},
		})
	} else {
		r.Volumes = append(r.Volumes, corev1.Volume{
			Name:         volume,
			VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}},
		})
		r.InitContainers = append(r.InitContainers, stagingContainer(volume, image, pullPolicy, mountPath))
	}
	mount := corev1.VolumeMount{Name: volume, MountPath: mountPath, ReadOnly: true}
	if imageVolumesSupported {
		mount.SubPath = PayloadDir
	}
	r.Mounts = append(r.Mounts, mount)
}

// stagingContainer copies an image's PayloadDir into its emptyDir, entry by
// entry. It never copies the directory itself onto the mount: cp -a would then
// re-apply /files's mode and times to the kubelet-owned mount root, which GNU
// cp treats as an error when the container is not root. It fails
// when the image has no PayloadDir or the copy is empty, so a mis-built image
// stops the pod instead of starting a harness with an empty mount. It copies
// nothing else from the image: unlike AgentPlugin's staging there is no
// top-level fallback, which would also have copied the mount into itself for
// /persona.
func stagingContainer(volume, image string, pullPolicy corev1.PullPolicy, mountPath string) corev1.Container {
	script := fmt.Sprintf(`set -e
src=/%s
dst=%q
[ -d "$src" ] || { echo "image has no $src directory; a persona or tools image keeps its payload there" >&2; exit 1; }
for item in "$src"/* "$src"/.[!.]* "$src"/..?*; do
  [ -e "$item" ] || [ -L "$item" ] || continue
  cp -a "$item" "$dst"/
done
[ -n "$(ls -A "$dst")" ] || { echo "$src is empty" >&2; exit 1; }`, PayloadDir, mountPath)
	return corev1.Container{
		Name:            stagePrefix + volume,
		Image:           image,
		ImagePullPolicy: pullPolicy,
		Command:         []string{"/bin/sh", "-c", script},
		Resources:       *stagingResources.DeepCopy(),
		SecurityContext: &corev1.SecurityContext{
			AllowPrivilegeEscalation: ptr.To(false),
			ReadOnlyRootFilesystem:   ptr.To(true),
			Capabilities:             &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}},
		},
		VolumeMounts: []corev1.VolumeMount{{Name: volume, MountPath: mountPath}},
	}
}
