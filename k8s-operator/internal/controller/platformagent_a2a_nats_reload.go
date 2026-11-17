package controller

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"os"
	"strconv"
	"strings"
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
	// a2aBusReloadFoldedAnnotation marks a pending reload that a later
	// config was folded into before it was confirmed. One load-time move
	// can't say which of the two writes it applied, so the move re-arms the
	// wait for the newest write rather than confirm it
	// (platformagent_a2a_profile_hold.go relies on that).
	a2aBusReloadFoldedAnnotation = "kubeagents.x-k8s.io/a2a-reload-folded"
	// a2aBusReloadRearmedAnnotation marks a wait re-armed off a folded
	// reload's move. Writes seconds apart usually reach the pod in one
	// kubelet write, so a re-armed wait often sees no further move, and
	// nothing the server reports can say whether the newest write was in the
	// one it saw. Past a2aBusReloadBound it is restarted onto the newest,
	// as any unconfirmed reload is: confirming by the move's absence would
	// admit a profile whose deny the server might not hold. The mark only
	// makes that restart a Normal Event rather than a failure Warning.
	a2aBusReloadRearmedAnnotation = "kubeagents.x-k8s.io/a2a-reload-rearmed"

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
	// a2aBusConfigReloadFoldedReason is the Normal Event for a restart onto
	// a folded config change (a2aBusReloadRearmedAnnotation).
	a2aBusConfigReloadFoldedReason = "BusConfigReloadFolded"
	a2aBusReloadFailedReason       = "BusConfigReloadFailed"
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
		Resources:       a2aNATSReloaderResources(),
	}
}

// a2aNATSReloaderResources adds an ephemeral-storage request and equal limit,
// for the container's own log, to its CPU and memory. It writes nothing else:
// its root filesystem is read-only and every mount is.
func a2aNATSReloaderResources() corev1.ResourceRequirements {
	resources := a2aResources(a2aNATSReloaderCPURequest, a2aNATSReloaderMemoryRequest, a2aNATSReloaderCPULimit, a2aNATSReloaderMemoryLimit)
	resources.Requests[corev1.ResourceEphemeralStorage] = resource.MustParse(emptyDirLogsAllowance)
	resources.Limits[corev1.ResourceEphemeralStorage] = resource.MustParse(emptyDirLogsAllowance)
	return resources
}

func a2aNATSPIDVolumeSource() corev1.VolumeSource {
	return corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{
		SizeLimit: ptr.To(resource.MustParse(a2aNATSPIDVolumeSizeLimit)),
	}}
}

// a2aBusReload is what one pass decided about reloading the bus in place.
type a2aBusReload struct {
	// restart names the one bus server to restart this pass: past
	// a2aBusReloadBound its reload wasn't confirmed, and every other server
	// is Ready, so the bus keeps quorum while it restarts. Empty restarts
	// nothing.
	restart string
	// restartUID is the pod restart names, as read this pass; the delete is
	// made on it, so a pod recreated since is left alone.
	restartUID types.UID
	// requeue is when to look again while a reload is pending.
	requeue time.Duration
	// reloaded: every server confirmed the pending reload on this pass.
	reloaded bool
	// folded: restart is for a server whose wait was re-armed off a folded
	// reload's move, whose newest write can't be confirmed in place, not one
	// whose reload failed (a2aBusReloadRearmedAnnotation).
	folded bool
	// busUID is the live StatefulSet's, which a restarted pod must belong to.
	busUID types.UID
}

// a2aBusReloadState is a pending reload as it rides the config Secret: when
// the newest config was written (by the operator's clock), and per bus server
// (by pod name) the config_load_time read just before it, empty where it
// couldn't be read; plus which servers' waits fold an earlier write, and which
// were re-armed off a folded move.
type a2aBusReloadState struct {
	requestedAt string
	baselines   map[string]string
	folded      map[string]bool
	rearmed     map[string]bool
}

// readA2ABusReloadState reads the pending reload off annotations, or reports
// none. A record an earlier operator wrote for its one server (a bare load
// time, and "true" for folded and re-armed) reads as that server's.
func readA2ABusReloadState(annotations map[string]string, stsName string) (a2aBusReloadState, bool) {
	requestedAt, pending := annotations[a2aBusReloadRequestedAnnotation]
	if !pending {
		return a2aBusReloadState{}, false
	}
	state := a2aBusReloadState{requestedAt: requestedAt, baselines: map[string]string{}, folded: map[string]bool{}, rearmed: map[string]bool{}}
	raw := annotations[a2aBusReloadBaselineAnnotation]
	if strings.HasPrefix(raw, "{") {
		if err := json.Unmarshal([]byte(raw), &state.baselines); err != nil {
			state.baselines = map[string]string{}
		}
	} else if raw != "" {
		state.baselines[stsName+a2aNATSPodOrdinalSuffix] = raw
	}
	state.folded = decodeA2ABusServerSet(annotations, a2aBusReloadFoldedAnnotation, stsName)
	state.rearmed = decodeA2ABusServerSet(annotations, a2aBusReloadRearmedAnnotation, stsName)
	return state, true
}

// decodeA2ABusServerSet reads a server-name list annotation; an earlier
// operator's "true" names its one server.
func decodeA2ABusServerSet(annotations map[string]string, key, stsName string) map[string]bool {
	value, ok := annotations[key]
	if !ok {
		return map[string]bool{}
	}
	if value == "true" {
		return map[string]bool{stsName + a2aNATSPodOrdinalSuffix: true}
	}
	return decodeA2AConfigProfiles(value)
}

// stamp writes the pending reload onto config's annotations.
func (s a2aBusReloadState) stamp(config *corev1.Secret) {
	if config.Annotations == nil {
		config.Annotations = map[string]string{}
	}
	config.Annotations[a2aBusReloadRequestedAnnotation] = s.requestedAt
	raw, _ := json.Marshal(s.baselines) // a map of strings always marshals
	config.Annotations[a2aBusReloadBaselineAnnotation] = string(raw)
	if len(s.folded) > 0 {
		config.Annotations[a2aBusReloadFoldedAnnotation] = encodeA2AConfigProfiles(setNames(s.folded))
	}
	if len(s.rearmed) > 0 {
		config.Annotations[a2aBusReloadRearmedAnnotation] = encodeA2AConfigProfiles(setNames(s.rearmed))
	}
}

// a2aBusServer is one bus server: its pod name, and the pod if one by that
// name exists and the live StatefulSet controls it. extra marks a pod at an
// ordinal past what the StatefulSet asks for (a scale-down unfinished, or a
// pod left over), which still serves whatever config it started with.
type a2aBusServer struct {
	name  string
	pod   *corev1.Pod
	extra bool
}

// serving reports whether the server's pod exists, is the StatefulSet's and
// isn't terminating.
func (s a2aBusServer) serving() bool { return s.pod != nil && s.pod.DeletionTimestamp == nil }

// a2aBusServers lists the servers the live StatefulSet asks for, by ordinal,
// and after them every pod that exists at a higher ordinal up to the most the
// bus ever runs (a2aNATSMaxReplicas) or the StatefulSet still counts, marked
// extra: during a scale-down (a three to one migration, say) those pods still
// serve, and nothing confirms what they loaded.
func (r *PlatformAgentReconciler) a2aBusServers(ctx context.Context, sts *appsv1.StatefulSet) ([]a2aBusServer, error) {
	replicas := 1
	if sts.Spec.Replicas != nil {
		replicas = int(*sts.Spec.Replicas)
	}
	ordinals := max(replicas, int(sts.Status.Replicas), a2aNATSMaxReplicas)
	out := make([]a2aBusServer, 0, replicas)
	for i := range ordinals {
		name := sts.Name + "-" + strconv.Itoa(i)
		pod := &corev1.Pod{}
		err := r.podReader().Get(ctx, types.NamespacedName{Name: name, Namespace: sts.Namespace}, pod)
		switch {
		case errors.IsNotFound(err):
			if i >= replicas {
				continue
			}
			pod = nil
		case err != nil:
			return nil, err
		default:
			// A pod by that name the StatefulSet doesn't control runs a
			// config nothing here records.
			if owner := metav1.GetControllerOf(pod); owner == nil || owner.UID != sts.UID {
				pod = nil
			}
		}
		out = append(out, a2aBusServer{name: name, pod: pod, extra: i >= replicas})
	}
	return out, nil
}

// a2aBusStartedAfter is the margin by which a pod must postdate a reload's
// request to count as started on its config: the config is applied in the
// pass that stamps the request, after the stamp, and the two clocks (the
// operator's and the API server's) are not the same.
const a2aBusStartedAfter = 10 * time.Second

// startedAfter reports whether the pod was created after requestedAt by
// a2aBusStartedAfter, so it started on the config that request wrote. A
// request time that doesn't parse dates no pod after it.
func startedAfter(pod *corev1.Pod, requestedAt string) bool {
	since, err := time.Parse(time.RFC3339Nano, requestedAt)
	return err == nil && pod.CreationTimestamp.After(since.Add(a2aBusStartedAfter))
}

// planA2ABusReload decides whether the config about to be applied reaches the
// bus by a reload rather than a roll, and stamps the pending reload onto
// config's annotations. The pod template's hash leaves out what a reload can
// apply (a2aConfigRolloutHash), so a config change with an unchanged hash is
// one only a reload would deliver. With a roll coming, or no bus yet, there
// is nothing to track. One server and a cluster take this one path.
//
// A reload is confirmed server by server, by each server's config_load_time
// moving off the value read from that server just before the new config was
// written; a server whose pod started after the write (a2aBusStartedAfter)
// runs it from the start. It fails closed: a server that can't be read, has
// no pod, or recorded no baseline hasn't loaded the config, so the pending
// reload stays, and the profile hold with it (a2aServerLoadedFrom), until
// every server confirms. Past a2aBusReloadBound the servers that haven't are
// restarted, one per pass and only while every other server is Ready, so a
// cluster keeps quorum; a pod started after the write is never restarted for
// it, since a restart would bring back the same config. The Secret and the
// StatefulSet are read uncached (a2aReader).
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
	servers, err := r.a2aBusServers(ctx, liveSts)
	if err != nil {
		return a2aBusReload{}, err
	}
	prior, pending := readA2ABusReloadState(live.Annotations, liveSts.Name)
	if string(live.Data[a2aNATSConfKey]) != string(config.Data[a2aNATSConfKey]) {
		// A new config for the running bus. A reload still pending from an
		// earlier change is folded in: the reloader applies whatever file
		// it finds, so the newest write is the one to wait for.
		state := a2aBusReloadState{requestedAt: now.UTC().Format(time.RFC3339Nano), baselines: map[string]string{}, folded: map[string]bool{}, rearmed: map[string]bool{}}
		for _, s := range servers {
			baseline := ""
			if s.serving() {
				if loaded, err := r.a2aBusConfigLoadTime(ctx, agent, s.pod); err == nil {
					baseline = loaded
				} else {
					logf.FromContext(ctx).Info("couldn't read an A2A bus server's config load time before a reload; it will be restarted if the reload can't be confirmed",
						"pod", s.name, "error", err.Error())
				}
			}
			state.baselines[s.name] = baseline
			// Folded only while the earlier write is still unloaded on this
			// server: a load time already off its pending baseline is that
			// write landing before the recheck saw it, and the server then
			// owes one reload, this one, which its next move confirms. A
			// load time that couldn't be read, now or then, can't tell, and
			// folds. A server that started after the earlier write had it
			// from the start.
			if pending {
				if s.pod != nil && startedAfter(s.pod, prior.requestedAt) {
					continue
				}
				if p := prior.baselines[s.name]; baseline == "" || p == "" || baseline == p {
					state.folded[s.name] = true
				}
			}
		}
		state.stamp(config)
		return a2aBusReload{requeue: a2aBusReloadRecheck, busUID: busUID}, nil
	}
	if !pending {
		return a2aBusReload{}, nil
	}
	state := prior
	confirmed := map[string]bool{}
	readable := map[string]bool{}
	for _, s := range servers {
		if !s.serving() || s.extra {
			// An extra pod is on its way out with whatever it loaded: the
			// reload isn't confirmed while it serves.
			continue
		}
		loaded, err := r.a2aBusConfigLoadTime(ctx, agent, s.pod)
		if err != nil {
			continue
		}
		readable[s.name] = true
		if startedAfter(s.pod, state.requestedAt) {
			confirmed[s.name] = true
			continue
		}
		baseline := state.baselines[s.name]
		if baseline == "" || loaded == baseline {
			continue
		}
		if state.folded[s.name] {
			// The move may be the earlier write landing: wait for one off
			// this load time, or restart past the bound, for the newest.
			state.baselines[s.name] = loaded
			delete(state.folded, s.name)
			state.rearmed[s.name] = true
			continue
		}
		confirmed[s.name] = true
	}
	if len(servers) > 0 && len(confirmed) == len(servers) {
		return a2aBusReload{reloaded: true, busUID: busUID}, nil
	}
	state.stamp(config)
	since, perr := time.Parse(time.RFC3339Nano, state.requestedAt)
	if perr == nil && now.Sub(since) < a2aBusReloadBound {
		return a2aBusReload{requeue: min(a2aBusReloadRecheck, a2aBusReloadBound-now.Sub(since)), busUID: busUID}, nil
	}
	plan := a2aBusReload{requeue: a2aBusReloadRecheck, busUID: busUID}
	if straggler, ok := a2aBusStragglerToRestart(servers, state, confirmed); ok {
		plan.restart, plan.restartUID = straggler.name, straggler.pod.UID
		// A re-armed fold the server still answers for is the expected
		// shape, not a failed reload; it restarts all the same.
		plan.folded = state.rearmed[straggler.name] && readable[straggler.name] && perr == nil
	}
	return plan, nil
}

// a2aBusStragglerToRestart picks the one server to restart past the bound:
// the first, by ordinal, that hasn't confirmed the reload and whose pod
// predates the write, and only when every other server's pod is serving and
// Ready. None qualifies while any other server is down or still starting.
func a2aBusStragglerToRestart(servers []a2aBusServer, state a2aBusReloadState, confirmed map[string]bool) (a2aBusServer, bool) {
	for _, s := range servers {
		if s.extra {
			// A scale-down is under way: the StatefulSet removes that pod
			// itself, and a restart now would only cost quorum.
			return a2aBusServer{}, false
		}
	}
	for i, s := range servers {
		if confirmed[s.name] || !s.serving() || startedAfter(s.pod, state.requestedAt) {
			continue
		}
		others := true
		for j, o := range servers {
			if j != i && (!o.serving() || !podIsReady(o.pod)) {
				others = false
				break
			}
		}
		if !others {
			return a2aBusServer{}, false
		}
		return s, true
	}
	return a2aBusServer{}, false
}

// actOnA2ABusReload acts on the plan before the config is applied: an Event
// for a reload every server confirmed, or one straggling server deleted, with
// a Warning, for a reload it didn't confirm in time. The live config is
// already the desired one on that path, and the StatefulSet recreates the pod
// on it; the pending record stays until that pod is read on it too. If the
// delete fails, the pass returns before the apply. The delete names the pod's
// UID, and only a pod the live StatefulSet controls is deleted.
func (r *PlatformAgentReconciler) actOnA2ABusReload(ctx context.Context, agent *agentv1alpha1.PlatformAgent, stsName string, plan a2aBusReload) error {
	if plan.reloaded {
		r.recordEvent(agent, corev1.EventTypeNormal, a2aBusConfigReloadedReason,
			"Reloaded the A2A bus's config in place on every server.")
		return nil
	}
	if plan.restart == "" {
		return nil
	}
	pod := &corev1.Pod{}
	if err := r.podReader().Get(ctx, types.NamespacedName{Name: plan.restart, Namespace: agent.Namespace}, pod); err != nil {
		return client.IgnoreNotFound(err)
	}
	if owner := metav1.GetControllerOf(pod); owner == nil || owner.UID != plan.busUID || pod.UID != plan.restartUID || pod.DeletionTimestamp != nil {
		return nil
	}
	uid := pod.UID
	if err := r.Delete(ctx, pod, client.Preconditions{UID: &uid}); err != nil {
		if errors.IsNotFound(err) || errors.IsConflict(err) {
			return nil
		}
		return fmt.Errorf("failed to restart A2A bus pod %s after a failed reload: %w", pod.Name, err)
	}
	if plan.folded {
		logf.FromContext(ctx).Info("restarted an A2A bus pod onto a folded config change", "pod", pod.Name, "bound", a2aBusReloadBound)
		r.recordEvent(agent, corev1.EventTypeNormal, a2aBusConfigReloadFoldedReason,
			fmt.Sprintf("Restarted A2A bus pod %s: two config changes reached it together, and the newer one can't be confirmed in place, so it restarts onto it.",
				pod.Name))
		return nil
	}
	logf.FromContext(ctx).Info("restarted an A2A bus pod: its config reload wasn't confirmed in time", "pod", pod.Name, "bound", a2aBusReloadBound)
	r.recordEvent(agent, corev1.EventTypeWarning, a2aBusReloadFailedReason,
		fmt.Sprintf("Restarted A2A bus pod %s: its config reload wasn't confirmed within %s, so it restarts onto the new config instead.",
			pod.Name, a2aBusReloadBound))
	return nil
}

// a2aBusConfigLoadTime reads config_load_time from one bus server's
// monitoring endpoint, by its pod IP, as the server reports it. A Service
// answers from whichever server it picks, and the per-pod names under the
// routes Service don't resolve (the StatefulSet's service is the client
// Service), so the pod's own address is the only way to ask a given server.
// It's compared as a string, so the operator's clock never meets the bus's.
func (r *PlatformAgentReconciler) a2aBusConfigLoadTime(ctx context.Context, agent *agentv1alpha1.PlatformAgent, pod *corev1.Pod) (string, error) {
	if r.busConfigLoadTime != nil {
		return r.busConfigLoadTime(ctx, agent, pod)
	}
	if pod.Status.PodIP == "" {
		return "", fmt.Errorf("A2A bus pod %s has no IP yet", pod.Name)
	}
	ctx, cancel := context.WithTimeout(ctx, a2aBusVarzTimeout)
	defer cancel()
	url := "http://" + net.JoinHostPort(pod.Status.PodIP, strconv.Itoa(a2aNATSMonitorPort)) + a2aBusVarzPath
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
		return "", fmt.Errorf("%s (%s) answered %s", url, pod.Name, resp.Status)
	}
	var varz struct {
		ConfigLoadTime string `json:"config_load_time"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&varz); err != nil {
		return "", fmt.Errorf("decoding %s (%s): %w", url, pod.Name, err)
	}
	if varz.ConfigLoadTime == "" {
		return "", fmt.Errorf("%s (%s) carries no config_load_time", url, pod.Name)
	}
	return varz.ConfigLoadTime, nil
}
