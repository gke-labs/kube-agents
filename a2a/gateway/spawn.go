package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"path"
	"strconv"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
	"k8s.io/utils/ptr"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// RouteSession is the DefaultAddressee sentinel that switches a conversation
// from a fixed addressee (the Hermes bridge's "platform") to a session pod
// of its own. Flipping to it — plus A2A_SPAWN_SESSIONS=true — is the W4
// switch; a setting, not surgery (retarget 8/26).
const RouteSession = "session"

const (
	// sweepInterval paces the orphan sweep; sweepPassTimeout bounds one
	// pass, so a hung stream or API call cannot make passes pile up.
	sweepInterval    = time.Minute
	sweepPassTimeout = time.Minute
	// podDeadlineGrace is what the pod-level activeDeadlineSeconds adds
	// above the adapter's task deadline. The adapter's contract is that its
	// deadline sits BELOW the pod's, so the failure is the adapter's to
	// report — the pod deadline only fires for a wedged adapter, handing it
	// to Sweep instead of letting it run indefinitely. It bounds the process,
	// not the credential: a bus connection the adapter already opened outlives
	// the pod by up to the callout's grant TTL, for the reason set out on
	// busTokenExpirationSeconds below.
	// The grace covers what the adapter's clock does not: the image pull
	// before the process starts (activeDeadlineSeconds runs from pod start)
	// plus the adapter's own shutdown-and-publish window at its deadline.
	podDeadlineGrace = 10 * time.Minute
	// ownerResolveTimeout bounds the boot-time GET for the owner
	// Deployment, so a hung API server fails boot fast into the crashloop
	// backoff instead of hanging it.
	ownerResolveTimeout = 30 * time.Second
	// primerCap bounds the rehydration-primer annotation, well under the
	// object annotation budget.
	primerCap = 8192
	// busTokenExpirationSeconds is how long the kubelet mints the session's
	// bus token for before rotating it. One hour is the API server's own
	// floor — ask for less and it is silently rounded up — and the kubelet
	// refreshes at 80% of it, so a session outliving its token is a case the
	// client's re-read on reconnect already handles.
	//
	// The lifetime is not the revocation story, and neither the lifetime nor
	// the pod binding revokes an established connection. Two clocks, and it
	// matters which one a claim is about:
	//
	//   - A NEW connection. The pod binding governs this one. Once the pod
	//     object is gone the API server stops authenticating the token, so a
	//     stolen token buys nothing; measured at about ten seconds behind the
	//     delete, the TokenReview success cache rather than the hour below.
	//   - An ALREADY-OPEN connection. Nothing above touches it. The callout
	//     is consulted once, at CONNECT, and the grants it issued live in a
	//     signed user JWT the server holds for that connection; the server
	//     closes it when that JWT expires and at no other time. That is the
	//     callout's grant TTL — an hour less jitter, not this token's hour —
	//     and authcallout's TestANarrowedMapDoesNotReachAnEstablishedConnection
	//     pins the behaviour.
	//
	// So a harness that opens a second bus connection before its task ends
	// keeps that connection's grants for up to the grant TTL after the pod is
	// reaped. What bounds the damage is that those grants are this session's
	// own subjects and nothing else, which is the change #1270 asked for; what
	// does NOT bound it is the pod lifecycle. Shortening the exposure means
	// shortening the callout's grant TTL, and that is the knob to reach for.
	busTokenExpirationSeconds = 3600

	// workerRunAsUser is the arbitrary non-root UID session pods run as.
	workerRunAsUser = 1000
	// Worker requests: the harness spike's per-session footprint, and what
	// the session cap's sizing math multiplies. The limits exist because a
	// namespace ResourceQuota that requires them refuses a pod without
	// them at admission, and a session pod refused there is a delegation
	// that never starts; they are the sandbox's (2 CPU, 2Gi), which is
	// what a headless harness turn has been observed to fit in. On an
	// Autopilot cluster without Pod bursting the limit is rewritten to the
	// request, so the request stays a ceiling a turn can live under.
	workerCPURequest    = "250m"
	workerMemoryRequest = "512Mi"
	workerCPULimit      = "2"
	workerMemoryLimit   = "2Gi"
	// sessionNameHexWidth suffixes minted session names — wide enough that
	// two conversations can't plausibly collide onto one addressee.
	// supervisorCorrHexWidth suffixes the fallback correlation id a
	// supervisor terminal mints when the orphan carried none.
	sessionNameHexWidth    = 4
	supervisorCorrHexWidth = 6

	// The temporary cluster view (Config.SessionClusterView). The token is
	// for the credential broker's session audience and nothing else - the
	// API server refuses a token minted for another audience, which is why
	// projecting it does not undo AutomountServiceAccountToken: false. 0444
	// because the kubelet writes it as root and the worker reads it as uid
	// 1000 (the shell sandbox's buildShellSandboxCredentialProxyTokenVolume
	// argues the same). Spelled identically in credential_proxy.py and the
	// operator's platformagent_broker_split.go.
	credentialProxySessionAudience        = "kubeagents-credential-proxy-session" // #nosec G101 -- audience name
	credentialProxyTokenVolume            = "credential-proxy-token"              // #nosec G101 -- volume name
	credentialProxyTokenMountPath         = "/var/run/secrets/kubeagents/credential-proxy"
	credentialProxyTokenFile              = "token"
	credentialProxyTokenMode              = 0444
	credentialProxyTokenExpirationSeconds = 3600
	// sessionHermesHome is where the shim files the kubeconfig artefacts
	// get-credentials returns ($HERMES_HOME/.kubeconfigs); the pod's
	// scratch emptyDir, the one writable place.
	sessionHermesHome = "/scratch"

	labelPartOf = "app.kubernetes.io/part-of"
	partOfValue = "a2a-next"
	labelRole   = "app.kubernetes.io/component"
	sessionRole = "a2a-session"
	annoTask    = "a2a.kubeagents.dev/task-id"
	annoContext = "a2a.kubeagents.dev/context-id"
	annoCorr    = "a2a.kubeagents.dev/correlation-id"
	annoAddr    = "a2a.kubeagents.dev/addressee"
	annoConvo   = "a2a.kubeagents.dev/session-key"
	annoPrimer  = "a2a.kubeagents.dev/rehydration-primer"
	// annoReuse marks a pod spawned to serve every turn of its
	// conversation (lib.EnvSessionReuse). The gateway hands a second task
	// only to a pod carrying it, read off the pod itself, so a pod an older
	// gateway spawned, whose adapter exits after one task, is never sent one.
	annoReuse  = "a2a.kubeagents.dev/session-reuse"
	reuseValue = "true"
	// workerContainer is the session pod's one container, the adapter's.
	workerContainer = "worker"
	// delegateToolOn is lib.EnvDelegateTool's value for a pod whose adapter
	// decides the tool per task (delegateToolEnv).
	delegateToolOn = "on"

	// primerVolume, primerMountPath and primerFileName place the primer
	// annotation in the worker container as a file.
	primerVolume    = "rehydration-primer"
	primerMountPath = "/etc/a2a-primer"
	primerFileName  = "primer"
)

// sessionNameAnimals seeds minted session names. W5 owns the canonical
// animal list (stolen from the demo); this short one keeps the dark path
// honest until integration.
var sessionNameAnimals = []string{"otter", "badger", "heron", "lynx", "marten", "puffin", "stoat", "vole"}

// spawner is the session-pod half of the lifecycle, dark behind
// SpawnSessions. What it creates is a pod holding a projected bus token: the
// credential a session authenticates with is minted by the API server against
// this pod object, so the pod IS the identity and Delete below is the closest
// thing to revocation the fabric has (bounded by the callout's grant TTL for
// a connection already open - see busTokenExpirationSeconds).
type spawner interface {
	// Spawn creates the session pod for a task and returns the pod name.
	// originSeq is the TASKS sequence of the submission the pod is being
	// spawned to execute; 0 means the caller could not determine one.
	Spawn(ctx context.Context, rec *SessionRecord, taskID, primer string, originSeq uint64) (string, error)
	// Delete removes a pod (reap).
	Delete(ctx context.Context, podName string) error
	// TerminalOrphans lists pods in a terminal phase, with the task identity
	// their annotations carry (sweep's scan).
	TerminalOrphans(ctx context.Context) ([]orphanPod, error)
	// LiveSessions counts session pods not yet in a terminal phase and not
	// being deleted - the session cap's denominator, over the same pods
	// SessionPods offers the cap's eviction.
	LiveSessions(ctx context.Context) (int, error)
	// Reusable reports whether a pod can take its conversation's next task:
	// spawned for reuse, running, not being deleted, its worker not exited,
	// and with room left in its lifetime for a whole task. A pod that is
	// gone is not reusable and not an error.
	Reusable(ctx context.Context, podName string) (bool, error)
	// SessionPods lists the session pods that are live and not being
	// deleted, with the conversation each belongs to: the cap eviction's
	// candidates.
	SessionPods(ctx context.Context) ([]sessionPod, error)
	// PodStarted is the pod's status.startTime, when the kubelet took it
	// (before the image pull), which is where activeDeadlineSeconds counts
	// from. Zero for a pod not yet started or gone; neither is an error.
	PodStarted(ctx context.Context, podName string) (time.Time, error)
}

// sessionPod is one live session pod as the cap eviction and the untracked
// sweep see it.
type sessionPod struct {
	PodName    string
	SessionKey string
	// Reuse is the pod's own reuse annotation: it serves every turn, and so
	// never exits on its own.
	Reuse bool
}

type orphanPod struct {
	PodName       string
	SessionKey    string
	Addressee     string
	TaskID        string
	ContextID     string
	CorrelationID string
}

// mintSessionName gives an incarnation its bus session name,
// <profile>-<animal> per the house ruling, minted FRESH per incarnation —
// reaping and respawning changes the pod and the bus session name;
// contextId is what persists (gateway design).
func mintSessionName(profile string) string {
	return fmt.Sprintf("%s-%s-%s", profile,
		sessionNameAnimals[int(time.Now().UnixNano())%len(sessionNameAnimals)], randHex(sessionNameHexWidth))
}

// originSeqValue renders the submission's stream sequence for the pod env.
// Zero is not a legal JetStream sequence, so it means the publish told us
// nothing; the sentinel says so out loud rather than leaving the variable off
// and making the worker guess whether its spawner was simply older.
func originSeqValue(seq uint64) string {
	if seq == 0 {
		return lib.OriginSeqUnknown
	}
	return strconv.FormatUint(seq, 10)
}

func activeCorrelation(rec *SessionRecord) string {
	if rec.ActiveTask != nil {
		return rec.ActiveTask.CorrelationID
	}
	return ""
}

// podSpawner is the client-go implementation.
type podSpawner struct {
	cfg    *Config
	client kubernetes.Interface
	log    *slog.Logger
	// owner is the ownerReference every spawned pod carries — the gateway's
	// own Deployment, resolved once at construction. Nil when no owner is
	// configured (playground).
	owner *metav1.OwnerReference
}

func newPodSpawner(cfg *Config, log *slog.Logger) (*podSpawner, error) {
	rc, err := rest.InClusterConfig()
	if err != nil {
		return nil, err
	}
	cs, err := kubernetes.NewForConfig(rc)
	if err != nil {
		return nil, err
	}
	s := &podSpawner{cfg: cfg, client: cs, log: log}
	rctx, cancel := context.WithTimeout(context.Background(), ownerResolveTimeout)
	defer cancel()
	if err := s.resolveOwner(rctx); err != nil {
		// Refuse to boot rather than quietly spawn unowned pods: an owner
		// was configured, so the orphaned-session window is supposed to be
		// closed, and a GET that fails here is a misconfig (name, RBAC) the
		// operator render owns.
		return nil, fmt.Errorf("resolving owner deployment %q: %w", cfg.OwnerDeployment, err)
	}
	return s, nil
}

// resolveOwner fetches the configured Deployment's UID and builds the
// ownerReference spawned pods carry. An ownerReference is name+UID, and the
// UID exists only server-side, so this is a read the gateway's Role grants
// on exactly this one Deployment (resourceNames).
func (s *podSpawner) resolveOwner(ctx context.Context) error {
	if s.cfg.OwnerDeployment == "" {
		return nil
	}
	dep, err := s.client.AppsV1().Deployments(s.cfg.Namespace).Get(ctx, s.cfg.OwnerDeployment, metav1.GetOptions{})
	if err != nil {
		return err
	}
	s.owner = &metav1.OwnerReference{
		APIVersion: "apps/v1",
		Kind:       "Deployment",
		Name:       dep.Name,
		UID:        dep.UID,
		// Controller marks this as the pod's managing owner for tooling
		// ("Controlled By"); no other controller claims session pods, and
		// the Deployment controller only manages ReplicaSets, so nothing
		// competes. BlockOwnerDeletion stays unset: GC does not need it and
		// setting it would require finalizer permissions on the owner.
		Controller: ptr.To(true),
	}
	return nil
}

// Spawn creates the session pod: the demo's reference worker shape — no
// ambient k8s credentials, scratch on emptyDir, 250m/512Mi requests —
// running the headless harness behind the worker adapter (W4's image).
// Model auth, as shipped (spec-chatops-gateway.md, amended 8/31): the
// worker talks to the install's own LiteLLM, in-namespace, with no per-pod
// credential at all — no Workload Identity. Direct Vertex via WI stays the
// target, and arming it is a policy change as well as an IAM one (the
// session egress fence encodes the shipped path).
//
// Its BUS credential is per session, and that is the change gke-labs#1270
// asked for. The pod runs as a no-RBAC ServiceAccount shared by every
// session and gets a projected token bound to itself; the callout reads the
// pod name the API server attests and mints grants for that session's
// subjects and no others. Two sessions running side by side hold different
// credentials, and neither holds a credential that speaks for the whole task
// plane -- the static `worker` user that did is retired, and what replaced it
// on the agent side is two narrower principals, neither of them reachable
// from a session pod.
func (s *podSpawner) Spawn(ctx context.Context, rec *SessionRecord, taskID, primer string, originSeq uint64) (string, error) {
	name := rec.BusSession
	// The gateway Deployment owns its sessions: when it goes — cleanupA2A on
	// a mode flip, or any other deletion — Kubernetes GC reaps the pods it
	// spawned, closing the orphaned-session window without an operator
	// exception to the IsControlledBy refusal. Empty on playground installs
	// that never configured an owner.
	var owners []metav1.OwnerReference
	if s.owner != nil {
		owners = []metav1.OwnerReference{*s.owner}
	}
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:            name,
			Namespace:       s.cfg.Namespace,
			OwnerReferences: owners,
			Labels: map[string]string{
				labelPartOf: partOfValue,
				labelRole:   sessionRole,
			},
			Annotations: map[string]string{
				annoTask:    taskID,
				annoContext: rec.ContextID,
				annoCorr:    activeCorrelation(rec),
				annoAddr:    rec.Addressee,
				annoConvo:   rec.Key,
				// The rehydration primer, mounted for the worker as a file
				// (primerVolume); bounded well under the object annotation
				// budget.
				annoPrimer: truncateRunes(primer, primerCap),
			},
		},
		Spec: corev1.PodSpec{
			RestartPolicy: corev1.RestartPolicyNever,
			// The session ServiceAccount, which holds no RBAC at all: it
			// exists so the kubelet can mint the bus token below against an
			// identity the callout's map is keyed on.
			ServiceAccountName: s.cfg.SessionServiceAccount,
			// Still false, and now more load-bearing than before. Automount
			// would add a SECOND token to the pod — default-audience, not
			// pod-bound, and usable against the API server. The projected
			// volume(s) below are the only credentials a session gets: the
			// bus token always, and under the cluster view, the credential
			// broker's session-audience token too — both audience-bound,
			// neither usable against the API server.
			AutomountServiceAccountToken: ptr.To(false),
			// The adapter's deadline sits below this by construction (its
			// contract, and podDeadlineGrace's comment): a healthy adapter
			// always publishes the terminal and exits first, so this fires
			// only for a wedged adapter — the one end Session lifecycle
			// used to name as unowned. A reused pod serves many tasks, so
			// its bound is the session's maximum lifetime instead
			// (podLifetime), with the reap scan's overdue check bounding
			// each task inside it.
			ActiveDeadlineSeconds: ptr.To(int64(podLifetime(s.cfg, s.cfg.reusePod(rec)) / time.Second)),
			SecurityContext: &corev1.PodSecurityContext{
				RunAsNonRoot:   ptr.To(true),
				RunAsUser:      ptr.To(int64(workerRunAsUser)),
				SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
			},
			Containers: []corev1.Container{{
				Name:  workerContainer,
				Image: s.cfg.WorkerImage,
				Env: []corev1.EnvVar{
					// The worker env contract (launch-card constants):
					// TASK_ID/PROFILE/NATS_URL. PROFILE names the
					// AgentProfile — the addressee is the bus session name,
					// which is not a profile. No bus credential rides here any
					// more: it is the projected token mounted below, which is
					// the whole of gke-labs#1270.
					{Name: "TASK_ID", Value: taskID},
					{Name: "PROFILE", Value: rec.Profile},
					{Name: "NATS_URL", Value: s.cfg.NATSURL},
					{Name: "A2A_SESSION", Value: rec.BusSession},
					{Name: lib.EnvPrimerFile, Value: primerMountPath + "/" + primerFileName},
					{Name: lib.EnvDelegateTool, Value: s.delegateToolEnv(rec, taskID)},
					// The pod's own name, from the kubelet rather than from
					// us. It equals A2A_SESSION by construction above, and
					// the adapter checks that rather than trusting either:
					// the callout derives this session's grants from the pod
					// name the API server attested, so a client that pinned
					// its inbox to the other name would hang on every reply.
					{Name: lib.EnvPodName, ValueFrom: &corev1.EnvVarSource{
						FieldRef: &corev1.ObjectFieldSelector{FieldPath: "metadata.name"},
					}},
					// The adapter's half of the deadline contract, rendered
					// from the same config the pod deadline above is sized
					// from — one number, two enforcement layers, no drift.
					{Name: "A2A_TASK_DEADLINE_SECONDS", Value: strconv.Itoa(int(s.cfg.TaskDeadline / time.Second))},
					// Which message on this task's `…in` subject is the
					// submission. Always set, including when we do not know:
					// the worker has to tell "spawned by something that never
					// says" from "spawned by something that says it could not
					// tell". Both of those fall back to scanning the subject,
					// so the distinction does not change what the worker does
					// - it changes what it can say about it. The sentinel is
					// a spawner admitting a gap; absence is a spawner too old
					// to have one, and only the second is unremarkable
					// (lib.EnvOriginSeq).
					{Name: lib.EnvOriginSeq, Value: originSeqValue(originSeq)},
					// The capability contract's two halves, rendered the same
					// way and for the same reason.
					//
					// Scope, because the gateway MINTS under its resolved
					// AuthorityScope and the executor CHECKS against its own:
					// they are compared, not merely both present, so a session
					// pod left to default would derive `namespace/-` — the
					// scope that contains nothing — and refuse every task with
					// a message that reads like a capability bug rather than a
					// missing variable. It cannot derive the real one either:
					// no POD_NAMESPACE rides here, deliberately, because the
					// value that has to match is the gateway's resolved
					// setting and not whatever namespace the pod landed in.
					//
					// Required, because a mixed-version install needs both
					// halves relaxed together. One switch on the gateway
					// Deployment arms or relaxes the executor too, so there is
					// no state where the gateway mints nothing and the
					// executor insists on a capability.
					{Name: "A2A_AUTHORITY_SCOPE", Value: string(s.cfg.AuthorityScope)},
					{Name: "A2A_CAPABILITY_REQUIRED", Value: strconv.FormatBool(!s.cfg.CapabilityOptional)},
				},
				Resources: corev1.ResourceRequirements{
					Requests: corev1.ResourceList{
						corev1.ResourceCPU:    resource.MustParse(workerCPURequest),
						corev1.ResourceMemory: resource.MustParse(workerMemoryRequest),
					},
					Limits: corev1.ResourceList{
						corev1.ResourceCPU:    resource.MustParse(workerCPULimit),
						corev1.ResourceMemory: resource.MustParse(workerMemoryLimit),
					},
				},
				VolumeMounts: []corev1.VolumeMount{
					{Name: "scratch", MountPath: "/scratch"},
					{Name: "bus-token", MountPath: path.Dir(lib.BusTokenPath), ReadOnly: true},
					{Name: primerVolume, MountPath: primerMountPath, ReadOnly: true},
				},
				SecurityContext: &corev1.SecurityContext{
					AllowPrivilegeEscalation: ptr.To(false),
					Capabilities:             &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}},
					SeccompProfile:           &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
				},
			}},
			Volumes: []corev1.Volume{
				{
					Name:         "scratch",
					VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}},
				},
				// The transcript primer, read by the worker before its
				// first turn (lib.EnvPrimerFile). The annotation is the
				// pod's own, so the file is written once at start and the
				// kubelet needs no API access to produce it.
				{
					Name: primerVolume,
					VolumeSource: corev1.VolumeSource{DownwardAPI: &corev1.DownwardAPIVolumeSource{
						Items: []corev1.DownwardAPIVolumeFile{{
							Path:     primerFileName,
							FieldRef: &corev1.ObjectFieldSelector{FieldPath: "metadata.annotations['" + annoPrimer + "']"},
						}},
					}},
				},
				// The bus credential, as a pod-bound token rather than a
				// shared password.
				//
				// Being a file is NOT what fixes gke-labs#1270, and reading
				// it that way would be a false sense of safety: the harness
				// runs at the same UID in the same pod, so it can read this
				// file as easily as it could read /proc/1/environ. What
				// changes is what the credential is worth. It is
				// audience-bound, so it is not a cluster credential; it is
				// bound by the kubelet to this pod, so the callout can tell
				// this session from the one beside it on the same account;
				// and the grants minted from it cover this session's own
				// subjects and nothing else. A harness that reads it holds
				// exactly the authority it already had by being the session.
				// The shared `worker` password it replaces spoke for the
				// whole task plane.
				{
					Name: "bus-token",
					VolumeSource: corev1.VolumeSource{Projected: &corev1.ProjectedVolumeSource{
						Sources: []corev1.VolumeProjection{{
							ServiceAccountToken: &corev1.ServiceAccountTokenProjection{
								Audience:          lib.BusTokenAudience,
								ExpirationSeconds: ptr.To(int64(busTokenExpirationSeconds)),
								Path:              path.Base(lib.BusTokenPath),
							},
						}},
					}},
				},
			},
		},
	}
	if s.cfg.reusePod(rec) {
		pod.Annotations[annoReuse] = reuseValue
		c := &pod.Spec.Containers[0]
		c.Env = append(c.Env, corev1.EnvVar{Name: lib.EnvSessionReuse, Value: reuseValue})
	}
	if s.cfg.SessionClusterView {
		c := &pod.Spec.Containers[0]
		c.Env = append(c.Env,
			corev1.EnvVar{Name: "CREDENTIAL_PROXY_URL", Value: s.cfg.CredentialProxyURL},
			corev1.EnvVar{Name: "CREDENTIAL_PROXY_TOKEN_FILE", Value: credentialProxyTokenMountPath + "/" + credentialProxyTokenFile},
			corev1.EnvVar{Name: "HERMES_HOME", Value: sessionHermesHome},
			corev1.EnvVar{Name: lib.EnvClusterView, Value: "true"},
		)
		c.VolumeMounts = append(c.VolumeMounts, corev1.VolumeMount{Name: credentialProxyTokenVolume, MountPath: credentialProxyTokenMountPath, ReadOnly: true})
		pod.Spec.Volumes = append(pod.Spec.Volumes, corev1.Volume{
			Name: credentialProxyTokenVolume,
			VolumeSource: corev1.VolumeSource{Projected: &corev1.ProjectedVolumeSource{
				DefaultMode: ptr.To(int32(credentialProxyTokenMode)),
				Sources: []corev1.VolumeProjection{{ServiceAccountToken: &corev1.ServiceAccountTokenProjection{
					Audience:          credentialProxySessionAudience,
					ExpirationSeconds: ptr.To(int64(credentialProxyTokenExpirationSeconds)),
					Path:              credentialProxyTokenFile,
				}}},
			}},
		})
	}
	created, err := s.client.CoreV1().Pods(s.cfg.Namespace).Create(ctx, pod, metav1.CreateOptions{})
	if err != nil {
		// AlreadyExists included: session names are minted per incarnation,
		// so a name collision means the mint raced a terminating predecessor
		// — surface it rather than adopt a pod that is about to vanish.
		return "", err
	}
	return created.Name, nil
}

func (s *podSpawner) Delete(ctx context.Context, podName string) error {
	err := s.client.CoreV1().Pods(s.cfg.Namespace).Delete(ctx, podName, metav1.DeleteOptions{})
	if apierrors.IsNotFound(err) {
		return nil
	}
	return err
}

// LiveSessions counts through the k8s API rather than the gateway's own
// registry: the API is authoritative, survives a gateway restart, and sees
// orphans the registry has forgotten. Terminal pods are sweep's inventory,
// not load, so they don't count - matching what a `pods` ResourceQuota
// counts, which is the layer that backstops this number.
//
// A pod being deleted does not count either, though the quota still does
// until it is gone: its slot is already freeing, and the cap's eviction
// cannot choose it (SessionPods skips it too), so counting it would evict
// some other conversation's live pod for a slot nobody needs. The count and
// the candidates agree, and the overlap while it terminates is the same one
// a successful eviction already leaves, which the quota headroom absorbs.
func (s *podSpawner) LiveSessions(ctx context.Context) (int, error) {
	pods, err := s.client.CoreV1().Pods(s.cfg.Namespace).List(ctx, metav1.ListOptions{
		LabelSelector: fmt.Sprintf("%s=%s,%s=%s", labelPartOf, partOfValue, labelRole, sessionRole),
	})
	if err != nil {
		return 0, err
	}
	n := 0
	for _, p := range pods.Items {
		if p.Status.Phase == corev1.PodSucceeded || p.Status.Phase == corev1.PodFailed || p.DeletionTimestamp != nil {
			continue
		}
		n++
	}
	return n, nil
}

// reusePod reports whether a pod spawned for rec serves every turn of its
// conversation: reuse is on and the conversation is on the session route.
// A one-shot Delegate from a fixed-route conversation is not: the next plain
// ask goes back to the fixed addressee, nothing would ever route to the pod
// again, and a pod that did not exit after its task would idle until the
// reap.
func (c *Config) reusePod(rec *SessionRecord) bool {
	return c.SessionReuse && rec.SessionRouted
}

// podLifetime is a session pod's activeDeadlineSeconds: the task deadline
// plus its grace for a pod that serves one task, and the session's maximum
// lifetime for one that serves its conversation (never less than a task's
// own bound, whatever the config says).
func podLifetime(cfg *Config, reuse bool) time.Duration {
	one := cfg.TaskDeadline + podDeadlineGrace
	if !reuse {
		return one
	}
	lifetime := cfg.SessionMaxLifetime
	if lifetime <= 0 {
		lifetime = defaultSessionMaxLifetime
	}
	return max(lifetime, cfg.TaskDeadline+2*podDeadlineGrace)
}

// delegateToolEnv is the pod's delegate tool setting. A one-task pod gets
// the setting for the task it is started for (delegateToolFor). A reused pod
// runs human turns and wakes alike, so it gets the tool, and its adapter
// turns it off for each task that is a wake, from the submission's own
// authority block (workeradapter.delegateOffForTask): the wake's `via`.
func (s *podSpawner) delegateToolEnv(rec *SessionRecord, taskID string) string {
	if s.cfg.reusePod(rec) {
		return delegateToolOn
	}
	return delegateToolFor(rec, taskID)
}

func (s *podSpawner) PodStarted(ctx context.Context, podName string) (time.Time, error) {
	p, err := s.client.CoreV1().Pods(s.cfg.Namespace).Get(ctx, podName, metav1.GetOptions{})
	if apierrors.IsNotFound(err) {
		return time.Time{}, nil
	}
	if err != nil {
		return time.Time{}, err
	}
	if p.Status.StartTime == nil {
		return time.Time{}, nil
	}
	return p.Status.StartTime.Time, nil
}

// Reusable reads the pod itself rather than the session record: only the API
// knows whether the worker has exited or the kubelet is about to end it.
// Running only, not Pending: a pod that has not started yet has no task
// behind it to have proved it can, and the heal that released its first task
// may have done so because it never will.
func (s *podSpawner) Reusable(ctx context.Context, podName string) (bool, error) {
	p, err := s.client.CoreV1().Pods(s.cfg.Namespace).Get(ctx, podName, metav1.GetOptions{})
	if apierrors.IsNotFound(err) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return podReusable(p, s.cfg.TaskDeadline, time.Now()), nil
}

// podReusable is Reusable's judgement on a pod already read. The lifetime
// check keeps a task off a pod the kubelet will end before the task's own
// bound: the pod's age plus the task deadline and twice its grace (the
// overdue check's point and a margin past it) has to fit in what is left.
func podReusable(p *corev1.Pod, taskDeadline time.Duration, now time.Time) bool {
	if p.Annotations[annoReuse] != reuseValue || p.DeletionTimestamp != nil || p.Status.Phase != corev1.PodRunning {
		return false
	}
	running := false
	for _, cs := range p.Status.ContainerStatuses {
		if cs.Name == workerContainer {
			running = cs.State.Running != nil
		}
	}
	if !running {
		return false
	}
	if p.Spec.ActiveDeadlineSeconds != nil && p.Status.StartTime != nil {
		end := p.Status.StartTime.Add(time.Duration(*p.Spec.ActiveDeadlineSeconds) * time.Second)
		if now.Add(taskDeadline + 2*podDeadlineGrace).After(end) {
			return false
		}
	}
	return true
}

func (s *podSpawner) SessionPods(ctx context.Context) ([]sessionPod, error) {
	pods, err := s.client.CoreV1().Pods(s.cfg.Namespace).List(ctx, metav1.ListOptions{
		LabelSelector: fmt.Sprintf("%s=%s,%s=%s", labelPartOf, partOfValue, labelRole, sessionRole),
	})
	if err != nil {
		return nil, err
	}
	var out []sessionPod
	for _, p := range pods.Items {
		if p.Status.Phase == corev1.PodSucceeded || p.Status.Phase == corev1.PodFailed || p.DeletionTimestamp != nil {
			continue
		}
		out = append(out, sessionPod{PodName: p.Name, SessionKey: p.Annotations[annoConvo], Reuse: p.Annotations[annoReuse] == reuseValue})
	}
	return out, nil
}

func (s *podSpawner) TerminalOrphans(ctx context.Context) ([]orphanPod, error) {
	pods, err := s.client.CoreV1().Pods(s.cfg.Namespace).List(ctx, metav1.ListOptions{
		LabelSelector: fmt.Sprintf("%s=%s,%s=%s", labelPartOf, partOfValue, labelRole, sessionRole),
	})
	if err != nil {
		return nil, err
	}
	var out []orphanPod
	for _, p := range pods.Items {
		if p.Status.Phase != corev1.PodSucceeded && p.Status.Phase != corev1.PodFailed {
			continue
		}
		out = append(out, orphanPod{
			PodName:       p.Name,
			SessionKey:    p.Annotations[annoConvo],
			Addressee:     p.Annotations[annoAddr],
			TaskID:        p.Annotations[annoTask],
			ContextID:     p.Annotations[annoContext],
			CorrelationID: p.Annotations[annoCorr],
		})
	}
	return out, nil
}

// refuseAtSessionCap is the usability half of the session-pod bound, checked
// before any route mutation that will need a fresh pod. At the cap the pod
// that has been idle longest makes room (evictIdleSession), and its
// conversation is told; with no idle pod the turn is refused with a reply
// naming the numbers - never silently queued, never dropped. replacing marks a turn that retires its previous incarnation in
// the same breath: the doomed pod is usually still live at count time, so
// it hands its slot to its successor rather than double-counting - and if
// it already went terminal or is already being deleted (LiveSessions counts
// neither), the extra slot is a transient overshoot the quota headroom
// absorbs.
//
// The count-then-create race is real, and wider than one: conversations run
// on distinct queue workers, so every delegation in flight inside the
// count-to-create window can pass this check together - the true bound on
// the overshoot is the namespace ResourceQuota the operator renders above
// this number, not cap+1 and not a lock here. A lock would only serialize
// this process, while the quota also holds against a gateway that has
// stopped honoring its own cap.
func (g *Gateway) refuseAtSessionCap(ctx context.Context, rec *SessionRecord, replacing bool) bool {
	// SessionRouted persists in the KV record; the spawner is a setting. A
	// W4 rollback leaves session-routed records this check still reaches -
	// with nothing to cap, let the turn degrade the way it always did
	// (publish toward an addressee nothing owns) rather than panic.
	if g.spawner == nil {
		return false
	}
	live, err := g.spawner.LiveSessions(ctx)
	if err != nil {
		// Proceeding blind would make the cap advisory exactly when the API
		// is misbehaving; say so instead of dropping the turn silently.
		g.log.Error("session cap: live count failed", "conversation", rec.Key, "err", err)
		g.post(rec.Key, "⚠️ not started: can't count the running session workers right now — try again in a moment")
		return true
	}
	limit := g.cfg.MaxSessions
	if replacing {
		limit++
	}
	if live < limit {
		return false
	}
	// Full, but a pod at the cap may only be holding a conversation nobody
	// is in right now. Evicting the longest-idle one makes room for one that
	// is; its conversation is told, and resumes cold with the primer. A
	// successful eviction is the slot; the count is not read again. A pod
	// already being deleted is not counted above (LiveSessions), so its
	// freeing slot never costs another conversation its pod.
	if g.evictIdleSession(ctx, rec) {
		return false
	}
	workers := fmt.Sprintf("%d session workers are", live)
	if live == 1 {
		workers = "1 session worker is"
	}
	g.log.Warn("session cap: delegation refused", "conversation", rec.Key, "live", live, "cap", g.cfg.MaxSessions)
	g.post(rec.Key, fmt.Sprintf(
		"🚦 not started: %s already running (cap %d). Wait for one to finish or `stop` one you started; an operator can raise the cap (A2A_MAX_SESSIONS / spec.harness.tuning.maxSessions).",
		workers, g.cfg.MaxSessions))
	return true
}

// ensureSessionPod spawns (or rehydrates) the session's incarnation for a
// new task. Only called on session-addressed routes.
func (g *Gateway) ensureSessionPod(ctx context.Context, rec *SessionRecord, taskID string, originSeq uint64) {
	if rec.PodName != "" {
		return
	}
	primer, authors, unknown, since := g.buildRehydrationPrimer(ctx, rec, taskID)
	// The pod reads the earlier turns, so their people count for a
	// delegation from it, as the delegating turn's own do.
	rec.seedSessionAuthors(authors, unknown, since)
	podName, err := g.spawner.Spawn(ctx, rec, taskID, primer, originSeq)
	if err != nil {
		g.log.Error("session pod spawn failed", "session", rec.Key, "err", err)
		// The task is already on the stream and no pod will ever run it.
		// Left alone it is invisible to Sweep (which watches pods) and
		// non-terminal for the whole retention window while the
		// conversation steers into it — so the supervisor rule applies
		// here exactly as at deletion: publish the terminal and let the
		// relay render it onto the rolling line, the same way it renders
		// Sweep's. `failed`, not `canceled`: nothing was stopped, the
		// executor never existed.
		if perr := g.publishSupervisorTerminal(ctx, rec.Addressee, taskID, rec.ContextID, activeCorrelation(rec),
			lib.StateFailed, "session pod could not be created; declared failed by its supervisor"); perr != nil {
			// The residue: the task publish just succeeded, so failing
			// here means the bus dropped between the two publishes. The
			// task stays non-terminal until the user's stop detaches it
			// and retirement completes the cancel.
			g.log.Error("spawn-failure supervisor terminal failed; task left non-terminal", "task", taskID, "err", perr)
			g.post(rec.Key, "⚠️ the session worker could not be started and the task could not be closed — `stop` it, then try again")
		}
		return
	}
	rec.PodName = podName
	rec.PodReuse = g.cfg.reusePod(rec)
	g.log.Info("spawned session pod", "session", rec.Key, "pod", podName, "task", taskID, "reuse", rec.PodReuse)
}

// sweepLoop is the gateway's half of the orphaned-task answer: it is the
// supervisor for sessions it spawned. A pod in a terminal phase whose task
// never emitted a final event gets a terminal event published by the
// gateway, then the pod is deleted. The state follows the supervisor rule:
// `failed` for an executor that died mid-work, `canceled` where the task
// had detached — a worker that exits or wedges after a `stop` reaches here
// routinely, and an unconditional `failed` would report broken for every
// task a user stopped. The synthesized event goes on the task's
// `…supervisor` subject, which only the gateway's grant reaches, so replay
// distinguishes "the worker said failed" from "the supervisor declared it
// dead" by where the bytes are rather than by what from says. (The
// dispatcher's janitor is the other half, for profile-addressed tasks —
// stage 3, and it inherits the token.)
func (g *Gateway) sweepLoop(ctx context.Context) {
	ticker := time.NewTicker(sweepInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			g.sweepOnce(ctx)
		}
	}
}

// isTaskNotFound reports whether a TasksGet failure means "no events yet"
// (the library's TaskNotFound A2AError) rather than the stream failing to
// answer. The supervisor paths may only proceed on the former.
func isTaskNotFound(err error) bool {
	var aerr *lib.A2AError
	return errors.As(err, &aerr) && aerr.Code == lib.CodeTaskNotFound
}

func (g *Gateway) sweepOnce(ctx context.Context) {
	ctx, cancel := context.WithTimeout(ctx, sweepPassTimeout)
	defer cancel()
	// The untracked pass runs whatever the orphan list does: it lists for
	// itself, and an API blip there must not hold up the other.
	defer g.sweepUntracked(ctx)
	orphans, err := g.spawner.TerminalOrphans(ctx)
	if err != nil {
		g.log.Error("sweep: pod list failed", "err", err)
		return
	}
	for _, o := range orphans {
		if o.TaskID == "" || o.Addressee == "" {
			g.log.Warn("sweep: terminal pod without task annotations; deleting", "pod", o.PodName)
			_ = g.spawner.Delete(ctx, o.PodName)
			g.releaseIncarnation(ctx, o)
			continue
		}
		// The record says which task the pod was on when it died, and
		// whether a stop is on the stream for it (the history carries that
		// mark durably; ActiveTask may long since have moved on). A record
		// we cannot read right now is a reason to wait a cycle, not to
		// guess.
		var rec *SessionRecord
		if o.SessionKey != "" {
			rec, err = g.reg.Get(ctx, o.SessionKey)
			if err != nil {
				g.log.Error("sweep: record read failed; retrying next cycle", "session", o.SessionKey, "err", err)
				continue
			}
		}
		settled := true
		for _, t := range sweepTasks(o, rec) {
			if !g.sweepTask(ctx, o, rec, t) {
				settled = false
			}
		}
		if !settled {
			continue // keep the pod as evidence until every terminal lands
		}
		_ = g.spawner.Delete(ctx, o.PodName)
		g.releaseIncarnation(ctx, o)
	}
}

// sweepUntracked deletes live reused pods that their conversation no longer
// names. A retirement whose delete failed clears the record's PodName anyway
// (retireIncarnation), and before reuse the pod it left exited after its task
// and reached the terminal phase the sweep above watches. A reused pod waits
// for its next task instead, so nothing would route to it, reap it or sweep
// it, and it would hold its credential and a cap slot until its lifetime
// ends. The check runs under the conversation's lock, only tried, the way the
// cap eviction does it (evictSession): every spawn and record write for that
// conversation holds the lock, so a pod its record does not name there is
// untracked rather than one whose spawn is still being written down.
func (g *Gateway) sweepUntracked(ctx context.Context) {
	pods, err := g.spawner.SessionPods(ctx)
	if err != nil {
		g.log.Error("sweep: live pod list failed", "err", err)
		return
	}
	for _, p := range pods {
		if !p.Reuse || p.SessionKey == "" {
			continue
		}
		g.deleteIfUntracked(ctx, p)
	}
}

// deleteIfUntracked deletes one pod if its conversation's record, read under
// that conversation's lock, names it neither as its pod nor as the session of
// a task still running. It reports whether it did.
//
// The session counts as well as the pod because the two reach the store
// separately: startTaskWith writes the record with the new session and its
// task before ensureSessionPod spawns, and PodName lands only in the turn's
// final write. A record whose final write failed names the session, with the
// task running on that pod, and no PodName; deleting there would kill the
// task with no terminal. It counts only while a task is active: once that
// task has ended, a record with no PodName can never route to the pod again
// (reuseLivePod requires PodName), the reap cannot find it, and sparing it
// here would leave it holding its credential and a cap slot until its
// lifetime ends.
func (g *Gateway) deleteIfUntracked(ctx context.Context, p sessionPod) bool {
	l := g.lockSession(p.SessionKey)
	if !l.TryLock() {
		return false
	}
	defer l.Unlock()
	rec, err := g.reg.Get(ctx, p.SessionKey)
	if err != nil || (rec != nil && (rec.PodName == p.PodName || (rec.BusSession == p.PodName && rec.ActiveTask != nil))) {
		return false
	}
	if err := g.spawner.Delete(ctx, p.PodName); err != nil {
		g.log.Error("untracked session pod delete failed", "pod", p.PodName, "err", err)
		return false
	}
	g.log.Info("deleted an untracked session pod", "conversation", p.SessionKey, "pod", p.PodName)
	return true
}

// sweptTask is one task a dead pod may owe a terminal for.
type sweptTask struct {
	ID, ContextID, CorrelationID string
}

// sweepTasks lists the tasks a dead pod may have left without a terminal: the
// one its annotation names, which is the task it was spawned for, and the
// task its conversation's record holds as active when that task ran on this
// pod. The second is the one that matters for a reused pod, which serves many
// tasks under an annotation naming only its first: a pod that dies on turn N
// has to close turn N. The pod's annotations cannot be updated to follow it
// (the gateway holds no patch on pods), and they do not need to be: every
// earlier turn reached its terminal before the next one was routed here.
func sweepTasks(o orphanPod, rec *SessionRecord) []sweptTask {
	tasks := []sweptTask{{ID: o.TaskID, ContextID: o.ContextID, CorrelationID: o.CorrelationID}}
	if rec == nil || rec.ActiveTask == nil || rec.ActiveTask.TaskID == o.TaskID ||
		rec.AddresseeFor(rec.ActiveTask.TaskID) != o.Addressee {
		return tasks
	}
	return append(tasks, sweptTask{ID: rec.ActiveTask.TaskID, ContextID: rec.ContextID, CorrelationID: rec.ActiveTask.CorrelationID})
}

// sweepTask closes one task a dead pod left without a terminal, and reports
// whether the task is settled: final on the stream already, or closed now.
// The supervisor writes what happened: `canceled` if a cancel for this task
// is on the stream, `failed` otherwise.
func (g *Gateway) sweepTask(ctx context.Context, o orphanPod, rec *SessionRecord, t sweptTask) bool {
	task, err := g.client.TasksGet(ctx, o.Addressee, t.ID)
	if err == nil && task.Final {
		return true // clean exit; nothing owed
	}
	if err != nil && !isTaskNotFound(err) {
		// TaskNotFound says "no events", which is the orphan shape.
		// Anything else is the stream not answering — and a terminal we
		// cannot rule out is a reason to wait a cycle, not to author what
		// could be the second final (assertion 10).
		g.log.Error("sweep: replay failed; retrying next cycle", "task", t.ID, "err", err)
		return false
	}
	state := lib.StateFailed
	note := "session pod exited without a terminal event; declared failed by its supervisor"
	if rec != nil && rec.TaskCanceled(t.ID) {
		state = lib.StateCanceled
		note = "the requester's stop, completed by the supervisor: the worker exited without publishing its terminal"
	}
	if err := g.publishSupervisorTerminal(ctx, o.Addressee, t.ID, t.ContextID, t.CorrelationID, state, note); err != nil {
		g.log.Error("sweep: supervisor terminal publish failed", "task", t.ID, "err", err)
		return false
	}
	g.log.Warn("sweep: closed orphaned task", "task", t.ID, "state", state, "pod", o.PodName)
	return true
}

// closeDetachedBeforeDelete is the one rule for every pod the gateway
// deletes itself (Session lifecycle states it once; reap, Sweep, Delegate,
// and the pre-flip retirement all reach it): a pod running a DETACHED task
// gets that task's terminal event published before the delete — deleting
// first would strand the task non-terminal for the whole retention window,
// since the adapter's deadline dies with the process and a deleted pod
// never reaches the phase Sweep watches. The state is `canceled`, not
// `failed`: detached means a stop already published a cancel, so the
// gateway is finishing the cancel the requester asked for (assertion 13's
// enumeration). Returns false when the terminal could not be published; the
// caller must keep the pod.
func (g *Gateway) closeDetachedBeforeDelete(ctx context.Context, rec *SessionRecord) bool {
	active := rec.ActiveTask
	if active == nil || !active.Detached {
		return true // idle pod, or a healed task: nothing owed
	}
	// The task ran under the addressee its own subjects carried — after a
	// Delegate re-home rec.Addressee is already the successor's.
	addressee := rec.AddresseeFor(active.TaskID)
	if addressee != rec.BusSession {
		// Not this pod's task: a child the session delegated to platform
		// runs on platform's executor, and retiring the pod that asked for
		// it ends nothing. A supervisor terminal here would be a claim about
		// another executor's task.
		return true
	}
	// Sweep's guard, for the same reason: Detached means the terminal has
	// not been RELAYED, not that it does not exist. A worker that confirmed
	// the cancel before the relay clears the flag — relay lag, a restart
	// with the delete already done — already put the one final on the
	// stream, and a second would be the protocol error assertion 10 makes
	// every consumer surface.
	task, err := g.client.TasksGet(ctx, addressee, active.TaskID)
	if err == nil && task.Final {
		return true
	}
	if err != nil && !isTaskNotFound(err) {
		// Same rule as Sweep: a replay failure means an existing final
		// could not be ruled out, and guessing would author the duplicate
		// the guard above exists to prevent. Keep the pod.
		g.log.Error("pre-delete replay failed; keeping the pod", "task", active.TaskID, "err", err)
		return false
	}
	err = g.publishSupervisorTerminal(ctx, addressee, active.TaskID, rec.ContextID, active.CorrelationID,
		lib.StateCanceled, "the requester's stop, completed by the supervisor at pod retirement")
	if err != nil {
		g.log.Error("supervisor terminal before delete failed", "task", active.TaskID, "err", err)
		return false
	}
	return true
}

// releaseIncarnation clears the session record's pod binding after sweep
// removes a dead pod — otherwise ensureSessionPod sees a PodName forever
// and an active conversation (which keeps resetting the idle clock, so reap
// never fires) has no executor and no way to get one.
func (g *Gateway) releaseIncarnation(ctx context.Context, o orphanPod) {
	if o.SessionKey == "" {
		return
	}
	l := g.lockSession(o.SessionKey)
	l.Lock()
	defer l.Unlock()
	rec, err := g.reg.Get(ctx, o.SessionKey)
	if err != nil || rec == nil || rec.PodName != o.PodName {
		return
	}
	rec.PodName = ""
	if err := g.reg.Put(ctx, rec); err != nil {
		g.log.Error("sweep: record release failed", "session", o.SessionKey, "err", err)
	}
}

// publishSupervisorTerminal writes a task's terminal event on behalf of the
// gateway as supervisor — `failed` for an executor that died mid-work,
// `canceled` where the supervisor is finishing a cancel already on the
// stream (the callers own that choice). On the task's `…supervisor` subject,
// never its `…events`: the executor's subject has one writer, and a
// supervisor terminal there is exactly the shape a hostile executor would
// forge.
func (g *Gateway) publishSupervisorTerminal(ctx context.Context, addressee, taskID, contextID, correlationID string, state lib.TaskState, note string) error {
	if correlationID == "" {
		// The record should always carry it; a missing one still gets a
		// terminal event, honestly labeled.
		correlationID = "corr-supervisor-" + randHex(supervisorCorrHexWidth)
	}
	payload, err := json.Marshal(lib.StatusUpdate{
		TaskID:    taskID,
		ContextID: contextID,
		Status: lib.TaskStatus{
			State: state,
			Message: &lib.Message{
				Role:      "agent",
				MessageID: "msg-" + randHex(messageIDHexWidth),
				Parts:     []lib.Part{{Kind: "text", Text: note}},
			},
		},
		Final: true,
	})
	if err != nil {
		return err
	}
	env, err := lib.NewStatusUpdateEnvelope(gatewayParty, taskID, contextID, correlationID, payload)
	if err != nil {
		return err
	}
	return g.client.Publish(ctx, lib.TaskSupervisorSubject(addressee, taskID), env)
}

// delegateToolFor is the session's delegate tool setting for the task a pod
// is started for: off on a wake turn, on otherwise. A wake reports what a
// delegation came back with. Given the tool, a wake that reads an interim
// answer ("still checking, the results will post here") asks again, each
// ask wakes another pod, and the chain ends at the depth bound with nothing
// answered. Without it the wake can only answer. A message the person sends
// while the wake runs is a steer into the wake, so it is answered without
// the tool too; one sent after the wake ends is a new human turn and has it.
func delegateToolFor(rec *SessionRecord, taskID string) string {
	for _, ref := range rec.Tasks {
		if ref.ID == taskID && ref.Role == taskRoleWake {
			return "off"
		}
	}
	return "on"
}
