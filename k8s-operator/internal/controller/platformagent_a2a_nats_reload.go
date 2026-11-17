package controller

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"os"
	"strconv"
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
	// a2aBusReloadBaselineAnnotation keeps the shape an earlier operator
	// reads: pod -0's config_load_time read just before the write, as a bare
	// string, empty when it couldn't be read. Rolled back mid-reload, that
	// operator then confirms or restarts the one server as it always did,
	// rather than mistaking another shape for a moved load time.
	a2aBusReloadBaselineAnnotation = "kubeagents.x-k8s.io/a2a-reload-baseline"
	// a2aBusReloadServersAnnotation is the per-server record a reload is
	// confirmed against (a2aBusServerRecord, by pod name, as JSON).
	a2aBusReloadServersAnnotation = "kubeagents.x-k8s.io/a2a-reload-servers"
	// a2aBusReloadWatchedAnnotation records the bus's TLS serving Secret's
	// resourceVersion as the pending reload last saw it. The reloader acts
	// on any file the server's config names, so a renewed certificate synced
	// into the pod reloads the server, moving its config_load_time, possibly
	// before the kubelet syncs the new nats.conf. A change to it while a
	// reload is pending folds every server: its next move re-arms the wait
	// rather than confirms it. A reload an admin sends by hand can't be told
	// from this one at all; that is left to whoever sends it.
	a2aBusReloadWatchedAnnotation = "kubeagents.x-k8s.io/a2a-reload-watched"
	// a2aBusReloadTLSSeenAnnotation is when (by the operator's clock) the
	// pending reload first saw a TLS Secret change that the kubelet may still
	// be syncing into the pods: a change seen mid-reload, or a write within
	// a2aBusKubeletSyncWindow before the config write, whose resourceVersion
	// the record already holds but whose file can land after the new
	// nats.conf is written. Every server stays folded until the window has
	// passed since then.
	a2aBusReloadTLSSeenAnnotation = "kubeagents.x-k8s.io/a2a-reload-tls-seen"
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
	// a2aBusKubeletSyncWindow is how long a projected Secret file in a pod
	// can lag the Secret object: the kubelet's sync period (syncFrequency, a
	// minute by default) plus the delay of its configMapAndSecretChange-
	// DetectionStrategy (Watch by default, whose cache can serve a value up
	// to its TTL old). A server that started more than this after the config
	// was written read the new file at start.
	a2aBusKubeletSyncWindow = 90 * time.Second
	// a2aBusReloadAfterStart is how far past a server's start a load time
	// must be to be a reload rather than the load at start, which nats-server
	// stamps in the same moment as start.
	a2aBusReloadAfterStart = 2 * time.Second
	// a2aBusReadyBeforeRestart is how long every other server must have been
	// Ready before a straggler is restarted, so a server that has only just
	// come back (and may still be catching up its streams) isn't counted on
	// for quorum.
	a2aBusReadyBeforeRestart = 30 * time.Second
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
	// a2aBusForeignPodReason is the Warning for a pod under a bus server's
	// name that the StatefulSet doesn't control: what it loaded is unknown,
	// so the reload can't be confirmed while it serves.
	a2aBusForeignPodReason = "BusPodNotControlled"
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
	// has been Ready for a2aBusReadyBeforeRestart, so the bus keeps quorum
	// while it restarts. Empty restarts nothing.
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
	// foreign names pods under a bus server's name that the StatefulSet
	// doesn't control, each of which holds the reload unconfirmed.
	foreign []string
	// busUID is the live StatefulSet's, which a restarted pod must belong to.
	busUID types.UID
}

// a2aBusServerRecord is what was read from one server just before a config
// write: its pod's UID, and from /varz its start and config_load_time. Empty
// Start and Loaded mean it couldn't be read.
type a2aBusServerRecord struct {
	UID    types.UID `json:"uid"`
	Start  string    `json:"start,omitempty"`
	Loaded string    `json:"loaded,omitempty"`
}

// a2aBusReloadState is a pending reload as it rides the config Secret: when
// the newest config was written (by the operator's clock, for the bound
// only), a record per bus server (by pod name), and which servers' waits fold
// an earlier write, or were re-armed off a folded move.
type a2aBusReloadState struct {
	requestedAt string
	watched     string
	tlsSeen     string
	servers     map[string]a2aBusServerRecord
	folded      map[string]bool
	rearmed     map[string]bool
}

// readA2ABusReloadState reads the pending reload off annotations, or reports
// none. A record an earlier operator wrote (a bare load time, no per-server
// record) reads as pod -0's with no UID, which confirms only a pod created
// after the write.
func readA2ABusReloadState(annotations map[string]string, stsName string) (a2aBusReloadState, bool) {
	requestedAt, pending := annotations[a2aBusReloadRequestedAnnotation]
	if !pending {
		return a2aBusReloadState{}, false
	}
	state := a2aBusReloadState{requestedAt: requestedAt, watched: annotations[a2aBusReloadWatchedAnnotation], tlsSeen: annotations[a2aBusReloadTLSSeenAnnotation], servers: map[string]a2aBusServerRecord{}}
	if raw, ok := annotations[a2aBusReloadServersAnnotation]; ok {
		if err := json.Unmarshal([]byte(raw), &state.servers); err != nil {
			state.servers = map[string]a2aBusServerRecord{}
		}
	} else if raw := annotations[a2aBusReloadBaselineAnnotation]; raw != "" {
		state.servers[stsName+a2aNATSPodOrdinalSuffix] = a2aBusServerRecord{Loaded: raw}
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

// stamp writes the pending reload onto config's annotations: the per-server
// record under its own key, and pod -0's load time alone under the key an
// earlier operator reads (a2aBusReloadBaselineAnnotation).
func (s a2aBusReloadState) stamp(config *corev1.Secret, stsName string) {
	if config.Annotations == nil {
		config.Annotations = map[string]string{}
	}
	config.Annotations[a2aBusReloadRequestedAnnotation] = s.requestedAt
	config.Annotations[a2aBusReloadBaselineAnnotation] = s.servers[stsName+a2aNATSPodOrdinalSuffix].Loaded
	raw, _ := json.Marshal(s.servers) // a map of plain structs always marshals
	config.Annotations[a2aBusReloadServersAnnotation] = string(raw)
	if s.watched != "" {
		config.Annotations[a2aBusReloadWatchedAnnotation] = s.watched
	}
	if s.tlsSeen != "" {
		config.Annotations[a2aBusReloadTLSSeenAnnotation] = s.tlsSeen
	}
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
// pod left over), which still serves whatever config it started with;
// foreign, a pod by that name the StatefulSet doesn't control.
type a2aBusServer struct {
	name    string
	pod     *corev1.Pod
	extra   bool
	foreign bool
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
		foreign := false
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
				pod, foreign = nil, true
			}
		}
		out = append(out, a2aBusServer{name: name, pod: pod, extra: i >= replicas, foreign: foreign})
	}
	return out, nil
}

// a2aBusConfigWrittenAt is when the operator last wrote the live config
// Secret, by the API server's clock: its own field manager's managedFields
// entry (fieldOwner), not another manager's (a later kubectl annotate, say,
// would otherwise move it). The operator's own later writes (a re-armed
// record) can only move it later than the config write a pending reload
// waits on, which is the safe side: a pod created after it read that config
// at start. No entry of the operator's, no time, and no pod is taken as
// started on the config.
func a2aBusConfigWrittenAt(live *corev1.Secret) (time.Time, bool) {
	var latest time.Time
	for _, f := range live.ManagedFields {
		if f.Manager == fieldOwner && f.Time != nil && f.Time.After(latest) {
			latest = f.Time.Time
		}
	}
	return latest, !latest.IsZero()
}

// a2aBusCreatedAfter reports whether pod was created after writtenAt, both by
// the API server's clock. Both carry whole seconds, so the same second is not
// after.
func a2aBusCreatedAfter(pod *corev1.Pod, writtenAt time.Time, known bool) bool {
	return known && pod.CreationTimestamp.Truncate(time.Second).After(writtenAt.Truncate(time.Second))
}

// a2aBusConfirm is one server's verdict on a pending reload.
type a2aBusConfirm int

const (
	a2aBusNotYet a2aBusConfirm = iota
	a2aBusConfirmed
	// a2aBusRearm: a folded server's move, which may be the earlier write
	// landing; its record moves to what was just read.
	a2aBusRearm
)

// a2aBusServerConfirms judges one readable server against its record. It
// fails closed: anything it can't show loaded the newest write is NotYet,
// and only a restart past the bound moves such a server on.
//
//   - The same pod, still on the start recorded: confirmed when its
//     config_load_time has moved (a reload).
//   - The same pod restarted in place (a new start): the start reloads the
//     file the kubelet last synced, which can be the old one, so a moved load
//     time proves nothing. Confirmed when the start came more than
//     a2aBusKubeletSyncWindow after the write (the file it read was synced),
//     or when a load time later than the start arrives (the reloader acting on
//     a later sync).
//   - A pod the record doesn't know (recreated, a new UID, or a record from
//     an earlier operator): a fresh pod mounts the Secret as it is at start,
//     so it is confirmed when created after the write by the API server's
//     clock (writtenAt), the operator's own restarts included.
//
// The shipped single-server path took any moved load time as the reload,
// restart included; this closes that on one server as on three.
func a2aBusServerConfirms(rec a2aBusServerRecord, recorded bool, pod *corev1.Pod, varz a2aBusVarz, writtenAt time.Time, known, folded bool) a2aBusConfirm {
	if !recorded || rec.UID == "" || rec.UID != pod.UID {
		if a2aBusCreatedAfter(pod, writtenAt, known) {
			return a2aBusConfirmed
		}
		return a2aBusNotYet
	}
	if rec.Start == "" {
		// Not read before the write: nothing in place can say which
		// write a load time is.
		return a2aBusNotYet
	}
	verdict := a2aBusNotYet
	if varz.Start == rec.Start {
		if varz.Loaded != rec.Loaded {
			verdict = a2aBusConfirmed
		}
	} else {
		start, serr := time.Parse(time.RFC3339Nano, varz.Start)
		loaded, lerr := time.Parse(time.RFC3339Nano, varz.Loaded)
		switch {
		case serr != nil:
		case known && start.After(writtenAt.Add(a2aBusKubeletSyncWindow)):
			return a2aBusConfirmed
		case lerr == nil && loaded.After(start.Add(a2aBusReloadAfterStart)):
			verdict = a2aBusConfirmed
		}
	}
	if verdict == a2aBusConfirmed && folded {
		return a2aBusRearm
	}
	return verdict
}

// planA2ABusReload decides whether the config about to be applied reaches the
// bus by a reload rather than a roll, and stamps the pending reload onto
// config's annotations. The pod template's hash leaves out what a reload can
// apply (a2aConfigRolloutHash), so a config change with an unchanged hash is
// one only a reload would deliver. With a roll coming, or no bus yet, there
// is nothing to track. One server and a cluster take this one path.
//
// A reload is confirmed server by server (a2aBusServerConfirms), against a
// record read from each server just before the write: its pod UID, start
// and config_load_time, by pod IP. It fails closed: a server that can't be
// read, answers under another name, has no pod, or can't be shown to hold
// the newest write hasn't loaded it, so the pending reload stays, and the
// profile hold with it (a2aServerLoadedFrom), until every server confirms. A
// pod at an ordinal past what the StatefulSet asks for, or one it doesn't
// control, keeps it pending too. Past a2aBusReloadBound the servers that
// haven't confirmed are restarted, one per pass and only once every other
// server has been Ready for a2aBusReadyBeforeRestart, so a cluster keeps
// quorum; the fresh pod mounts the current Secret, and is confirmed by its
// creation time. The Secret and the StatefulSet are read uncached
// (a2aReader).
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
		watched, tlsAt, tlsKnown, err := r.a2aBusWatched(ctx, agent)
		if err != nil {
			return a2aBusReload{}, err
		}
		state := a2aBusReloadState{requestedAt: now.UTC().Format(time.RFC3339Nano), watched: watched, servers: map[string]a2aBusServerRecord{}, folded: map[string]bool{}, rearmed: map[string]bool{}}
		// A TLS write just before this config write may still be syncing
		// into the pods, so a reload it causes can follow this write.
		if tlsKnown && now.Sub(tlsAt) < a2aBusKubeletSyncWindow {
			state.tlsSeen = state.requestedAt
		}
		for _, s := range servers {
			if !s.serving() {
				continue
			}
			rec := a2aBusServerRecord{UID: s.pod.UID}
			if varz, err := r.a2aBusVarz(ctx, agent, s.pod); err == nil {
				rec.Start, rec.Loaded = varz.Start, varz.Loaded
			} else {
				logf.FromContext(ctx).Info("couldn't read an A2A bus server's config load time before a reload; it will be restarted if the reload can't be confirmed",
					"pod", s.name, "error", err.Error())
			}
			state.servers[s.name] = rec
			if state.tlsSeen != "" {
				state.folded[s.name] = true
			}
			// Folded only while the earlier write is still unloaded on this
			// server: a load time already off its pending record on the same
			// start is that write landing before the recheck saw it, and the
			// server then owes one reload, this one, which its next move
			// confirms. Anything else can't tell, and folds.
			if pending {
				p, ok := prior.servers[s.name]
				if !ok || p.UID != rec.UID || p.Start == "" || rec.Start != p.Start || rec.Loaded == p.Loaded {
					state.folded[s.name] = true
				}
			}
		}
		state.stamp(config, liveSts.Name)
		return a2aBusReload{requeue: a2aBusReloadRecheck, busUID: busUID}, nil
	}
	if !pending {
		return a2aBusReload{}, nil
	}
	writtenAt, known := a2aBusConfigWrittenAt(live)
	if r.busConfigWrittenAt != nil {
		writtenAt, known = r.busConfigWrittenAt(live)
	}
	state := prior
	if state.folded == nil {
		state.folded = map[string]bool{}
	}
	if state.rearmed == nil {
		state.rearmed = map[string]bool{}
	}
	// The TLS Secret changed since the record: a reload it causes can't be
	// told from this one, so every recorded server folds, a re-armed one
	// included, until the kubelet's sync window has passed since the change
	// was first seen.
	watched, _, _, err := r.a2aBusWatched(ctx, agent)
	if err != nil {
		return a2aBusReload{}, err
	}
	if watched != state.watched {
		state.watched = watched
		state.tlsSeen = now.UTC().Format(time.RFC3339Nano)
	}
	if seen, err := time.Parse(time.RFC3339Nano, state.tlsSeen); err == nil && now.Sub(seen) < a2aBusKubeletSyncWindow {
		for name := range state.servers {
			delete(state.rearmed, name)
			state.folded[name] = true
		}
	}
	confirmed := map[string]bool{}
	readable := map[string]bool{}
	var foreign []string
	for _, s := range servers {
		if s.foreign {
			foreign = append(foreign, s.name)
		}
		if !s.serving() || s.extra {
			// An extra pod is on its way out with whatever it loaded: the
			// reload isn't confirmed while it serves.
			continue
		}
		varz, err := r.a2aBusVarz(ctx, agent, s.pod)
		if err != nil {
			continue
		}
		readable[s.name] = true
		rec, recorded := state.servers[s.name]
		switch a2aBusServerConfirms(rec, recorded, s.pod, varz, writtenAt, known, state.folded[s.name]) {
		case a2aBusConfirmed:
			confirmed[s.name] = true
		case a2aBusRearm:
			state.servers[s.name] = a2aBusServerRecord{UID: s.pod.UID, Start: varz.Start, Loaded: varz.Loaded}
			delete(state.folded, s.name)
			state.rearmed[s.name] = true
		}
	}
	if len(servers) > 0 && len(confirmed) == len(servers) {
		return a2aBusReload{reloaded: true, busUID: busUID}, nil
	}
	state.stamp(config, liveSts.Name)
	since, perr := time.Parse(time.RFC3339Nano, state.requestedAt)
	if perr == nil && now.Sub(since) < a2aBusReloadBound {
		return a2aBusReload{requeue: min(a2aBusReloadRecheck, a2aBusReloadBound-now.Sub(since)), busUID: busUID, foreign: foreign}, nil
	}
	plan := a2aBusReload{requeue: a2aBusReloadRecheck, busUID: busUID, foreign: foreign}
	if straggler, ok := a2aBusStragglerToRestart(servers, confirmed, writtenAt, known, now); ok {
		plan.restart, plan.restartUID = straggler.name, straggler.pod.UID
		// A re-armed fold the server still answers for is the expected
		// shape, not a failed reload; it restarts all the same.
		plan.folded = state.rearmed[straggler.name] && readable[straggler.name] && perr == nil
	}
	return plan, nil
}

// a2aBusYoungReplacement reports whether pod was created after the write and
// less than a2aBusReloadBound ago: it mounted the current Secret, and is
// owed the time to start and answer before anything restarts it.
func a2aBusYoungReplacement(pod *corev1.Pod, writtenAt time.Time, known bool, now time.Time) bool {
	return a2aBusCreatedAfter(pod, writtenAt, known) && now.Sub(pod.CreationTimestamp.Time) < a2aBusReloadBound
}

// a2aBusWatched reads the bus's TLS serving Secret: its resourceVersion
// (a2aBusReloadWatchedAnnotation) and when it was last written by the API
// server's clock, the latest managedFields entry of any manager (any write
// may change the files the reloader acts on, and a later time only folds
// more). Empty and unknown with none.
func (r *PlatformAgentReconciler) a2aBusWatched(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (string, time.Time, bool, error) {
	tls := &corev1.Secret{}
	if err := r.a2aReader().Get(ctx, types.NamespacedName{Name: agentv1alpha1.A2ANATSTLSSecretName(agent.Name), Namespace: agent.Namespace}, tls); err != nil {
		return "", time.Time{}, false, client.IgnoreNotFound(err)
	}
	var at time.Time
	if r.busTLSWrittenAt != nil {
		var ok bool
		if at, ok = r.busTLSWrittenAt(tls); !ok {
			at = time.Time{}
		}
	} else {
		for _, f := range tls.ManagedFields {
			if f.Time != nil && f.Time.After(at) {
				at = f.Time.Time
			}
		}
	}
	return tls.ResourceVersion, at, !at.IsZero(), nil
}

// a2aBusReadyFor reports whether pod has been Ready for at least d at now,
// by its Ready condition's last transition.
func a2aBusReadyFor(pod *corev1.Pod, d time.Duration, now time.Time) bool {
	for _, c := range pod.Status.Conditions {
		if c.Type == corev1.PodReady {
			return c.Status == corev1.ConditionTrue && !c.LastTransitionTime.IsZero() && now.Sub(c.LastTransitionTime.Time) >= d
		}
	}
	return false
}

// a2aBusStragglerToRestart picks the one server to restart past the bound:
// the first, by ordinal, that hasn't confirmed the reload, and only when every
// other server's pod is serving and has been Ready for
// a2aBusReadyBeforeRestart. None qualifies while any other server is down or
// just back, or while a scale-down is under way. A pod created after the
// write (the operator's own replacement, say) is given a full
// a2aBusReloadBound from its own creation to come up and answer before it can
// be restarted, so a replacement still starting isn't deleted again.
func a2aBusStragglerToRestart(servers []a2aBusServer, confirmed map[string]bool, writtenAt time.Time, known bool, now time.Time) (a2aBusServer, bool) {
	for _, s := range servers {
		if s.extra {
			// A scale-down is under way: the StatefulSet removes that pod
			// itself, and a restart now would only cost quorum.
			return a2aBusServer{}, false
		}
	}
	// Exactly one server not (long enough) Ready, and every other one is:
	// restarting that one costs no quorum, so it goes first when it hasn't
	// confirmed. Otherwise no server is restarted while another is down.
	var down []a2aBusServer
	for _, s := range servers {
		if !s.serving() || !a2aBusReadyFor(s.pod, a2aBusReadyBeforeRestart, now) {
			down = append(down, s)
		}
	}
	if len(down) == 1 {
		s := down[0]
		if s.serving() && !confirmed[s.name] && !a2aBusYoungReplacement(s.pod, writtenAt, known, now) {
			return s, true
		}
		return a2aBusServer{}, false
	}
	for i, s := range servers {
		if confirmed[s.name] || !s.serving() || a2aBusYoungReplacement(s.pod, writtenAt, known, now) {
			continue
		}
		others := true
		for j, o := range servers {
			if j != i && (!o.serving() || !a2aBusReadyFor(o.pod, a2aBusReadyBeforeRestart, now)) {
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
// for a reload every server confirmed, a Warning per pod the StatefulSet
// doesn't control, or one straggling server deleted, with a Warning, for a
// reload it didn't confirm in time. The live config is already the desired
// one on that path, and the StatefulSet recreates the pod on it; the pending
// record stays until that pod is read too. If the delete fails, the pass
// returns before the apply. The delete names the pod's UID, and only a pod
// the live StatefulSet controls is deleted.
func (r *PlatformAgentReconciler) actOnA2ABusReload(ctx context.Context, agent *agentv1alpha1.PlatformAgent, stsName string, plan a2aBusReload) error {
	for _, name := range plan.foreign {
		r.recordEvent(agent, corev1.EventTypeWarning, a2aBusForeignPodReason,
			fmt.Sprintf("Pod %s holds an A2A bus server's name, but the bus StatefulSet doesn't control it: what config it serves is unknown, so the bus config reload stays unconfirmed (and newly bound profiles held) while it exists.", name))
	}
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

// a2aBusVarz is what a reload reads from one server's /varz.
type a2aBusVarz struct {
	Name   string `json:"server_name"`
	Start  string `json:"start"`
	Loaded string `json:"config_load_time"`
}

// a2aBusVarz reads one bus server's /varz by its pod IP: a Service answers
// from whichever server it picks, and the per-pod names under the routes
// Service don't resolve (the StatefulSet's service is the client Service), so
// the pod's own address is the only way to ask a given server. An answer whose
// server_name (nats.conf sets it to $POD_NAME) isn't the pod's, an address
// reused by another pod, say, is no answer. Times are compared as the server
// reports them, or against the API server's clock where a2aBusServerConfirms
// says so. nats-server 2.10 serves no config digest to compare instead.
func (r *PlatformAgentReconciler) a2aBusVarz(ctx context.Context, agent *agentv1alpha1.PlatformAgent, pod *corev1.Pod) (a2aBusVarz, error) {
	var varz a2aBusVarz
	switch {
	case r.busVarz != nil:
		v, err := r.busVarz(ctx, agent, pod)
		if err != nil {
			return a2aBusVarz{}, err
		}
		varz = v
	case r.busConfigLoadTime != nil:
		loaded, err := r.busConfigLoadTime(ctx, agent, pod)
		if err != nil {
			return a2aBusVarz{}, err
		}
		varz = a2aBusVarz{Name: pod.Name, Start: "s", Loaded: loaded}
	default:
		v, err := a2aBusReadVarz(ctx, pod)
		if err != nil {
			return a2aBusVarz{}, err
		}
		varz = v
	}
	if varz.Name != pod.Name {
		return a2aBusVarz{}, fmt.Errorf("A2A bus pod %s answered as server %q", pod.Name, varz.Name)
	}
	if varz.Loaded == "" || varz.Start == "" {
		return a2aBusVarz{}, fmt.Errorf("A2A bus pod %s carries no start or config_load_time", pod.Name)
	}
	return varz, nil
}

// a2aBusReadVarz is the HTTP read behind a2aBusVarz.
func a2aBusReadVarz(ctx context.Context, pod *corev1.Pod) (a2aBusVarz, error) {
	if pod.Status.PodIP == "" {
		return a2aBusVarz{}, fmt.Errorf("A2A bus pod %s has no IP yet", pod.Name)
	}
	ctx, cancel := context.WithTimeout(ctx, a2aBusVarzTimeout)
	defer cancel()
	url := "http://" + net.JoinHostPort(pod.Status.PodIP, strconv.Itoa(a2aNATSMonitorPort)) + a2aBusVarzPath
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return a2aBusVarz{}, err
	}
	resp, err := a2aBusVarzClient.Do(req)
	if err != nil {
		return a2aBusVarz{}, err
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		return a2aBusVarz{}, fmt.Errorf("%s (%s) answered %s", url, pod.Name, resp.Status)
	}
	var varz a2aBusVarz
	if err := json.NewDecoder(resp.Body).Decode(&varz); err != nil {
		return a2aBusVarz{}, fmt.Errorf("decoding %s (%s): %w", url, pod.Name, err)
	}
	return varz, nil
}
