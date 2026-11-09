package controller

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	// The bus pod's config reloader, from the same publisher as nats-server.
	// It watches the mounted nats.conf and sends nats-server the reload
	// signal when the kubelet writes a new one. Pinned by digest; images.json
	// carries it as `nats-server-config-reloader`, and
	// hack/check-image-inventory.sh holds this constant to it.
	a2aNATSReloaderImageEnvVar  = "A2A_NATS_RELOADER_IMAGE"
	defaultA2ANATSReloaderImage = "natsio/nats-server-config-reloader:0.25.0@sha256:24bacde6b844f8875f93424ed153d33b3d9e9367fcccc14b683eb71fab3a4d8c"
	a2aNATSReloaderContainer    = "reloader"

	// nats-server writes its PID here (pid_file in nats.conf) and the
	// reloader reads it, so the two share this emptyDir. The pod shares one
	// PID namespace, so the PID the reloader reads is one it can signal.
	a2aNATSPIDVolume    = "nats-pid"
	a2aNATSPIDMountDir  = "/var/run/nats"
	a2aNATSPIDFile      = a2aNATSPIDMountDir + "/nats.pid"
	a2aNATSConfMountDir = "/etc/nats"
	a2aNATSConfKey      = "nats.conf"
	a2aNATSConfFile     = a2aNATSConfMountDir + "/" + a2aNATSConfKey

	a2aNATSReloaderCPURequest    = "10m"
	a2aNATSReloaderMemoryRequest = "16Mi"
	a2aNATSReloaderCPULimit      = "50m"
	a2aNATSReloaderMemoryLimit   = "32Mi"
	a2aNATSPIDVolumeSizeLimit    = "1Mi"

	// A reload the operator asked for, recorded on the config Secret: when
	// it wrote the new config (by its own clock), and the bus's
	// config_load_time just before. A config_load_time that moves off the
	// recorded one is the reload landing.
	a2aBusReloadRequestedAnnotation = "kubeagents.x-k8s.io/a2a-reload-requested-at"
	a2aBusReloadBaselineAnnotation  = "kubeagents.x-k8s.io/a2a-reload-baseline"

	// How long a reload may take before the operator restarts the bus pod
	// instead. The kubelet writes an updated Secret into a running pod
	// within its sync period plus its cache TTL, about a minute by default,
	// and the reloader signals within seconds of the write.
	a2aBusReloadBound = 3 * time.Minute
	// How often a pending reload is checked.
	a2aBusReloadRecheck = 15 * time.Second
	// The bus's monitoring endpoint answers on the pod network to the
	// operator alone (buildA2ANATSNetworkPolicy).
	a2aBusVarzPath    = "/varz"
	a2aBusVarzTimeout = 3 * time.Second

	a2aBusConfigReloadedReason = "BusConfigReloaded"
	a2aBusReloadFailedReason   = "BusConfigReloadFailed"
)

// a2aBusVarzClient reads the bus's monitoring endpoint directly: an
// HTTP(S)_PROXY in the operator's environment is for its egress, and a
// Service name sent through it would never be answered.
var a2aBusVarzClient = &http.Client{Transport: &http.Transport{Proxy: nil}}

func a2aNATSReloaderImage() string {
	if override := os.Getenv(a2aNATSReloaderImageEnvVar); override != "" {
		return override
	}
	return defaultA2ANATSReloaderImage
}

// buildA2ANATSReloaderContainer is the bus pod's reloader. It mounts the
// config, the serving pair and the PID file, all read-only: no service
// account token (the pod mounts none), a read-only root filesystem, and no
// capabilities. The serving pair is there because the reloader refuses to
// start unless every cert_file, key_file and ca_file the config names exists,
// and it watches those too. It runs as the pod's user, the same as
// nats-server's, which is what lets it signal that process without CAP_KILL.
// Sharing that user and one PID namespace also means it could read what
// nats-server holds through /proc, so the mounts don't narrow it further than
// the pod already is.
func buildA2ANATSReloaderContainer() corev1.Container {
	return corev1.Container{
		Name:  a2aNATSReloaderContainer,
		Image: a2aNATSReloaderImage(),
		Args:  []string{"-pid", a2aNATSPIDFile, "-config", a2aNATSConfFile},
		VolumeMounts: []corev1.VolumeMount{
			{Name: "config", MountPath: a2aNATSConfMountDir, ReadOnly: true},
			{Name: a2aNATSTLSVolume, MountPath: a2aNATSTLSMountDir, ReadOnly: true},
			{Name: a2aNATSPIDVolume, MountPath: a2aNATSPIDMountDir, ReadOnly: true},
		},
		SecurityContext: hardenedSecurityContext(),
		Resources:       a2aResources(a2aNATSReloaderCPURequest, a2aNATSReloaderMemoryRequest, a2aNATSReloaderCPULimit, a2aNATSReloaderMemoryLimit),
	}
}

func a2aNATSPIDVolumeSource() corev1.VolumeSource {
	return corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{
		SizeLimit: ptr.To(resource.MustParse(a2aNATSPIDVolumeSizeLimit)),
	}}
}

// a2aBusReload is what one pass decided about reloading the bus in place.
type a2aBusReload struct {
	// restartPod: the reload didn't land within a2aBusReloadBound, so the
	// bus pod is to be restarted onto the new config.
	restartPod bool
	// requeue is when to look again while a reload is pending.
	requeue time.Duration
	// reloaded: the pending reload landed on this pass.
	reloaded bool
	// busUID is the live StatefulSet's, which a restarted pod must belong to.
	busUID types.UID
}

// planA2ABusReload decides whether the config about to be applied reaches the
// bus by a reload rather than a roll, and stamps the pending reload onto
// config's annotations. The pod template's hash leaves out what a reload can
// apply (a2aConfigRolloutHash with no profiles), so a config change with an
// unchanged hash is one only a reload would deliver. With a roll coming, or
// no bus yet, there is nothing to track.
//
// A reload is confirmed by the bus's config_load_time moving off the value
// read just before the new config was written. Until it moves, the pending
// reload rides the Secret's annotations from pass to pass. Past
// a2aBusReloadBound it's given up on and the bus pod is restarted, which is
// also what happens when the monitoring endpoint can't be read at all. The
// Secret and the StatefulSet are read uncached (a2aReader).
func (r *PlatformAgentReconciler) planA2ABusReload(ctx context.Context, agent *agentv1alpha1.PlatformAgent, config *corev1.Secret, sts *appsv1.StatefulSet, now time.Time) (a2aBusReload, error) {
	live := &corev1.Secret{}
	if err := r.a2aReader().Get(ctx, types.NamespacedName{Name: config.Name, Namespace: config.Namespace}, live); err != nil {
		return a2aBusReload{}, client.IgnoreNotFound(err)
	}
	liveSts := &appsv1.StatefulSet{}
	if err := r.a2aReader().Get(ctx, types.NamespacedName{Name: sts.Name, Namespace: sts.Namespace}, liveSts); err != nil {
		return a2aBusReload{}, client.IgnoreNotFound(err)
	}
	if liveSts.Spec.Template.Annotations[a2aConfigHashAnnotation] != sts.Spec.Template.Annotations[a2aConfigHashAnnotation] {
		return a2aBusReload{}, nil
	}
	busUID := liveSts.UID
	if string(live.Data[a2aNATSConfKey]) != string(config.Data[a2aNATSConfKey]) {
		// A new config for the running bus. A reload still pending from an
		// earlier change is folded in: the reloader applies whatever file
		// it finds, so the newest write is the one to wait for.
		baseline, err := r.a2aBusConfigLoadTime(ctx, agent)
		if err != nil {
			logf.FromContext(ctx).Info("couldn't read the A2A bus's config load time before a reload; it will be restarted if the reload can't be confirmed",
				"error", err.Error())
			baseline = ""
		}
		stampA2ABusReload(config, now.UTC().Format(time.RFC3339Nano), baseline)
		return a2aBusReload{requeue: a2aBusReloadRecheck, busUID: busUID}, nil
	}
	requestedAt, pending := live.Annotations[a2aBusReloadRequestedAnnotation]
	if !pending {
		return a2aBusReload{}, nil
	}
	baseline := live.Annotations[a2aBusReloadBaselineAnnotation]
	loaded, err := r.a2aBusConfigLoadTime(ctx, agent)
	// A moved load time is taken as this config landing. When a second
	// change was folded into a pending one before the first reached the
	// pod, the first reload landing can clear both; the reloader still
	// applies the second when the kubelet writes it.
	if err == nil && baseline != "" && loaded != baseline {
		return a2aBusReload{reloaded: true, busUID: busUID}, nil
	}
	since, perr := time.Parse(time.RFC3339Nano, requestedAt)
	if perr != nil || now.Sub(since) >= a2aBusReloadBound {
		return a2aBusReload{restartPod: true, busUID: busUID}, nil
	}
	stampA2ABusReload(config, requestedAt, baseline)
	return a2aBusReload{requeue: min(a2aBusReloadRecheck, a2aBusReloadBound-now.Sub(since)), busUID: busUID}, nil
}

func stampA2ABusReload(config *corev1.Secret, requestedAt, baseline string) {
	if config.Annotations == nil {
		config.Annotations = map[string]string{}
	}
	config.Annotations[a2aBusReloadRequestedAnnotation] = requestedAt
	config.Annotations[a2aBusReloadBaselineAnnotation] = baseline
}

// actOnA2ABusReload acts on the plan before the config is applied, so the
// pending record the apply drops is never gone while its action is still
// owed: an Event for a reload that landed, or the bus pod deleted, with a
// Warning, for one that didn't. The live config is already the desired one on
// that path, and the StatefulSet recreates the pod on it. If the delete
// fails, the pass returns before the apply and the record stays for the
// next. The delete names the pod's UID, and only a pod the live StatefulSet
// controls is deleted.
func (r *PlatformAgentReconciler) actOnA2ABusReload(ctx context.Context, agent *agentv1alpha1.PlatformAgent, stsName string, plan a2aBusReload) error {
	if plan.reloaded {
		r.recordEvent(agent, corev1.EventTypeNormal, a2aBusConfigReloadedReason,
			"Reloaded the A2A bus's config in place.")
		return nil
	}
	if !plan.restartPod {
		return nil
	}
	pod := &corev1.Pod{}
	if err := r.podReader().Get(ctx, types.NamespacedName{Name: stsName + a2aNATSPodOrdinalSuffix, Namespace: agent.Namespace}, pod); err != nil {
		return client.IgnoreNotFound(err)
	}
	if owner := metav1.GetControllerOf(pod); owner == nil || owner.UID != plan.busUID || pod.DeletionTimestamp != nil {
		return nil
	}
	uid := pod.UID
	if err := r.Delete(ctx, pod, client.Preconditions{UID: &uid}); err != nil {
		if errors.IsNotFound(err) || errors.IsConflict(err) {
			return nil
		}
		return fmt.Errorf("failed to restart A2A bus pod %s after a failed reload: %w", pod.Name, err)
	}
	logf.FromContext(ctx).Info("restarted the A2A bus pod: its config reload wasn't confirmed in time", "pod", pod.Name, "bound", a2aBusReloadBound)
	r.recordEvent(agent, corev1.EventTypeWarning, a2aBusReloadFailedReason,
		fmt.Sprintf("Restarted A2A bus pod %s: its config reload wasn't confirmed within %s, so it restarts onto the new config instead.",
			pod.Name, a2aBusReloadBound))
	return nil
}

// a2aBusConfigLoadTime reads config_load_time from the bus's monitoring
// endpoint, as the server reports it. It's compared as a string, so the
// operator's clock never meets the bus's.
func (r *PlatformAgentReconciler) a2aBusConfigLoadTime(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (string, error) {
	if r.busConfigLoadTime != nil {
		return r.busConfigLoadTime(ctx, agent)
	}
	ctx, cancel := context.WithTimeout(ctx, a2aBusVarzTimeout)
	defer cancel()
	url := fmt.Sprintf("http://%s.%s.svc:%d%s", a2aNATSName(agent), agent.Namespace, a2aNATSMonitorPort, a2aBusVarzPath)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return "", err
	}
	resp, err := a2aBusVarzClient.Do(req)
	if err != nil {
		return "", err
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("%s answered %s", url, resp.Status)
	}
	var varz struct {
		ConfigLoadTime string `json:"config_load_time"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&varz); err != nil {
		return "", fmt.Errorf("decoding %s: %w", url, err)
	}
	if varz.ConfigLoadTime == "" {
		return "", fmt.Errorf("%s carries no config_load_time", url)
	}
	return varz.ConfigLoadTime, nil
}
